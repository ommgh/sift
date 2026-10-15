#!/bin/bash
# Builds llama-server (llama.cpp, MIT) with Vulkan, for Sift's embedding runtime.
#
# Pinned to a tag verified against EmbeddingGemma 2's reference vectors (b11452+ is the first with
# text + vision + audio), plus a patch that stops non-causal embedding contexts reserving logits
# (1 MiB per token with Gemma's 262k vocabulary). Built for this machine's CPU only (GGML_NATIVE).
#
# Output: $SIFT_DATA/llama/bin/llama-server (default ~/.local/share/sift). Does nothing if that build is current.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG=b11461
COMMIT=4d756bc72bf00a4aacf410ae15a2d315f3db400d
PATCH="$PWD/scripts/llama.cpp-no-logits.patch"
DATA="${SIFT_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/sift}"
SOURCE="$DATA/llama/src"
BUILD="$DATA/llama/build"
OUT="$DATA/llama"
FLAVOR=$(command -v glslc > /dev/null && echo vulkan || echo cpu)
STAMP="$TAG $(sha256sum "$PATCH" | cut -c1-12) $FLAVOR"

if [[ -x "$OUT/bin/llama-server" && "$(cat "$OUT/BUILD" 2>/dev/null)" == "$STAMP" ]]; then
  echo "llama.cpp $TAG ($FLAVOR) already built"
  exit 0
fi

mkdir -p "$OUT"
# Fetched by its exact commit (a tag can move), and checked out detached.
if [[ ! -d "$SOURCE/.git" ]]; then
  rm -rf "$SOURCE"
  git init --quiet "$SOURCE"
  # The SHA is written out (not $COMMIT) so the marketplace's checks can see what's fetched; keep both in step.
  git -C "$SOURCE" fetch --quiet --depth 1 https://github.com/ggml-org/llama.cpp 4d756bc72bf00a4aacf410ae15a2d315f3db400d
  git -C "$SOURCE" -c advice.detachedHead=false checkout --quiet --detach 4d756bc72bf00a4aacf410ae15a2d315f3db400d
fi
if [[ "$(git -C "$SOURCE" rev-parse HEAD)" != "$COMMIT" ]]; then
  echo "llama.cpp's $TAG isn't commit $COMMIT: not building it" >&2
  exit 1
fi
if git -C "$SOURCE" apply --check "$PATCH" 2>/dev/null; then
  git -C "$SOURCE" apply "$PATCH"
elif ! git -C "$SOURCE" apply --reverse --check "$PATCH" 2>/dev/null; then
  echo "$PATCH doesn't apply and isn't applied already" >&2
  exit 1
fi

GPU=()
[[ $FLAVOR == vulkan ]] && GPU=(-DGGML_VULKAN=ON)
cmake -S "$SOURCE" -B "$BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON "${GPU[@]}" \
  -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TOOLS=ON -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DBUILD_SHARED_LIBS=OFF > /dev/null
cmake --build "$BUILD" -j "$(nproc)" --target llama-server > /dev/null

mkdir -p "$OUT/bin"
cp "$BUILD/bin/llama-server" "$OUT/bin/"
echo "$STAMP" > "$OUT/BUILD"
echo "Built llama.cpp $TAG ($FLAVOR) into $OUT/bin"
