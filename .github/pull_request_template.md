<!-- Into dev for features and fixes (squash merge). dev → main only for a release (merge commit). See AGENTS.md, "Git workflow". -->

## What and why



## Checklist

- [ ] CHANGELOG.md: a line under Unreleased (or the release's dated section, for a release PR)
- [ ] The hardening rules in AGENTS.md still hold (private storage, no TCP, bounded readers, no symlinks followed, escaped writes)
- [ ] `omarchy plugin validate .` passes, and the change was tried on a real install (`install.sh` re-run, `sift doctor`)
- [ ] After UI changes: `omarchy restart shell`, then the panel checked
