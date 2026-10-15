"""Whether it's a good time to index: plugged in, not too hot, not paused."""

from pathlib import Path

from . import config


def _hwmon(name: str) -> Path | None:
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hwmon / "name").read_text().strip() == name:
                return hwmon
        except OSError:
            continue
    return None


def cpu_temp() -> float | None:
    """The CPU's control temperature (°C): k10temp's Tctl on AMD, coretemp's package on Intel."""
    for name in ("k10temp", "coretemp", "zenpower"):
        hwmon = _hwmon(name)
        if hwmon:
            try:
                return int((hwmon / "temp1_input").read_text()) / 1000
            except (OSError, ValueError):
                return None
    return None


def gpu_temps() -> list[float]:
    temps = []
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hwmon / "name").read_text().strip() == "amdgpu":
                temps.append(int((hwmon / "temp1_input").read_text()) / 1000)
        except (OSError, ValueError):
            continue
    return temps


def hottest() -> float | None:
    temps = [t for t in [cpu_temp(), *gpu_temps()] if t is not None]
    return max(temps) if temps else None


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
