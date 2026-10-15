# Sift: agent guide

Sift is on-device semantic file search for Omarchy (Arch + Hyprland), shipped as an Omarchy shell plugin.
Press Super+S, describe a file ("the clause about pets in the lease", "me in the mountains", "INV-2041"), and get the
photo, screenshot, PDF page or document that matches, by meaning or by exact words. Nothing leaves the machine.

- Repo: github.com/ommgh/sift (public, MIT), branch `main`. Landing page lives in a separate private repo,
  `ommgh/sift-site` (see `../sift-site/AGENTS.md`), deployed at https://sift.ommishra.in.
- Status (2026-10-16): base version done and in daily use on the author's laptop. Next sessions improve it.

## Working agreements

- Commit straight to `main` and push at meaningful milestones. No branches or PRs. End commit messages with the
  `Co-Authored-By` line the harness asks for.
- No thermal pausing, pacing, or CPU/memory caps. The goal is "runs smoothly without crashing", not throttling;
  the indexer already runs at idle CPU/IO priority. Don't reintroduce temperature checks or resource limits.
- Ask before big product decisions; improving on the original design is welcome.
- Python is glue (about 44 MB daemon, 66 MB indexer, 1–4 ms per query in Python); llama-server dominates.
  A Rust/Go rewrite isn't worth it now, maybe later for packaging.
- Copy and docs: short, plain, specific; real example queries over adjectives.

## Layout

```
manifest.json          Omarchy plugin manifest: id "sift", kinds overlay + bar-widget, keepLoaded
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
.github/               README banners (light/dark, rendered from sift-site /card/banner*), FUNDING.yml
```

Runtime files: index `~/.local/share/sift/index.db`, models and llama build in `~/.local/share/sift/`, thumbnails in
`~/.cache/sift/thumbs`, socket and status in `$XDG_RUNTIME_DIR/sift/`, pause flag in `~/.local/state/sift/paused`.

## How it works

- **Distribution:** marketplace users run `omarchy plugin add https://github.com/ommgh/sift --enable` (clones into
  ~/.config/omarchy/plugins/sift; Omarchy never runs install hooks), then `install.sh` there. Keep the repo free of
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
  Caveat: any query containing the word "me" is restricted to the "me" person's photos when that person exists.
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
(cd backend && UV_PROJECT_ENVIRONMENT=~/.local/share/sift/venv uv sync)   # deps (or just re-run install.sh)
systemctl --user restart sift sift-indexer        # after backend changes
omarchy restart shell                             # after ANY ui/*.qml change: the panel is keepLoaded
sift status; sift search "…"; journalctl --user -u sift-indexer -f
SIFT_INDEX=/tmp/test.db sift index                # a throwaway index for experiments
```

Gotchas learned the hard way:
- Use anchored `pkill -f '^…'` patterns; a loose pattern once killed its own shell and the real services.
- Don't hold long SQLite transactions in the indexer ("database is locked" for the daemon).
- llama-server randomizes its media marker; it's pinned via env (`LLAMA_MEDIA_MARKER="<__media__>"`).
- Watcher paths map to the most specific configured folder (a recursive folder nested in a top-level-only one).
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
