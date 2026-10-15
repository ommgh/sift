"""The folders Sift reads, as the panel's Folders view and `sift folders` show them: each with how many of its files
are read, and a browser for picking more."""

import os
from pathlib import Path

from . import config, rules

# Offered on first setup and at the top of the browser, when they exist.
COMMON = ["~/Documents", "~/Downloads", "~/Pictures", "~/Desktop", "~/Screenshots", "~/Pictures/Screenshots",
          "~/Notes", "~/Dropbox", "~/Nextcloud", "~/Sync"]


def overview(db) -> list[dict]:
    """The configured folders, in order, each with its counts. A file counts toward the most specific folder holding
    it (~/Documents/papers read whole, inside ~/Documents read top level only)."""
    folders = config.folder_list()
    roots = sorted(((Path(f["path"]), f) for f in folders), key=lambda rf: len(rf[0].parts), reverse=True)
    for f in folders:
        f.update(files=0, done=0, errors=0)
    for path, status in db.execute("SELECT path, status FROM files"):
        for root, f in roots:
            if path.startswith(str(root) + "/"):
                f["files"] += 1
                f["done"] += status != "pending"
                f["errors"] += status == "error"
                break
    return folders


def suggestions() -> list[dict]:
    """Common folders that exist and aren't read yet."""
    out = []
    for entry in COMMON:
        path = Path(os.path.expanduser(entry)).resolve()
        if path.is_dir() and config.covered_by(path) is None and \
                all(Path(f["path"]) != path for f in config.folder_list()):
            out.append({"path": str(path), "label": config.display_path(path)})
    return out


def browse(path: str | None) -> dict:
    """A folder's subfolders, for picking one to add: hidden and build folders are left out, like the indexer does."""
    folder = Path(os.path.expanduser(path or "~")).resolve()
    if not folder.is_dir():
        folder = Path.home()
    configured = {Path(f["path"]): f for f in config.folder_list()}
    raw_skip = config.load().skip
    skip_names = {s for s in raw_skip if "/" not in s}
    skip_paths = {Path(os.path.expanduser(s)).resolve() for s in raw_skip if "/" in s}
    dirs = []
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError:
        entries = []
    for entry in entries:
        try:
            if entry.name.startswith(".") or entry.name in rules.SKIPPED_FOLDERS or not entry.is_dir():
                continue
        except OSError:
            continue
        p = Path(entry.path).resolve()
        if entry.name in skip_names or p in skip_paths:
            continue
        covering = config.covered_by(p)
        dirs.append({"name": entry.name, "path": str(p), "added": p in configured,
                     "covered": covering is not None and covering != p})
    return {"path": str(folder), "label": config.display_path(folder),
            "parent": str(folder.parent) if folder != folder.parent else None, "dirs": dirs,
            "added": folder in configured, "covered": config.covered_by(folder) is not None}


def count_files(path: Path, subfolders: bool = True, limit: int = 200_000) -> int:
    """How many files Sift would read in a folder (for setup's "Documents (1,204 files)")."""
    n = 0
    for _ in rules.walk(path, [], subfolders):
        n += 1
        if n >= limit:
            break
    return n
