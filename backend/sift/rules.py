"""What gets read, and as what."""

import os
from pathlib import Path

# Folders never worth walking into (hidden folders are skipped too).
SKIPPED_FOLDERS = {
    "node_modules", "bower_components", "venv", ".venv", "site-packages", "__pycache__", "DerivedData", "Pods",
    "Carthage", "target", "build", "dist", ".next", ".nuxt", ".dart_tool", ".gradle", "vendor", "coverage",
}

# Any of these in a folder makes it a code project, where only screenshots are read.
PROJECT_MARKERS = (
    ".git", "package.json", "pyproject.toml", "setup.py", "requirements.txt", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "CMakeLists.txt", "pubspec.yaml", "Package.swift",
)

DOC_TYPES = {"txt", "text", "md", "markdown", "rst", "org", "rtf", "docx", "odt", "html", "htm"}
PDF_TYPES = {"pdf"}
IMAGE_TYPES = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}

SCREENSHOT_PREFIXES = ("screenshot", "screen shot", "cleanshot", "scr-", "grim", "satty", "flameshot")

SECRET_TYPES = {"pem", "key", "p12", "pfx", "keystore", "jks", "ppk", "kdbx", "gpg", "asc", "crt", "cer", "der", "p8"}
SECRET_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".env", "secret", "credential", ".netrc", ".npmrc",
                   ".pypirc", "htpasswd", ".htpasswd")

MAX_BYTES = {"doc": 20 << 20, "pdf": 500 << 20, "image": 60 << 20, "screenshot": 60 << 20}


def is_secret(name: str) -> bool:
    lower = name.lower()
    return lower.rsplit(".", 1)[-1] in SECRET_TYPES or lower.startswith(SECRET_PREFIXES)


def is_screenshot(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() == ".png" and (name.startswith(SCREENSHOT_PREFIXES) or "screenshot" in path.parent.name.lower())


def kind_of(path: Path, in_project: bool) -> str | None:
    """'doc', 'pdf', 'image', 'screenshot', or None for a file Sift doesn't read."""
    if is_secret(path.name):
        return None
    ext = path.suffix.lower().lstrip(".")
    if is_screenshot(path):
        return "screenshot"
    if in_project:
        return None
    if ext in DOC_TYPES:
        return "doc"
    if ext in PDF_TYPES:
        return "pdf"
    if ext in IMAGE_TYPES:
        return "image"
    return None


def is_project(folder: Path, names: set[str]) -> bool:
    return any(marker in names for marker in PROJECT_MARKERS)


def walk(root: Path, skip: list[str], recursive: bool = True):
    """Yields (path, kind, stat) for every file under root Sift reads (directly in root, when not recursive)."""
    skip_names = {s for s in skip if "/" not in s}
    skip_paths = {Path(os.path.expanduser(s)).resolve() for s in skip if "/" in s}
    stack = [(root, False)]
    while stack:
        folder, in_project = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        names = {e.name for e in entries}
        in_project = in_project or is_project(folder, names)
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    path = Path(entry.path)
                    if not recursive or entry.name in SKIPPED_FOLDERS or entry.name in skip_names \
                            or path in skip_paths:
                        continue
                    stack.append((path, in_project))
                elif entry.is_file(follow_symlinks=False):
                    path = Path(entry.path)
                    kind = kind_of(path, in_project)
                    if kind is None:
                        continue
                    stat = entry.stat()
                    if stat.st_size == 0 or stat.st_size > MAX_BYTES[kind]:
                        continue
                    yield path, kind, stat
            except OSError:
                continue


def project_root_of(path: Path, roots: list[Path]) -> bool:
    """Whether path sits inside a code project under one of roots (for files the watcher reports one at a time)."""
    for root in roots:
        if root in path.parents:
            folder = path.parent
            while True:
                try:
                    if is_project(folder, set(os.listdir(folder))):
                        return True
                except OSError:
                    return False
                if folder == root:
                    return False
                folder = folder.parent
    return False


def is_skipped_path(path: Path, roots: list[Path], skip: list[str], shallow: set[Path] = frozenset()) -> bool:
    """Whether a path the watcher reports is under a skipped or hidden folder, or below a top-level-only one."""
    skip_names = {s for s in skip if "/" not in s}
    skip_paths = {Path(os.path.expanduser(s)).resolve() for s in skip if "/" in s}
    # The most specific folder holding it: ~/Documents/me (whole) inside ~/Documents (top level only).
    root = max((r for r in roots if r in path.parents), key=lambda r: len(r.parts), default=None)
    if root is None or (root in shallow and path.parent != root):
        return True
    for part in path.relative_to(root).parts[:-1]:
        if part.startswith(".") or part in SKIPPED_FOLDERS or part in skip_names:
            return True
    return any(p in skip_paths for p in path.parents)
