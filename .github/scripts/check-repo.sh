#!/bin/bash
# What has to hold for every commit on dev and main (CI runs it; run it locally before a PR too):
# no symlinks (Omarchy refuses plugin folders with them), scripts that parse, one version everywhere, the literal
# llama.cpp SHA the marketplace scan follows, and, for main, a changelog entry for the version being released.
set -euo pipefail
cd "$(dirname "$0")/../.."
fail() { echo "✗ $*" >&2; status=1; }
status=0

links=$(git ls-files -s | awk '$1 == "120000" { print $4 }')
[[ -z $links ]] || fail "symlinks in the repo: $links"

for script in $(git ls-files '*.sh'); do
  bash -n "$script" || fail "$script doesn't parse"
done

manifest=$(python3 -c 'import json; print(json.load(open("manifest.json"))["version"])')
project=$(python3 -c 'import tomllib; print(tomllib.load(open("backend/pyproject.toml", "rb"))["project"]["version"])')
locked=$(python3 -c 'import tomllib; print(next(p["version"] for p in tomllib.load(open("backend/uv.lock", "rb"))["package"] if p["name"] == "sift"))')
[[ $manifest == "$project" && $project == "$locked" ]] \
  || fail "versions differ: manifest.json $manifest, pyproject.toml $project, uv.lock $locked (run uv lock)"

commit=$(sed -n 's/^COMMIT=\([0-9a-f]\{40\}\)$/\1/p' scripts/build-llama.sh)
[[ -n $commit ]] || fail "build-llama.sh: no COMMIT=<40-char sha>"
for use in "fetch --quiet --depth 1 https://github.com/ggml-org/llama.cpp $commit" "checkout --quiet --force --detach $commit"; do
  grep -qF -- "$use" scripts/build-llama.sh || fail "build-llama.sh: the literal SHA must appear in: $use"
done

if [[ ${BASE_REF:-} == main ]]; then
  grep -qE "^## \[$manifest\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" CHANGELOG.md \
    || fail "CHANGELOG.md has no dated '## [$manifest] - YYYY-MM-DD' section for this release"
fi

(( status == 0 )) && echo "✓ repo rules"
exit $status
