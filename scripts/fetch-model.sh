#!/bin/bash
# Downloads EmbeddingGemma 2 (ggml-org's Q8_0 GGUF, a pinned revision) and checks it. 865 MB, once.
set -euo pipefail
DATA="${SIFT_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/sift}"
REPO=ggml-org/embeddinggemma-2-GGUF
REV=bfcd298762cc34d0357ece5ebdd31791a3a374d8
mkdir -p "$DATA/models"
cd "$DATA/models"
while read -r sum name; do
  if [[ -f $name ]] && echo "$sum  $name" | sha256sum -c --quiet 2>/dev/null; then
    continue
  fi
  echo "Downloading $name"
  curl -fL --retry 3 -C - --progress-bar -o "$name.part" "https://huggingface.co/$REPO/resolve/$REV/$name"
  echo "$sum  $name.part" | sha256sum -c --quiet
  mv "$name.part" "$name"
done <<SUMS
2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135 embeddinggemma-2-Q8_0.gguf
c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f mmproj-embeddinggemma-2-Q8_0.gguf
SUMS
echo "Model ready in $DATA/models"
