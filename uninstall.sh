#!/bin/bash
# Removes Sift: its services, shortcut, launcher entry and command, and (unless --keep-index) the index, models and
# settings. Your files are never touched. Then remove the plugin itself: omarchy plugin remove sift
set -euo pipefail
DATA="${SIFT_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/sift}"

if [[ -x $HOME/.local/bin/sift ]]; then
  "$HOME/.local/bin/sift" shortcut off > /dev/null 2>&1 || true
fi
systemctl --user disable --now sift.service sift-indexer.service 2> /dev/null || true
rm -f "$HOME/.config/systemd/user/sift.service" "$HOME/.config/systemd/user/sift-indexer.service"
systemctl --user daemon-reload
rm -f "$HOME/.local/bin/sift" "$HOME/.local/share/applications/sift.desktop"
# A clone outside the plugins folder was linked into it.
[[ -L $HOME/.config/omarchy/plugins/sift ]] && rm "$HOME/.config/omarchy/plugins/sift"

if [[ ${1:-} == --keep-index ]]; then
  echo "Sift is off. Its index, models and settings stay in $DATA and ~/.config/sift."
else
  rm -rf "$DATA" "${XDG_CONFIG_HOME:-$HOME/.config}/sift" "${XDG_CACHE_HOME:-$HOME/.cache}/sift" \
    "${XDG_STATE_HOME:-$HOME/.local/state}/sift"
  echo "Sift is removed, with its index, models and settings. Your files are untouched."
fi
if [[ -d $HOME/.config/omarchy/plugins/sift && ! -L $HOME/.config/omarchy/plugins/sift ]]; then
  echo "Last step: omarchy plugin remove sift"
fi
