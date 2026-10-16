"""Paths and settings. Settings live in ~/.config/sift/config.toml; everything has a default."""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "sift"
DATA_DIR = Path(os.environ.get("SIFT_DATA", Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "sift"))
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "sift"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "sift"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "sift"

CONFIG_FILE = CONFIG_DIR / "config.toml"
INDEX_DB = Path(os.environ.get("SIFT_INDEX", DATA_DIR / "index.db"))
MODELS_DIR = DATA_DIR / "models"
LLAMA_SERVER = DATA_DIR / "llama/bin/llama-server"
TEXT_MODEL = MODELS_DIR / "embeddinggemma-2-Q8_0.gguf"
MEDIA_MODEL = MODELS_DIR / "mmproj-embeddinggemma-2-Q8_0.gguf"
THUMBS_DIR = CACHE_DIR / "thumbs"
SOCKET = RUNTIME_DIR / "sift.sock"
STATUS_FILE = RUNTIME_DIR / "status.json"
PAUSE_FILE = STATE_DIR / "paused"


def make_private() -> None:
    """What Sift keeps holds what's in your files (passages, page renders, face crops): only you may read it, whatever
    the umask. New files come out 0600 and folders 0700; folders from before this get tightened."""
    os.umask(0o077)
    for folder in (DATA_DIR, CACHE_DIR, STATE_DIR):
        if folder.is_dir():
            folder.chmod(0o700)


DEFAULT_CONFIG = """\
# Sift settings. Saved changes are picked up by the indexer within a few seconds.

# Folders to search, indexed in this order.
folders = ["~/Documents", "~/Downloads", "~/Pictures"]

# Of those, folders read without their subfolders (only the files directly in them).
top_level_only = []

# Folder names or paths (with ~) to leave out, on top of the built-in rules.
skip = []

[power]
# Index only when plugged in.
ac_only = true
# CPU threads the model may use: "auto" is 3 next to a GPU (it does most of the work) and every core on the CPU alone.
threads = "auto"
# Which device runs the model: "auto" (the discrete GPU if there is one, else the integrated one, else the CPU),
# "dgpu", "igpu" or "cpu". The CPU alone is about 3x slower.
device = "auto"

[faces]
# Find faces in photos and recognize the people you name (sift faces on / off). Off by default; turning it off
# deletes every faceprint.
enabled = false
# How alike two faces must be to count as the same person (SFace cosine; higher is stricter).
threshold = 0.363
"""


@dataclass
class Power:
    ac_only: bool = True
    threads: int | str = "auto"
    device: str = "auto"


@dataclass
class Faces:
    enabled: bool = False
    threshold: float = 0.363


@dataclass
class Config:
    folders: list[Path] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    shallow: set[Path] = field(default_factory=set)
    power: Power = field(default_factory=Power)
    faces: Faces = field(default_factory=Faces)


def ensure_config() -> None:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(DEFAULT_CONFIG)


def load() -> Config:
    ensure_config()
    raw = tomllib.loads(CONFIG_FILE.read_text())
    power = Power(**{k: v for k, v in raw.get("power", {}).items() if k in Power.__dataclass_fields__})
    folders = [Path(os.path.expanduser(f)).resolve() for f in raw.get("folders", [])]
    shallow = {Path(os.path.expanduser(f)).resolve() for f in raw.get("top_level_only", [])}
    faces = Faces(**{k: v for k, v in raw.get("faces", {}).items() if k in Faces.__dataclass_fields__})
    return Config(folders=[f for f in folders if f.is_dir()], skip=list(raw.get("skip", [])), shallow=shallow,
                  power=power, faces=faces)


def display_path(path: Path) -> str:
    """~/Documents rather than /home/you/Documents, as the settings file writes it."""
    try:
        return "~/" + str(Path(path).relative_to(HOME)) if Path(path) != HOME else "~"
    except ValueError:
        return str(path)


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values) + "]"


def _write_top_level(text: str, key: str, values: list[str]) -> str:
    """Sets a top-level list (folders, top_level_only, skip), keeping the rest of the file as written."""
    line = f"{key} = {_toml_list(values)}"
    pattern = re.compile(rf"^{key}\s*=\s*\[[^\]]*\]", re.M)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    section = re.search(r"^\[", text, re.M)
    at = section.start() if section else len(text)
    return text[:at] + line + "\n\n" + text[at:]


def set_value(section: str, key: str, value: str) -> None:
    """Sets key = value (already TOML) in [section], keeping the rest of the settings file as written."""
    ensure_config()
    text = CONFIG_FILE.read_text()
    found = re.search(rf"^\[{re.escape(section)}\][^\[]*", text, re.M)
    if found:
        body = found.group(0)
        if re.search(rf"^{key}\s*=", body, re.M):
            body = re.sub(rf"^{key}\s*=.*$", lambda _: f"{key} = {value}", body, count=1, flags=re.M)
        else:
            body = body.replace(f"[{section}]\n", f"[{section}]\n{key} = {value}\n", 1)
        text = text[:found.start()] + body + text[found.end():]
    else:
        text = text.rstrip("\n") + f"\n\n[{section}]\n{key} = {value}\n"
    CONFIG_FILE.write_text(text)


def set_faces_enabled(enabled: bool) -> None:
    set_value("faces", "enabled", "true" if enabled else "false")


# ---------------------------------------------------------------- folders

def _raw() -> dict:
    ensure_config()
    return tomllib.loads(CONFIG_FILE.read_text())


def _resolve(entry: str) -> Path:
    return Path(os.path.expanduser(entry)).resolve()


def folder_list() -> list[dict]:
    """The folders as configured, in order: {"path", "label", "subfolders", "exists"}."""
    raw = _raw()
    shallow = {_resolve(f) for f in raw.get("top_level_only", [])}
    out = []
    for entry in raw.get("folders", []):
        path = _resolve(entry)
        out.append({"path": str(path), "label": display_path(path), "subfolders": path not in shallow,
                    "exists": path.is_dir()})
    return out


def covered_by(path: Path) -> Path | None:
    """The configured folder that already reads this one, subfolders and all, if any."""
    path = Path(path).resolve()
    for folder in folder_list():
        root = Path(folder["path"])
        if folder["subfolders"] and (root == path or root in path.parents):
            return root
    return None


def _save_folders(folders: list[Path], shallow: set[Path]) -> None:
    text = CONFIG_FILE.read_text()
    text = _write_top_level(text, "folders", [display_path(f) for f in folders])
    text = _write_top_level(text, "top_level_only", [display_path(f) for f in folders if f in shallow])
    CONFIG_FILE.write_text(text)


def _current() -> tuple[list[Path], set[Path]]:
    raw = _raw()
    folders = [_resolve(f) for f in raw.get("folders", [])]
    shallow = {_resolve(f) for f in raw.get("top_level_only", [])}
    return folders, shallow


def add_folder(path: Path, subfolders: bool = True) -> None:
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"not a folder: {path}")
    folders, shallow = _current()
    if path not in folders:
        folders.append(path)
    if subfolders:
        shallow.discard(path)
    else:
        shallow.add(path)
    _save_folders(folders, shallow)


def remove_folder(path: Path) -> bool:
    path = Path(path).expanduser().resolve()
    folders, shallow = _current()
    if path not in folders:
        return False
    folders.remove(path)
    shallow.discard(path)
    _save_folders(folders, shallow)
    return True


def set_subfolders(path: Path, subfolders: bool) -> None:
    add_folder(path, subfolders)
