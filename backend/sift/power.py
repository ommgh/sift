"""Whether it's a good time to index: plugged in, not paused."""

from pathlib import Path

from . import config


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def on_ac() -> bool:
    mains = [s for s in Path("/sys/class/power_supply").glob("*") if _read(s / "type") == "Mains"]
    if not mains:
        return True   # a desktop
    return any(_read(s / "online") == "1" for s in mains)


def paused() -> bool:
    return config.PAUSE_FILE.exists()


def set_paused(value: bool) -> None:
    if value:
        config.PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.PAUSE_FILE.touch()
    else:
        config.PAUSE_FILE.unlink(missing_ok=True)
