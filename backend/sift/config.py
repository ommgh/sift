"""Paths and settings. Settings live in ~/.config/sift/config.toml; everything has a default."""

import os
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

DEFAULT_CONFIG = """\
# Sift settings. Saved changes are picked up by the indexer within a few seconds.

# Folders to search, indexed in this order.
folders = ["~/Documents", "~/Downloads", "~/Pictures"]

# Folder names or paths (with ~) to leave out, on top of the built-in rules.
skip = []

[power]
# Index only when plugged in.
ac_only = true
# Pause indexing when the CPU gets this hot (°C), resume once it's back under resume_temp.
max_temp = 80
resume_temp = 68
# CPU threads the model may use (the GPU does most of the work).
threads = 3
# Which device runs the model: "auto" (the discrete GPU if there is one), "dgpu", "igpu" or "cpu".
# The CPU alone is about 3x slower and runs much hotter.
device = "auto"
# Rest between files for this share of the time spent working (0.3: 30%), so heat doesn't build up.
pace = 0.3
"""


@dataclass
class Power:
    ac_only: bool = True
    max_temp: float = 80
    resume_temp: float = 68
    threads: int = 3
    device: str = "auto"
    pace: float = 0.3


@dataclass
class Config:
    folders: list[Path] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    power: Power = field(default_factory=Power)


def ensure_config() -> None:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(DEFAULT_CONFIG)


def load() -> Config:
    ensure_config()
    raw = tomllib.loads(CONFIG_FILE.read_text())
    power = Power(**{k: v for k, v in raw.get("power", {}).items() if k in Power.__dataclass_fields__})
    folders = [Path(os.path.expanduser(f)).resolve() for f in raw.get("folders", [])]
    return Config(folders=[f for f in folders if f.is_dir()], skip=list(raw.get("skip", [])), power=power)
