#!/bin/bash
# Removes Sift: its services, shortcut, launcher entry and command, and (unless --keep-index) the index, models and
# settings. Your files are never touched. Then remove the plugin itself: omarchy plugin remove io.github.ommgh.sift
set -euo pipefail
PLUGIN="$HOME/.config/omarchy/plugins/io.github.ommgh.sift"
DATA="${SIFT_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/sift}"

if [[ -x $HOME/.local/bin/sift ]]; then
  "$HOME/.local/bin/sift" shortcut off > /dev/null 2>&1 || true
fi
systemctl --user disable --now sift.service sift-indexer.service 2> /dev/null || true
rm -f "$HOME/.config/systemd/user/sift.service" "$HOME/.config/systemd/user/sift-indexer.service"
systemctl --user daemon-reload
rm -f "$HOME/.local/bin/sift" "$HOME/.local/share/applications/sift.desktop"
# A clone outside the plugins folder was linked into it.
[[ -L $PLUGIN ]] && rm "$PLUGIN"

if [[ ${1:-} == --keep-index ]]; then
  echo "Sift is off. Its index, models and settings stay in $DATA and ~/.config/sift."
else
  rm -rf "$DATA" "${XDG_CONFIG_HOME:-$HOME/.config}/sift" "${XDG_CACHE_HOME:-$HOME/.cache}/sift" \
    "${XDG_STATE_HOME:-$HOME/.local/state}/sift"
  echo "Sift is removed, with its index, models and settings. Your files are untouched."
fi
if [[ -d $PLUGIN && ! -L $PLUGIN ]]; then
  echo "Last step: omarchy plugin remove io.github.ommgh.sift"
fi
