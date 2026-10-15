"""Whether it's a good time to index: plugged in, not paused."""

from pathlib import Path

from . import config


def on_ac() -> bool:
    supplies = list(Path("/sys/class/power_supply").glob("*"))
    mains = [s for s in supplies if (s / "type").exists() and (s / "type").read_text().strip() == "Mains"]
    if not mains:
        return True   # a desktop
    return any((s / "online").read_text().strip() == "1" for s in mains)


def paused() -> bool:
    return config.PAUSE_FILE.exists()


def set_paused(value: bool) -> None:
    if value:
        config.PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.PAUSE_FILE.touch()
    else:
        config.PAUSE_FILE.unlink(missing_ok=True)
