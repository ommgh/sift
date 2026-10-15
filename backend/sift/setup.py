"""First-run questions (`sift setup`: which folders, which shortcut, people or not) and `sift doctor`, which says what
Sift runs on and what's missing. Questions use gum, which Omarchy ships; without a terminal they're skipped."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, folders, llama, shortcut


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and shutil.which("gum") is not None


def gum(*args: str) -> str | None:
    """A gum prompt's answer, or None when it was cancelled (Esc, Ctrl+C)."""
    result = subprocess.run(["gum", *args], stdout=subprocess.PIPE, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def confirm(question: str, default: bool) -> bool:
    return subprocess.run(["gum", "confirm", question, f"--default={'yes' if default else 'no'}"]).returncode == 0


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


# ---------------------------------------------------------------- folders

def ask_folders() -> None:
    current = {f["path"]: f for f in config.folder_list()}
    candidates = []
    for entry in folders.COMMON:
        path = Path(os.path.expanduser(entry)).resolve()
        if path.is_dir() and str(path) not in current and config.covered_by(path) is None:
            candidates.append(path)
    candidates = [Path(p) for p in current] + candidates
    print("Counting files…", end="\r")
    labels = {}
    for path in candidates:
        n = folders.count_files(path, limit=100_000)
        labels[f"{config.display_path(path)}  ·  {n:,}{'+' if n >= 100_000 else ''} file{'' if n == 1 else 's'}"] = path
    print(" " * 20, end="\r")
    selected = [label for label, path in labels.items() if str(path) in current]
    heading("Which folders should Sift read?")
    print("Space selects, Enter confirms. You can change this any time in the panel (Ctrl+S) or with sift folders.")
    picked = gum("choose", "--no-limit", "--height=14", f"--selected={','.join(selected)}",
                 *labels.keys(), "Another folder…")
    if picked is None:
        return
    chosen = [labels[line] for line in picked.splitlines() if line in labels]
    if "Another folder…" in picked.splitlines():
        while True:
            other = gum("file", "--directory", "--file=false", str(Path.home()))
            if other and Path(other).is_dir():
                chosen.append(Path(other).resolve())
            if not confirm("Add another folder?", default=False):
                break
    if not chosen:
        print("No folders: Sift won't read anything until you add some (sift folders add PATH).")
    deep = set(chosen)
    if chosen:
        names = {config.display_path(p): p for p in chosen}
        heading("Read their subfolders too?")
        print("Unselect a folder to read only the files directly in it (handy for a busy Downloads).")
        keep = gum("choose", "--no-limit", f"--selected={','.join(names)}", *names.keys())
        if keep is not None:
            deep = {names[line] for line in keep.splitlines() if line in names}
    for path in list(current):
        if Path(path) not in chosen:
            config.remove_folder(Path(path))
    for path in chosen:
        config.add_folder(path, subfolders=path in deep)


# ---------------------------------------------------------------- shortcut

def ask_shortcut() -> None:
    combo, managed = shortcut.current()
    if combo and not managed:
        print(f"Sift opens with {combo} (a binding in your bindings.lua).")
        return
    binds = shortcut.live_binds()
    options = {}
    for suggestion in shortcut.SUGGESTIONS:
        taken = shortcut.used_by(suggestion, binds) if binds is not None else None
        if combo == suggestion:
            note = "current"
        elif taken:
            note = f"replaces “{taken}”"
        else:
            note = "free"
        options[f"{suggestion.title()}   ({note})"] = suggestion
    other, none = "Another key…", "No shortcut (open Sift from the app launcher or the bar)"
    heading("Which shortcut should open Sift?")
    picked = gum("choose", *options.keys(), other, none)
    if picked is None:
        return
    if picked == none:
        if shortcut.remove():
            print("Shortcut removed.")
        return
    if picked == other:
        while True:
            typed = gum("input", "--placeholder", "e.g. SUPER + CTRL + F")
            if not typed:
                return
            try:
                choice = shortcut.normalize(typed)
            except ValueError as e:
                print(e)
                continue
            taken = shortcut.used_by(choice, binds) if binds is not None else None
            if taken and not confirm(f"{choice} is “{taken}”. Use it for Sift instead?", default=False):
                continue
            break
    else:
        choice = options[picked]
    replaced = shortcut.bind(choice)
    print(f"{choice.title()} opens Sift" + (f" (it was “{replaced}”; sift shortcut off gives it back)." if replaced else "."))


def ask_faces() -> None:
    if config.load().faces.enabled:
        return
    heading("Recognize people in your photos?")
    print("Then you can search \"priya at the beach\". You name faces in the panel (Ctrl+S); faceprints stay on this\n"
          "machine and turning it off deletes them. Downloads two small models (37 MB).")
    if confirm("Turn on people?", default=False):
        from . import faces
        faces.ensure_models()
        config.set_faces_enabled(True)


def run(only: str | None = None) -> None:
    if not interactive():
        print("sift setup asks its questions in a terminal (and needs gum). Meanwhile: sift folders, sift shortcut.")
        return
    config.ensure_config()
    if only in (None, "folders"):
        ask_folders()
    if only in (None, "shortcut"):
        ask_shortcut()
    if only in (None, "people"):
        ask_faces()


# ---------------------------------------------------------------- doctor

def _ok(good: bool, text: str) -> None:
    print(f"  {'✓' if good else '✗'} {text}")


def _gpus_on_bus() -> list[str]:
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.split(": ", 1)[-1] for line in out.splitlines() if " VGA " in line or "3D controller" in line
            or "Display controller" in line]


def doctor(bench: bool = False) -> None:
    cfg = config.load()
    heading("Engine")
    stamp = config.LLAMA_SERVER.parent.parent / "BUILD"
    _ok(config.LLAMA_SERVER.exists(), f"llama-server {stamp.read_text().strip() if stamp.exists() else 'not built'}")
    for model in (config.TEXT_MODEL, config.MEDIA_MODEL):
        _ok(model.exists(), f"{model.name} ({model.stat().st_size >> 20} MB)" if model.exists() else f"{model.name} missing")

    heading("Hardware")
    devices = llama.list_devices() if config.LLAMA_SERVER.exists() else {}
    kinds = llama.classify(devices)
    picked = llama.pick_device(cfg.power.device) if devices else None
    for device, name in devices.items():
        print(f"  {'→' if device == picked else ' '} {device}: {name} ({kinds[device]})")
    bus = _gpus_on_bus()
    if any("nvidia" in g.lower() for g in bus) and not any(
            "nvidia" in n.lower() or "geforce" in n.lower() or "rtx" in n.lower() for n in devices.values()):
        _ok(False, "an NVIDIA GPU is in the machine but has no Vulkan driver: omarchy pkg add nvidia-utils "
                   "(or the -580xx one for older cards), then reboot")
    if not devices:
        print("  No GPU through Vulkan: indexing runs on the CPU, about 3x slower. Vulkan drivers: vulkan-radeon (AMD),")
        print("  vulkan-intel (Intel), nvidia-utils (NVIDIA).")
    threads = llama.threads_for(cfg.power.threads, picked)
    print(f"  Indexing runs on {devices.get(picked, 'the CPU')}, {threads} threads "
          f"(device = \"{cfg.power.device}\", threads = {json.dumps(cfg.power.threads)} in sift config).")
    print("  Searching always runs on the CPU (~20 ms a query).")

    heading("Running")
    for unit in ("sift", "sift-indexer"):
        state = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True).stdout.strip()
        _ok(state == "active", f"{unit}.service {state}")
    try:
        plugins = json.loads(subprocess.run(["omarchy", "plugin", "list", "--json"], capture_output=True, text=True,
                                            timeout=10).stdout)
        mine = next((p for p in plugins if p.get("id") == shortcut.PLUGIN_ID), None)
        _ok(bool(mine and mine.get("enabled")), "shell plugin " + ("enabled" if mine and mine.get("enabled") else
                                                                    f"not enabled: omarchy plugin enable {shortcut.PLUGIN_ID}"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    combo, managed = shortcut.current()
    _ok(combo is not None, f"shortcut {combo}" if combo else "no shortcut: sift shortcut set \"SUPER + S\"")
    try:
        status = json.loads(config.STATUS_FILE.read_text())
        print(f"  index: {status.get('state')}, {status.get('done', 0):,} of {status.get('total', 0):,} files read")
    except (OSError, ValueError):
        pass

    if bench:
        benchmark(picked, threads)


def benchmark(device: str | None, threads: int) -> None:
    """Times the model on this machine: a screenshot-sized picture and a batch of passages."""
    import io

    from PIL import Image, ImageDraw

    from . import extract

    heading("Speed")
    print("  Loading the model…", end="\r")
    server = llama.LlamaServer(media=True, device=device, threads=threads, name="bench")
    try:
        server.start()
        image = Image.new("RGB", (1600, 1000), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        for i in range(30):
            draw.text((40, 30 + i * 32), f"Line {i}: payment declined, card ending 4242, retry in settings", fill=(20, 20, 20))
        png = extract.prepare_image(image, extract.SCREENSHOT_BUDGET)
        server.embed_image(png)   # warms up
        start = time.time()
        for _ in range(3):
            server.embed_image(png)
        picture = (time.time() - start) / 3
        texts = [extract.document_prompt("notes", "The tenant may keep one cat or small dog with written consent. " * 25)] * 8
        server.embed_texts(texts)
        start = time.time()
        server.embed_texts(texts)
        passage = (time.time() - start) / 8
        print(f"\r  {picture:.2f} s a screenshot or page, {passage * 1000:.0f} ms a text passage "
              f"(photos take about half a screenshot's time)")
        try:
            busy = json.loads(config.STATUS_FILE.read_text()).get("state") in ("indexing", "faces")
        except (OSError, ValueError):
            busy = False
        if busy:
            print("  (the indexer was running at the same time, so this is slower than it'll really be)")
    finally:
        server.stop()
