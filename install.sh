#!/bin/bash
# Sets Sift up for this user: installs what's missing, builds llama-server while the model and the Python backend
# download, asks which folders to read and which key opens Sift (first run), and starts the two services. Safe to run
# again: that's also how to update (after `omarchy plugin update io.github.ommgh.sift` or `git pull`).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
REPO="$PWD"
DATA="${SIFT_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/sift}"
LOGS="${XDG_STATE_HOME:-$HOME/.local/state}/sift"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/sift/config.toml"
PLUGINS="$HOME/.config/omarchy/plugins"
ID=io.github.ommgh.sift
# Sift's data holds what's in your files (and its logs, file names): readable by you alone.
mkdir -p "$DATA" "$LOGS"
chmod 700 "$DATA" "$LOGS"

FIRST_RUN=false
[[ -f $CONFIG ]] || FIRST_RUN=true
ASK=false
[[ -t 0 && -t 1 ]] && command -v gum > /dev/null && ASK=true
[[ ${1:-} == --setup ]] && FIRST_RUN=true

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
# Runs a command under a spinner in a terminal, plainly otherwise.
busy() {
  local title=$1; shift
  if $ASK; then gum spin --spinner dot --title "$title" -- "$@"; else echo "$title"; "$@"; fi
}

# ------------------------------------------------------------ packages
missing=()
for pkg in cmake ninja shaderc vulkan-headers spirv-headers tesseract tesseract-data-eng; do
  pacman -Q "$pkg" > /dev/null 2>&1 || missing+=("$pkg")
done
command -v uv > /dev/null || missing+=(uv)
if (( ${#missing[@]} )); then
  step "Sift needs a few packages: ${missing[*]}"
  if [[ -t 0 ]] && { ! $ASK || gum confirm "Install them now (asks for your password)?"; }; then
    omarchy pkg add "${missing[@]}"
  else
    echo "Install them with: omarchy pkg add ${missing[*]}"
    exit 1
  fi
fi

# ------------------------------------------------------------ engine, model and backend, side by side
step "Setting up the search engine"
echo "Building llama.cpp for this machine while the model (865 MB) and the Python backend download."
echo "About 3 minutes the first time; a re-run only redoes what changed. Logs: $LOGS"
scripts/fetch-model.sh > "$LOGS/setup-model.log" 2>&1 &
model=$!
# The backend's environment lives with Sift's data, not in the plugin folder: Omarchy refuses plugin folders that
# contain symlinks (a virtualenv is full of them), which would block `omarchy plugin update`.
(cd backend && UV_PROJECT_ENVIRONMENT="$DATA/venv" uv sync --quiet) > "$LOGS/setup-backend.log" 2>&1 &
backend=$!
if ! busy "Building llama.cpp (Vulkan)…" sh -c 'exec scripts/build-llama.sh > "$0" 2>&1' "$LOGS/setup-build.log"; then
  tail -n 30 "$LOGS/setup-build.log"
  echo "Building llama.cpp failed; the full log is $LOGS/setup-build.log"
  exit 1
fi
tail -n 1 "$LOGS/setup-build.log"
if ! busy "Downloading the model…" tail --pid="$model" -f /dev/null || ! wait "$model"; then
  tail -n 5 "$LOGS/setup-model.log"
  echo "Downloading the model failed (a re-run resumes it); the log is $LOGS/setup-model.log"
  exit 1
fi
tail -n 1 "$LOGS/setup-model.log"
if ! wait "$backend"; then
  tail -n 20 "$LOGS/setup-backend.log"
  echo "Setting up the Python backend failed; the log is $LOGS/setup-backend.log"
  exit 1
fi
# Where it used to live (inside the plugin folder): gone, see above.
[[ -f backend/.venv/pyvenv.cfg ]] && rm -rf backend/.venv

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/sift" <<EOF
#!/bin/bash
exec "$DATA/venv/bin/sift" "\$@"
EOF
chmod +x "$HOME/.local/bin/sift"
SIFT="$HOME/.local/bin/sift"

# ------------------------------------------------------------ the plugin and its launcher entry
# Added with `omarchy plugin add`, Sift already sits in the plugins folder; a clone elsewhere gets linked there.
# Before 0.2 the plugin id was plain "sift".
if [[ -L $PLUGINS/sift && $(readlink -f "$PLUGINS/sift") == "$REPO" ]]; then
  omarchy plugin disable sift > /dev/null 2>&1 || true
  rm "$PLUGINS/sift"
fi
if [[ $REPO != "$PLUGINS/$ID" && ! -e $PLUGINS/$ID ]]; then
  mkdir -p "$PLUGINS"
  ln -s "$REPO" "$PLUGINS/$ID"
fi
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/sift.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Sift
GenericName=File search
Comment=Find files by what's in them
Exec=omarchy-shell shell toggle io.github.ommgh.sift '{}'
Icon=system-search
Terminal=false
Categories=Utility;
Keywords=search;find;files;photos;pdf;documents;
EOF

# ------------------------------------------------------------ first run: what to read, which key
if $FIRST_RUN && $ASK; then
  "$SIFT" setup
else
  "$SIFT" shortcut refresh > /dev/null 2>&1 || true
fi

# ------------------------------------------------------------ services
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"
ln -sf "$REPO/systemd/sift.service" "$UNITS/sift.service"
ln -sf "$REPO/systemd/sift-indexer.service" "$UNITS/sift-indexer.service"
systemctl --user daemon-reload
systemctl --user enable --quiet sift.service sift-indexer.service
# A re-run picks up new code.
systemctl --user restart sift.service sift-indexer.service

if command -v omarchy-shell > /dev/null; then
  omarchy-shell shell rescanPlugins > /dev/null 2>&1 || true
  omarchy plugin enable "$ID" > /dev/null 2>&1 || true
  # The panel stays loaded between uses, so new panel code only loads with a fresh shell.
  omarchy restart shell > /dev/null 2>&1 || true
fi

step "Sift is running"
combo=$("$SIFT" shortcut 2>/dev/null | head -n 1)
case $combo in
  "No shortcut"*) echo "Open it from the app launcher (Sift), or give it a key: sift shortcut pick" ;;
  *) echo "Press ${combo%%  *} to search. It reads your folders in the background; the bar icon shows how far along it is." ;;
esac
echo "Folders and people: Ctrl+S in the panel. Everything else: sift --help, sift doctor."
if $FIRST_RUN && [[ -n ${WAYLAND_DISPLAY:-} ]]; then
  (sleep 2 && omarchy-shell shell toggle "$ID" '{}' > /dev/null 2>&1) &
fi
