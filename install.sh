#!/bin/bash
# Installs Sift for this user: builds llama-server, fetches the model, sets up the Python backend, links the Omarchy
# shell plugin, and starts the two services. Safe to run again (that's also how to update).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
REPO="$PWD"

missing=()
for pkg in cmake ninja shaderc vulkan-headers spirv-headers tesseract tesseract-data-eng; do
  pacman -Q "$pkg" > /dev/null 2>&1 || missing+=("$pkg")
done
command -v uv > /dev/null || missing+=(uv)
if (( ${#missing[@]} )); then
  echo "Sift needs: ${missing[*]}"
  echo "Install them with: omarchy pkg add ${missing[*]}"
  exit 1
fi

echo "==> llama.cpp (Vulkan)"
scripts/build-llama.sh
echo "==> Model"
scripts/fetch-model.sh
echo "==> Backend"
(cd backend && uv sync --quiet)

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/sift" <<EOF
#!/bin/bash
exec "$REPO/backend/.venv/bin/sift" "\$@"
EOF
chmod +x "$HOME/.local/bin/sift"

echo "==> Omarchy shell plugin"
PLUGINS="$HOME/.config/omarchy/plugins"
mkdir -p "$PLUGINS"
if [[ ! -e "$PLUGINS/sift" ]]; then
  ln -s "$REPO" "$PLUGINS/sift"
fi

echo "==> Services"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"
ln -sf "$REPO/systemd/sift.service" "$UNITS/sift.service"
ln -sf "$REPO/systemd/sift-indexer.service" "$UNITS/sift-indexer.service"
systemctl --user daemon-reload
systemctl --user enable --now sift.service sift-indexer.service
# Pick up code changes on a re-run.
systemctl --user restart sift.service sift-indexer.service

if command -v omarchy-shell > /dev/null; then
  omarchy-shell shell rescanPlugins > /dev/null 2>&1 || true
  omarchy plugin enable sift > /dev/null 2>&1 || true
  # The panel stays loaded between uses, so new panel code only loads with a fresh shell.
  omarchy restart shell > /dev/null 2>&1 || true
fi

echo
echo "Sift is running. Settings: ~/.config/sift/config.toml (sift config). Progress: sift status."
echo "Bind the search panel in ~/.config/hypr/bindings.lua, e.g.:"
echo "  o.bind(\"SUPER + S\", \"Sift\", \"omarchy-shell shell toggle sift '{}'\")"
