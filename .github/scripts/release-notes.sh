#!/bin/bash
# Prints the CHANGELOG.md section for a release tag, after checking the tag is one: vX.Y.Z matching manifest.json,
# on a commit that's on main. The release workflow turns the output into the GitHub release.
set -euo pipefail
cd "$(dirname "$0")/../.."
tag=${1:?usage: release-notes.sh vX.Y.Z}
version=${tag#v}

manifest=$(python3 -c 'import json; print(json.load(open("manifest.json"))["version"])')
[[ $tag == "v$manifest" ]] || { echo "tag $tag doesn't match manifest.json's version $manifest" >&2; exit 1; }
git merge-base --is-ancestor "$tag^{commit}" origin/main \
  || { echo "$tag isn't on main: releases are tagged on main only" >&2; exit 1; }

notes=$(awk -v heading="## [$version]" '
  index($0, heading) == 1 { found = 1; next }
  found && /^## \[/ { exit }
  found && /^\[[^]]+\]: / { exit }
  found { print }
' CHANGELOG.md)
[[ -n ${notes//[[:space:]]/} ]] || { echo "CHANGELOG.md has no section for $version" >&2; exit 1; }
printf '%s\n' "$notes"
