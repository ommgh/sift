"""Every test runs against throwaway folders: Sift's paths come from the environment when its modules are imported,
so they're pointed at a temporary home before anything imports sift."""

import os
import tempfile
from pathlib import Path

_home = Path(tempfile.mkdtemp(prefix="sift-tests-"))
for variable, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state"),
                         ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "run")):
    (_home / folder).mkdir(mode=0o700)
    os.environ[variable] = str(_home / folder)
os.environ["SIFT_BINDINGS"] = str(_home / "bindings.lua")
os.environ.pop("SIFT_DATA", None)
os.environ.pop("SIFT_INDEX", None)
