"""The keyboard shortcut that opens Sift: a marked block in ~/.config/hypr/bindings.lua that `sift shortcut` writes,
changes and removes. When the key already does something (Omarchy's Super+S toggles the scratchpad), the block unbinds
that first, and removing the block gives the key back. A Sift binding the user wrote by hand is left alone."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

BINDINGS = Path(os.environ.get("SIFT_BINDINGS", Path.home() / ".config/hypr/bindings.lua"))
BEGIN = "-- Sift: begin (written by `sift shortcut`; change or remove it with that command)"
END = "-- Sift: end"
PLUGIN_ID = "io.github.ommgh.sift"
COMMAND = f"omarchy-shell shell toggle {PLUGIN_ID} '{{}}'"

MODS = {"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}
ALIASES = {"CONTROL": "CTRL", "WIN": "SUPER", "MOD4": "SUPER", "META": "SUPER", "CMD": "SUPER", "OPTION": "ALT"}
ORDER = ["SUPER", "SHIFT", "CTRL", "ALT"]

# Offered by `sift shortcut pick`, in this order: the first one is Sift's own (and what the README says).
SUGGESTIONS = ["SUPER + S", "SUPER + CTRL + ALT + S", "SUPER + SHIFT + CTRL + S"]


def normalize(combo: str) -> str:
    """"super+s", "Super S", "SUPER + s" → "SUPER + S"."""
    parts = [p for p in re.split(r"[\s+]+", combo.strip().upper()) if p]
    if not parts:
        raise ValueError("no key given")
    parts = [ALIASES.get(p, p) for p in parts]
    mods, key = parts[:-1], parts[-1]
    if key.startswith("CODE:"):
        key = "code:" + key[5:]   # Hyprland only knows the lowercase prefix
    # A key name (S, F1, SPACE, XF86AUDIOMUTE) or a keycode (code:39), nothing else: it's written into a Lua file.
    if key in MODS or any(m not in MODS for m in mods) or not re.fullmatch(r"[A-Z0-9_]+|code:\d+", key):
        raise ValueError(f"not a key combination: {combo!r} (try \"SUPER + S\")")
    return " + ".join([m for m in ORDER if m in mods] + [key])


def _mask_and_key(combo: str) -> tuple[int, str]:
    parts = combo.split(" + ")
    return sum(MODS[m] for m in parts[:-1]), parts[-1].upper()


def live_binds() -> list[dict] | None:
    """Hyprland's bindings right now, or None when it isn't reachable."""
    try:
        out = subprocess.run(["hyprctl", "binds", "-j"], capture_output=True, text=True, timeout=5)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def used_by(combo: str, binds: list[dict] | None = None) -> str | None:
    """What the key does now (its description), other than opening Sift; None when it's free."""
    binds = live_binds() if binds is None else binds
    mask, key = _mask_and_key(normalize(combo))
    for b in binds or []:
        if b.get("modmask") == mask and str(b.get("key", "")).upper() == key and not b.get("submap"):
            what = b.get("description") or f"{b.get('dispatcher')} {b.get('arg')}".strip()
            if what != "Sift":
                return what
    return None


def _text() -> str:
    try:
        return BINDINGS.read_text()
    except FileNotFoundError:
        return ""


def _without_block(text: str) -> str:
    return re.sub(rf"\n*{re.escape(BEGIN)}\n.*?{re.escape(END)}\n?", "\n", text, flags=re.S).rstrip("\n") + "\n"


def current() -> tuple[str | None, bool]:
    """(the combo that opens Sift, whether `sift shortcut` manages it). (None, False) when nothing does."""
    text = _text()
    block = re.search(rf"{re.escape(BEGIN)}\n(.*?){re.escape(END)}", text, re.S)
    if block:
        found = re.search(r'o\.bind\("([^"]+)"', block.group(1))
        return (found.group(1) if found else None), True
    for line in _without_block(text).splitlines():
        found = re.match(r'\s*o\.bind\("([^"]+)".*toggle (?:io\.github\.ommgh\.)?sift\b', line)
        if found:
            return found.group(1), False
    return None, False


def _reload_until(check) -> None:
    """Hyprland picks up a saved bindings.lua by itself; reload it if that hasn't happened in a moment."""
    for _ in range(10):
        if check():
            return
        time.sleep(0.15)
    subprocess.run(["hyprctl", "reload"], capture_output=True, timeout=10)


def _replaced_note() -> str | None:
    found = re.search(rf'{re.escape(BEGIN)}\n-- .*? was "(.*)": unbound', _text())
    return found.group(1) if found else None


def bind(combo: str) -> str | None:
    """Binds combo to Sift, replacing Sift's previous managed shortcut. Returns what the key did before, if anything."""
    combo = normalize(combo)
    combo_now, managed = current()
    if combo_now and not managed:
        raise RuntimeError(f"{BINDINGS} already opens Sift with {combo_now} (a line you wrote): edit it there")
    if managed and combo_now == combo and COMMAND in _text():
        return _replaced_note()   # already so; its unbind (if any) stays
    replaced = _replaced_note() if managed and combo_now == combo else used_by(combo)
    if replaced:   # another binding's description, kept in a Lua comment: one line, no quotes
        replaced = re.sub(r'[\x00-\x1f"\\]+', " ", replaced).strip()
    text = _without_block(_text())
    lines = [BEGIN]
    if replaced:
        lines.append(f"-- {combo} was \"{replaced}\": unbound while Sift has it")
        lines.append(f'hl.unbind("{combo}")')
    lines.append(f'o.bind("{combo}", "Sift", "{COMMAND}")')
    lines.append(END)
    BINDINGS.parent.mkdir(parents=True, exist_ok=True)
    BINDINGS.write_text(text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n")
    if live_binds() is not None:
        _reload_until(lambda: used_by(combo) is None and _bound_to_sift(combo))
    return replaced


def _bound_to_sift(combo: str) -> bool:
    mask, key = _mask_and_key(normalize(combo))
    return any(b.get("modmask") == mask and str(b.get("key", "")).upper() == key and b.get("description") == "Sift"
               for b in live_binds() or [])


def remove() -> bool:
    """Removes Sift's managed shortcut (and gives back the key it took). False if there was none."""
    combo, managed = current()
    if not managed:
        return False
    BINDINGS.write_text(_without_block(_text()))
    if combo and live_binds() is not None:
        _reload_until(lambda: not _bound_to_sift(combo))
    return True
