# Sift: agent guide

Sift is on-device semantic file search for Omarchy (Arch + Hyprland), shipped as an Omarchy shell plugin.
Press Super+S, describe a file ("the clause about pets in the lease", "me in the mountains", "INV-2041"), and get the
photo, screenshot, PDF page or document that matches, by meaning or by exact words. Nothing leaves the machine.

- Repo: github.com/ommgh/sift (public, MIT). `main` = releases only, `dev` = the next release (see "Git workflow and
  releases"). Listed on https://plugins.omarchy.org/plugin.html?id=io.github.ommgh.sift. Landing page lives in a separate private repo,
  `ommgh/sift-site` (see `../sift-site/AGENTS.md`), deployed at https://sift.ommishra.in.
- Status (2026-10-18): base version done and in daily use on the author's laptop. Next sessions improve it.

## Working agreements

- Branches and PRs, never direct pushes to `main` or `dev` (both are protected; see "Git workflow and releases").
  End commit messages with the `Co-Authored-By` line the harness asks for, PR bodies with the Claude Code line.
- No thermal pausing, pacing, or CPU/memory caps. The goal is "runs smoothly without crashing", not throttling;
  the indexer already runs at idle CPU/IO priority. Don't reintroduce temperature checks or resource limits.
- Ask before big product decisions; improving on the original design is welcome.
- Python is glue (about 44 MB daemon, 66 MB indexer, 1–4 ms per query in Python); llama-server dominates.
  A Rust/Go rewrite isn't worth it now, maybe later for packaging.
- Copy and docs: short, plain, specific; real example queries over adjectives.

## Layout

```
manifest.json          Omarchy plugin manifest: id "io.github.ommgh.sift" (plain "sift" before 0.2; install.sh migrates), kinds overlay + bar-widget, keepLoaded
ui/Panel.qml           search overlay (Quickshell), styled with omarchy-shell's [menu] tokens; first-run setup state
ui/Manage.qml          Ctrl+S view inside the panel: Folders tab (list, browse, add/remove, subfolders) and People
                       tab (face groups as thumbnails, type a name, rename/merge, forget, hide, on/off)
ui/BarWidget.qml       bar indicator, visible only while indexing/scanning/starting/faces/paused/error or not set up
backend/               uv project (Python ≥3.12), venv in ~/.local/share/sift/venv (NOT in the plugin folder:
                       Omarchy's plugin validation rejects symlinks, which blocked `omarchy plugin update`)
  sift/config.py       paths (XDG), ~/.config/sift/config.toml defaults, SIFT_INDEX / SIFT_DATA overrides;
                       comment-preserving edits (folders, top_level_only, set_value)
  sift/rules.py        what gets read and as what; skip rules; code projects; walk(); is_skipped_path()
  sift/extract.py      PDF pages, doc chunks (1800 chars / 200 overlap), image budgets, tesseract OCR
  sift/llama.py        llama-server process manager (atexit + SIGTERM cleanup, pinned media marker); GPU type
                       from libvulkan via ctypes (name regex only as fallback); threads "auto"
  sift/store.py        SQLite WAL: files, segments with float16 vectors, FTS5 over names and text
  sift/indexer.py      scan + inotify watcher, embedding batches, OCR overlapped with GPU, face pass after
  sift/search.py       hybrid ranking (below)
  sift/daemon.py       socket server for the panel; CPU text model for queries, unloaded after 10 idle min
  sift/faces.py        opt-in face recognition: YuNet + SFace (opencv_zoo, sha256-pinned), groups, sheets,
                       face crops (~/.cache/sift/faces), overview() for the People view, rename/merge, hidden faces
  sift/folders.py      folder overview with counts (most specific folder wins), suggestions, browse()
  sift/shortcut.py     the marked Sift block in ~/.config/hypr/bindings.lua; conflicts from `hyprctl binds -j`
  sift/setup.py        `sift setup` (gum questions: folders, shortcut, people) and `sift doctor [--bench]`
  sift/power.py        AC detection and pause file only
  sift/cli.py          index | serve | search | status | pause | resume | toggle | open | reveal | config |
                       faces on/off/status/groups [--open] | person add/list/remove | folders | shortcut |
                       setup | doctor
scripts/build-llama.sh llama.cpp b11461 (commit 4d756bc7…) + llama.cpp-no-logits.patch, Vulkan, GGML_NATIVE,
                       all cores (2m47s here; half the cores was 3m13s)
scripts/fetch-model.sh ggml-org/embeddinggemma-2-GGUF rev bfcd2987…, Q8_0 + mmproj (865 MB), sha-checked
systemd/               sift.service (daemon), sift-indexer.service (Nice=19, idle CPU/IO), Restart=always
install.sh             missing packages (asks, then omarchy pkg add); build ∥ model download ∥ uv sync; wrapper
                       ~/.local/bin/sift; sift.desktop (app launcher); first run: `sift setup` BEFORE the services
                       start; services; plugin enable; shell restart. A clone outside the plugins folder is
                       symlinked in (the author's dev setup). Re-run = update. Panel/bar launch it in
                       omarchy-launch-floating-terminal-with-presentation when ~/.local/bin/sift is missing.
uninstall.sh           services, shortcut block, launcher entry, wrapper, data (unless --keep-index)
backend/tests/         pytest: the privacy and safety guarantees (hostile files, permissions, escaping)
CHANGELOG.md           Unreleased (on dev) + one dated section per release; release notes come from it
.github/               README banners (light/dark, rendered from sift-site /card/banner*), FUNDING.yml, PR template,
                       workflows/checks.yml (tests on 3.12 + 3.14, repo rules), workflows/release.yml (tag → release),
                       scripts/check-repo.sh, scripts/release-notes.sh. .github/ and tests/ aren't marketplace-scanned
```

Runtime files: index `~/.local/share/sift/index.db`, models and llama build in `~/.local/share/sift/`, thumbnails in
`~/.cache/sift/thumbs`, sockets (the daemon's, and each llama-server's `llama-<name>-<pid>.sock`) and status in
`$XDG_RUNTIME_DIR/sift/`, pause flag in `~/.local/state/sift/paused`.

## Git workflow and releases

Why: the marketplace verified one exact commit (`listingValidatedCommit`, now v0.2.0 = a239f3a). Any new commit on
`main` turns the listing "Update unverified" until a maintainer re-approves it, and `omarchy plugin add`/`update`
install `main`'s HEAD (the default branch). So `main` only ever moves by a release; everything else waits on `dev`.

- Branches: `main` (releases, each a `vX.Y.Z` tag), `dev` (integration for the next release). Work happens on short
  branches off `dev`: `feat/…`, `fix/…`, `docs/…`, `chore/…`, `ci/…`. Hotfixes: `hotfix/…` off `main`.
- Both `main` and `dev` are protected, admins included: changes land by PR only, with the checks `repo rules`,
  `tests (python 3.12)` and `tests (python 3.14)` green; no force pushes or deletions. Merged branches auto-delete.
- PRs into `dev`: squash merge, title = the change. Each adds a line to CHANGELOG.md under Unreleased (Added /
  Changed / Fixed / Security). Use the PR template's checklist. Claude opens PRs; the author merges them (the
  harness refuses agent self-merges without review). To stack work while a PR waits, branch off that PR's branch
  and still target `dev`; once the lower PR is squash-merged the upper one merges cleanly (identical changes).
- The version stays the last release's on `dev` until release prep; `check-repo.sh` requires manifest.json,
  backend/pyproject.toml and uv.lock to agree.
- Local checks before a PR: `.github/scripts/check-repo.sh`; tests with
  `(cd backend && UV_PROJECT_ENVIRONMENT=~/.cache/sift-dev/venv uv run --locked pytest -q)` (never a `.venv` inside
  the repo: it's the author's live plugin folder, and Omarchy rejects plugin folders containing symlinks);
  `omarchy plugin validate .`.
- The author's machine runs whatever branch is checked out in ~/projects/omarchy/sift (the plugin folder is a
  symlink to it), so dev work is dogfooded; restart the services/shell after switching branches.

Releasing X.Y.Z (only when the author asks; semver: features → minor, fixes → patch):
1. Branch `release/vX.Y.Z` off `dev`: set the version in manifest.json and backend/pyproject.toml, run `uv lock`,
   rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a fresh empty `## [Unreleased]` above it, update the
   compare links at the bottom. PR into `dev`, merge.
2. Re-run install.sh from `dev` on this machine, `sift doctor`, real searches, `omarchy plugin validate .`.
3. PR `dev` → `main` titled "Release vX.Y.Z", merged with **Create a merge commit** (not squash: `dev` must stay an
   ancestor of `main`). The `repo rules` check requires the dated changelog section on PRs into `main`.
4. Tag the merge commit and push the tag: `git tag -a vX.Y.Z origin/main -m "Sift X.Y.Z" && git push origin vX.Y.Z`.
   workflows/release.yml checks the tag matches the version and is on `main`, then publishes the GitHub release
   from the changelog section.
5. Marketplace: open https://github.com/omacom/omarchy-plugin-marketplace/issues/new?template=verify-plugin.yml,
   choose "Verify and publish a newer upstream commit", give `io.github.ommgh.sift`, `https://github.com/ommgh/sift`
   and the full 40-char SHA of `main`'s HEAD. Answer bot or reviewer findings with a fix release; a maintainer then
   applies `approved-and-verified` and the listing moves to the new snapshot.
6. Update ../AGENTS.md (workspace notes): the verified snapshot and the date.

Hotfix: `hotfix/…` off `main` with the fix, a patch version bump and a dated changelog section → PR into `main`
(merge commit) → tag → marketplace verification as above → PR `main` → `dev` (merge commit) to bring it back.

## Hardening (keep it: the marketplace's reviewers check for exactly these)

The marketplace review (omacom/omarchy-plugin-marketplace#11729) reads the code as if another local user, or a
hostile file in Downloads, were the attacker. Invariants:
- Private storage: `config.make_private()` (umask 077; data, cache, state and config folders 0700) runs first in every
  `sift` command; `store.connect()` makes the index and its WAL/SHM 0600; units set `UMask=0077`, `NoNewPrivileges`.
- No TCP: llama-server listens on a Unix socket in `$XDG_RUNTIME_DIR/sift` (`--host *.sock`), with `--no-slots`
  and `--no-webui`. Never go back to a localhost port (other users can connect to it, or bind it first).
- Bounded decoders (`extract.py`): DOCX/ODT unpacked to at most `MAX_XML_BYTES` whatever the zip header says, and
  refused if they hold a DOCTYPE; Pillow only with `IMAGE_FORMATS` (no EPS → Ghostscript) and at most `MAX_PIXELS`
  (big JPEGs decoded with `draft`); PDF pages rendered through `extract.render` (`MAX_RENDER_PIXELS`), opened with
  `filetype="pdf"`; at most `MAX_CHUNKS` passages per file. Any new reader needs the same kind of limit.
- Symlinks are never followed out of folders (walk and the watcher both use lstat).
- Cached page renders are deleted, and orphaned face crops pruned, when the daemon goes idle.
- Anything written into another program's config is validated: shortcut keys `[A-Z0-9_:]+`, descriptions stripped
  of quotes and control characters (bindings.lua is Lua); config.toml strings via `json.dumps` and written
  atomically.
- Installs use `uv sync --locked` (uv.lock has hashes); downloads are pinned by commit/revision and SHA-256.

## How it works

- **Distribution:** marketplace users run `omarchy plugin add https://github.com/ommgh/sift --enable` (clones into
  ~/.config/omarchy/plugins/io.github.ommgh.sift; Omarchy never runs install hooks), then `install.sh` there. Keep the repo free of
  symlinks and run `omarchy plugin validate .` before pushing.
- **Model:** Google DeepMind's EmbeddingGemma 2 (Q8_0 GGUF + mmproj) on llama.cpp's llama-server. One embedding
  space for text and images. Indexing runs on the GPU (`device = "auto"`: discrete, else integrated, else CPU; the
  type comes from the Vulkan driver) and the server is stopped 60 s after the queue empties, so the GPU powers down.
  Queries use a separate CPU text-only instance in the daemon (`--device none`, ~20 ms), so searching never wakes
  a GPU. NVIDIA works through nvidia-utils' Vulkan ICD (untested on hardware; ask for `sift doctor --bench`).
- **Reading:** PDFs page by page (pages with < 200 chars of text are embedded as pictures); docs (txt, md, rst,
  org, html, rtf, docx, odt) in passages; images with vision token budgets photo 140 / screenshot 280 / page 280;
  tesseract OCR for screenshots and PNGs. Inside code projects (any `.git`, `package.json`, … marker) only
  screenshots are read. Never reads hidden folders, node_modules-style folders, keys/certs/.env (`is_secret`).
  Never writes to, moves or uploads user files.
- **Ranking (`search.py`):** per modality, cosine minus the query's mean, in standard deviations (z). Only
  z ≥ `Z_GATE` (2.0) counts as a meaning match ("nothing found" is honest). Word coverage from FTS5 adds on top
  (coverage^1.5; codes/numbers weigh `WORD_WEIGHT` 2). Kind hints in the query ("screenshot", "pdf", …) and a
  lookup mode for code-like queries. Results under `STRONG_RATIO` of the best fold behind Tab as weaker matches.
  A query naming a known person filters to photos with their face (`only`), and the rest of the query ranks them.
  A name right after "show/find/give/get/…" (`faces.NOT_A_NAME_AFTER`) isn't a person: "show me the lease".
- **Panel protocol:** JSON lines over `$XDG_RUNTIME_DIR/sift/sift.sock`. Requests
  `{"id", "op": "search", "q", "limit"}`, `{"op": "warm"}`, `{"op": "status"}`, `{"op": "ping"}`; search replies
  include hits and `people`. The panel debounces 160 ms and keeps a query pending while the socket reconnects.
  Manage ops (string ids "m…", routed to Manage.qml): folders, browse, folder-add/remove/subfolders, people, faces
  {on}, person-name {face, name} (names the last-listed group holding that face), person-rename, person-remove,
  group-hide. The panel payload `{"view": "folders"|"people"}` opens those tabs directly.
- **Panel keys:** Enter open (PDF at page, via `sift open`), Ctrl+Enter reveal, Ctrl+C copy file (uri-list),
  Tab weaker matches, Ctrl+S folders & people, Esc clear then close, Up/Down, Ctrl+J/K/N/P, PgUp/PgDn. Leave
  Super+Q behavior alone. Destructive keys in Manage (Delete, Ctrl+Delete) need a second press.
- **Shortcut:** Omarchy's default Super+S is the scratchpad toggle. `sift shortcut` writes a marked block
  (`-- Sift: begin` … `-- Sift: end`) that unbinds the old use while Sift has the key; removing it gives it back.
  A hand-written Sift line is reported and left alone.
- **Faces:** opt-in (`sift faces on`). YuNet detects, SFace embeds (CPU, ~80 ms a photo), threshold 0.363
  (cosine). `sift faces off` deletes every faceprint and name.

## Dev loop

```bash
(cd backend && UV_PROJECT_ENVIRONMENT=~/.local/share/sift/venv uv sync --locked --no-dev)   # deps (or re-run install.sh)
systemctl --user restart sift sift-indexer        # after backend changes
omarchy restart shell                             # after ANY ui/*.qml change: the panel is keepLoaded
sift status; sift search "…"; journalctl --user -u sift-indexer -f
SIFT_INDEX=/tmp/test.db sift index                # a throwaway index for experiments
```

Tests and repo rules: see "Git workflow and releases" (the dev venv lives in ~/.cache/sift-dev, never in the repo).

Gotchas learned the hard way:
- Use anchored `pkill -f '^…'` patterns; a loose pattern once killed its own shell and the real services.
- Don't hold long SQLite transactions in the indexer ("database is locked" for the daemon).
- llama-server randomizes its media marker; it's pinned via env (`LLAMA_MEDIA_MARKER="<__media__>"`).
- Watcher paths map to the most specific configured folder (a recursive folder nested in a top-level-only one).
- The plugin marketplace re-scans every commit (omacom/omarchy-plugin-marketplace,
  scripts/security-baseline-analysis.mjs). build-llama.sh must keep the literal 40-char SHA in its fetch and in a
  `checkout --detach <sha>` that is `&&`-chained all the way to the cmake build, or it's flagged
  `remote-git-execution-unpinned`. Bumping llama.cpp means changing COMMIT and both literal SHAs.
- GNOME's `localsearch-3` may also be indexing on the machine (duplicate work), unrelated to Sift.

## Measured (MSI Bravo 15: Ryzen 5 5600H, 15 GB, RX 5500M 4 GB + Vega iGPU)

Per picture at 280 tokens: dGPU 1.94 s, iGPU 1.74–1.81 s, CPU 3.36 s at 6 threads ("auto" on CPU) vs 5.4 s at 3.
Per text chunk: dGPU 52 ms, iGPU 157–177 ms, CPU 334 ms (6 threads). Query ~20 ms on CPU. Resident memory: daemon 44 MB, indexer 66 MB, llama-server 562 MB (only while
indexing). These numbers are on the landing page; update both if they change.

## License

MIT. `LICENSE` also carries a third-party MIT notice for ported parts; it's required, so don't remove it unless
those parts are rewritten independently. Details are in the private workspace notes (`../AGENTS.md`).

## Ideas on the table (none started)

- Lower the screenshot image budget (~2× faster indexing) after checking quality.
- Audio/video search, or a `code:` mode for source files.
- Face threshold 0.5 to drop weak false positives.
- Packaging (AUR / Omarchy package), possibly with a compiled backend.
