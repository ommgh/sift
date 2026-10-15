# Sift

**Search your files by what's in them, on Omarchy.**

Press <kbd>Super</kbd> + <kbd>S</kbd> and describe what you're looking for: "the clause about pets in the lease",
"screenshot of a payment declined error", "python code editor with a rag script". Sift finds the photo, screenshot,
PDF page or document that matches, whatever it's called. Exact words count too: invoice numbers, error codes and
names are matched as written, including text read from screenshots.

Everything runs on your machine with Google DeepMind's [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2)
on [llama.cpp](https://github.com/ggml-org/llama.cpp) (Vulkan). Nothing you index leaves it.

Sift is a native Omarchy shell plugin: the search panel and the bar indicator live in `omarchy-shell` and follow
your theme. It's inspired by, and ports parts of, [DigUp](https://github.com/ARahim3/DigUp).

## Install

```bash
omarchy pkg add cmake ninja shaderc vulkan-headers spirv-headers tesseract tesseract-data-eng
git clone https://github.com/ommgh/sift ~/projects/sift && ~/projects/sift/install.sh
```

The installer builds llama-server with Vulkan, downloads the model (865 MB, checksummed), links the plugin into
`~/.config/omarchy/plugins/sift`, and starts two user services. Then bind the panel in
`~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + S", "Sift", "omarchy-shell shell toggle sift '{}'")
```

## Using it

| Key | |
| --- | --- |
| type | search as you type |
| <kbd>↑</kbd> <kbd>↓</kbd> (or <kbd>Ctrl</kbd> <kbd>J</kbd>/<kbd>K</kbd>) | move through results |
| <kbd>Enter</kbd> | open it: PDFs at the matching page |
| <kbd>Ctrl</kbd> <kbd>Enter</kbd> | show it in the file manager |
| <kbd>Ctrl</kbd> <kbd>C</kbd> | copy the file, to paste anywhere |
| <kbd>Tab</kbd> | show weaker matches |
| <kbd>Esc</kbd> | clear, then close |

The bar indicator only shows while Sift is busy: hover for progress, click to search, right-click to pause.

From a terminal: `sift search "…"`, `sift status`, `sift pause` / `sift resume`, `sift config`.

## What it reads

The folders in `~/.config/sift/config.toml` (default: Documents, Downloads, Pictures), and in them:

- **pictures and screenshots** by what they show, plus the text in screenshots and PNGs (tesseract)
- **PDFs** page by page; pages without much text (scans, slides) are read as pictures
- **documents**: txt, md, rst, org, html, rtf, docx, odt, in ~450-token passages

Inside code projects (a folder with `.git`, `package.json`, …) only screenshots are read. It never reads hidden
folders, `node_modules` and the like, keys, certificates or `.env` files. It only reads: it never changes, moves or
uploads anything.

## Staying out of the way

Indexing is the only heavy part, and Sift keeps it gentle:

- the model runs on the GPU (the discrete one if there is one), in a process that's **stopped whenever there's
  nothing to read**, so the GPU powers down and its ~1 GB is freed
- the indexer runs at idle CPU and disk priority, capped at 3 threads and 2.6 GB (`systemd/sift-indexer.service`)
- it **waits while on battery**, **pauses when the CPU or GPU passes 80 °C** (resuming under 68 °C), and rests
  between files (`pace`)
- searches use the small text model on the CPU (a query is ~20 ms), so searching never wakes the discrete GPU; it's
  unloaded after 10 idle minutes

All of that is tunable in `~/.config/sift/config.toml`.

## How it works

```
folders ─► sift-indexer ─► readers (PyMuPDF, Pillow, tesseract)
                        ─► llama-server: EmbeddingGemma 2 Q8_0, text + vision, Vulkan, only while there's work
                        ─► ~/.local/share/sift/index.db: SQLite, a float16 vector per page, passage and picture,
                           plus FTS5 over names and text
panel ──socket──► sift (daemon) ─► query vector (CPU) ─► meaning + exact words ─► results
```

Ranking is DigUp's hybrid, tuned on its evals: each vector's similarity is put on the query's own scale per
modality, only files that stand out count as meaning matches, and the share of the query's words a file contains
adds on top (codes and numbers weigh words double).

## Uninstall

```bash
systemctl --user disable --now sift sift-indexer
rm ~/.config/systemd/user/sift*.service ~/.config/omarchy/plugins/sift ~/.local/bin/sift
rm -rf ~/.local/share/sift ~/.config/sift ~/.cache/sift ~/.local/state/sift
```
