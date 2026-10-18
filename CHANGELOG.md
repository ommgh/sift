# Changelog

Sift's releases. Each one is a tag on `main` (`vX.Y.Z`) and a GitHub release built from its section here. Work in
progress is listed under Unreleased, on the `dev` branch, until it ships.

## [Unreleased]

Planned as 0.3.0.

### Added

- CI on every pull request: the privacy and safety guarantees as tests (on Python 3.12 and 3.14), and repository
  rules (no symlinks, scripts parse, one version everywhere, the pinned llama.cpp commit).
- A release process: `dev` for what's next, `main` for releases only, a GitHub release built from this changelog
  for every tag.
- The README links the marketplace listing.

### Changed

- `install.sh` installs the backend without its development tools.

### Fixed

- "show me the lease" no longer looks only at photos of a person you named "me": a name right after "show", "find",
  "give" and similar verbs isn't taken as a person.
- `sift shortcut set "SUPER + code:39"` writes the keycode the way Hyprland reads it (it was written as `CODE:39`).

### Security

- The search daemon drops requests over 64 KB instead of reading them whole.

## [0.2.0] - 2026-10-18

The first release, listed and verified on the
[Omarchy plugin marketplace](https://plugins.omarchy.org/plugin.html?id=io.github.ommgh.sift).

- Search by meaning and by exact words across documents (txt, md, rst, org, html, rtf, docx, odt), PDFs page by
  page, photos and screenshots, with EmbeddingGemma 2 on llama.cpp (Vulkan GPU, or CPU).
- OCR for screenshots and scanned pages, so error messages, codes and names are found word for word.
- A search overlay and a bar indicator for the Omarchy shell; Ctrl+S opens folders and people.
- People (opt-in): name faces once, then search "priya at the beach".
- First-run setup asks which folders to read and which key opens Sift; `sift doctor` shows what it runs on.
- Indexing runs at idle priority, only while plugged in by default, and stops the model when there's nothing to read.
- Privacy and safety: nothing leaves the machine, the model is served over a private Unix socket, what Sift keeps is
  readable by you alone, every file reader has a hard limit, and downloads are pinned and checksummed.

[Unreleased]: https://github.com/ommgh/sift/compare/v0.2.0...dev
[0.2.0]: https://github.com/ommgh/sift/releases/tag/v0.2.0
