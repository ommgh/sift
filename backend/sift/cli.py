"""sift: the command line. `sift --help` lists the commands."""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

from . import config, power


def request(payload: dict, timeout: float = 60) -> dict:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(str(config.SOCKET))
        s.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def cmd_search(args) -> None:
    query = " ".join(args.query)
    try:
        reply = request({"op": "search", "q": query, "limit": args.limit})
    except OSError:
        sys.exit("the search daemon isn't running: systemctl --user start sift")
    if not reply.get("ok"):
        sys.exit(reply.get("error"))
    if args.json:
        print(json.dumps(reply["results"], indent=2))
        return
    for hit in reply["results"]:
        where = f"  p.{hit['page']}" if hit.get("page") else ""
        print(f"{hit['score']:6.2f}  {hit['kind']:10}  {hit['folder']}/{hit['name']}{where}")
        if hit.get("snippet"):
            print(f"{'':20}{hit['snippet'][:110]}")
    print(f"({reply['ms']} ms)")


def cmd_status(args) -> None:
    try:
        status = json.loads(config.STATUS_FILE.read_text())
    except (OSError, ValueError):
        status = {"state": "stopped"}
    if args.json:
        print(json.dumps(status))
        return
    line = f"{status.get('state')}: {status.get('done', 0)} of {status.get('total', 0)} files"
    if status.get("errors"):
        line += f", {status['errors']} unreadable"
    if status.get("eta"):
        line += f", ~{round(status['eta'] / 60)} min left"
    print(line)
    if status.get("current"):
        print(f"  reading {status['current']}")


def pdf_opener(page: int, path: str) -> list[str] | None:
    """A command that opens the default PDF viewer at the page, when it's one that can be told a page."""
    try:
        desktop = subprocess.run(["xdg-mime", "query", "default", "application/pdf"], capture_output=True,
                                 text=True, timeout=5).stdout.strip().lower()
    except (OSError, subprocess.TimeoutExpired):
        return None
    url = Path(path).resolve().as_uri() + f"#page={page}"
    for browser in ("chromium", "google-chrome", "brave", "firefox", "zen", "helium"):
        if browser in desktop:
            binary = shutil.which(browser) or shutil.which(f"{browser}-browser")
            return [binary, url] if binary else None
    if "evince" in desktop:
        return ["evince", "-i", str(page), path]
    if "okular" in desktop:
        return ["okular", "-p", str(page), path]
    if "zathura" in desktop:
        return ["zathura", "-P", str(page), path]
    return None


def launch(command: list[str]) -> None:
    if shutil.which("uwsm-app"):
        command = ["uwsm-app", "--", *command]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def cmd_open(args) -> None:
    command = pdf_opener(args.page, args.path) if args.page and args.path.lower().endswith(".pdf") else None
    launch(command or ["xdg-open", args.path])


def cmd_reveal(args) -> None:
    if shutil.which("nautilus"):
        launch(["nautilus", "--select", args.path])
    else:
        launch(["xdg-open", str(Path(args.path).parent)])


def cmd_faces(args) -> None:
    from . import faces, store

    cfg = config.load()
    db = store.connect()
    if args.action == "on":
        faces.ensure_models()
        config.set_faces_enabled(True)
        print("Faces are on. Sift will look for faces in your photos after it's done reading them.")
        print("Then name people: sift person add NAME photo.jpg …  (or: sift faces groups, then --group N)")
    elif args.action == "off":
        if not args.yes and sys.stdin.isatty():
            if input("Turn faces off and delete every faceprint and named person? [y/N] ").strip().lower() != "y":
                return
        config.set_faces_enabled(False)
        faces.wipe(db)
        print("Faces are off; all face data is deleted.")
    elif args.action == "status":
        scanned, photos = db.execute("SELECT coalesce(sum(faces_done), 0), count(*) FROM files "
                                     "WHERE kind = 'image' AND status = 'done'").fetchone()
        found = db.execute("SELECT count(*) FROM faces").fetchone()[0]
        named = db.execute("SELECT count(*) FROM people").fetchone()[0]
        print(f"faces {'on' if cfg.faces.enabled else 'off'}: {scanned} of {photos} photos looked at, "
              f"{found} faces found, {named} people named")
    elif args.action == "groups":
        if not cfg.faces.enabled:
            sys.exit("faces are off: sift faces on")
        found = faces.groups(db, cfg.faces.threshold)
        if not found:
            print("No groups of unnamed faces yet.")
            return
        out = config.CACHE_DIR / "groups"
        sheets = []
        for number, ids in enumerate(found[:args.limit], start=1):
            photos = db.execute(f"SELECT count(DISTINCT file) FROM faces WHERE id IN ({','.join(map(str, ids))})").fetchone()[0]
            sheet = faces.contact_sheet(db, ids, out / f"{number}.jpg")
            sheets.append(str(sheet))
            print(f"{number:3}  {len(ids):4} faces in {photos:4} photos   {sheet}")
        print("\nName one: sift person add NAME --group N")
        if args.open:
            launch(["xdg-open", sheets[0]] if len(sheets) == 1 else ["imv", *sheets] if shutil.which("imv")
                   else ["xdg-open", sheets[0]])


def cmd_person(args) -> None:
    from . import faces, store

    cfg = config.load()
    db = store.connect()
    if args.action == "list":
        index = faces.FaceIndex(db)
        if not index.people:
            print("Nobody named yet: sift person add NAME photo.jpg …")
        for name, refs in sorted(index.people.items()):
            print(f"{name:20} {len(refs):3} reference faces, in {len(index.matches(name, cfg.faces.threshold)):4} photos")
        return
    if args.action == "remove":
        print("removed" if faces.remove_person(db, args.name) else f"nobody called {args.name}")
        return
    if not cfg.faces.enabled:
        sys.exit("faces are off: sift faces on")
    vectors = []
    for number in args.group or []:
        group = faces.group_vectors(db, number)
        if not group:
            sys.exit(f"no group {number}: run sift faces groups first")
        vectors += group
    if args.photos:
        faces.ensure_models()
        reader = faces.FaceReader()
        for photo in args.photos:
            found = reader.read(Path(photo).expanduser())
            if not found:
                print(f"no face found in {photo}")
                continue
            biggest = max(found, key=lambda f: f["box"][2] * f["box"][3])
            if len(found) > 1:
                print(f"{photo}: {len(found)} faces, using the biggest")
            vectors.append(biggest["vector"])
    if not vectors:
        sys.exit("nothing to learn from: give photos of them, or --group N")
    faces.add_person(db, args.name, vectors)
    matched = len(faces.FaceIndex(db).matches(args.name, cfg.faces.threshold))
    print(f"{args.name}: {len(vectors)} reference faces, found in {matched} photos. Search: \"{args.name} at the beach\"")


def cmd_folders(args) -> None:
    from . import folders, store

    if args.action == "add":
        for path in args.paths:
            covering = config.covered_by(Path(path).expanduser())
            if covering and covering != Path(path).expanduser().resolve() and not args.top_level:
                print(f"{path}: already read as part of {config.display_path(covering)}")
                continue
            config.add_folder(Path(path), subfolders=not args.top_level)
    elif args.action == "remove":
        for path in args.paths:
            if not config.remove_folder(Path(path)):
                print(f"{path} isn't one of Sift's folders")
    elif args.action in ("subfolders", "top-level"):
        for path in args.paths:
            config.set_subfolders(Path(path), args.action == "subfolders")
    elif args.action == "suggest":
        for f in folders.suggestions():
            print(f["label"])
        return
    for f in folders.overview(store.connect()):
        scope = "with subfolders" if f["subfolders"] else "top level only"
        state = "missing" if not f["exists"] else f"{f['done']:,} of {f['files']:,} files read"
        print(f"{f['label']:32} {scope:16} {state}")
    if args.action != "list":
        print("The indexer picks this up within a few seconds.")


def cmd_shortcut(args) -> None:
    from . import shortcut

    if args.action == "set":
        try:
            replaced = shortcut.bind(" ".join(args.combo))
        except (ValueError, RuntimeError) as e:
            sys.exit(str(e))
        combo, _ = shortcut.current()
        print(f"{combo} opens Sift" + (f" (it was “{replaced}”; sift shortcut off gives it back)" if replaced else ""))
    elif args.action == "off":
        print("removed" if shortcut.remove() else "no shortcut written by sift (a binding you wrote stays as it is)")
    elif args.action == "refresh":   # install.sh: rewrite Sift's block with the current command
        combo, managed = shortcut.current()
        if combo and managed:
            shortcut.bind(combo)
    elif args.action == "pick":
        from . import setup
        setup.run("shortcut")
    else:
        combo, managed = shortcut.current()
        if not combo:
            print("No shortcut. Set one: sift shortcut set \"SUPER + S\" (or sift shortcut pick)")
        else:
            print(f"{combo}" + ("" if managed else f"  (your own line in {shortcut.BINDINGS})"))


def cmd_pause(args) -> None:
    if args.command == "toggle":
        power.set_paused(not power.paused())
    else:
        power.set_paused(args.command == "pause")
    print("paused" if power.paused() else "running")


def main() -> None:
    parser = argparse.ArgumentParser(prog="sift", description="Search your files by what's in them.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("index", help="run the indexer (the sift-indexer service)")
    sub.add_parser("serve", help="run the search daemon (the sift service)")
    p = sub.add_parser("search", help="search from the terminal")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("status", help="how indexing is going")
    p.add_argument("--json", action="store_true")
    for name in ("pause", "resume", "toggle"):
        sub.add_parser(name, help=f"{name} indexing")
    p = sub.add_parser("open", help="open a result (a PDF at its page)")
    p.add_argument("path")
    p.add_argument("--page", type=int)
    p = sub.add_parser("reveal", help="show a file in the file manager")
    p.add_argument("path")
    sub.add_parser("config", help="open the settings file in $EDITOR")
    p = sub.add_parser("faces", help="find and recognize faces in photos (opt-in)")
    p.add_argument("action", choices=["on", "off", "status", "groups"])
    p.add_argument("--yes", action="store_true", help="off: don't ask before deleting face data")
    p.add_argument("--limit", type=int, default=12, help="groups: how many to show")
    p.add_argument("--open", action="store_true", help="groups: open the face sheets")
    p = sub.add_parser("folders", help="the folders Sift reads: list, add, remove")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "remove", "subfolders", "top-level", "suggest"])
    p.add_argument("paths", nargs="*")
    p.add_argument("--top-level", action="store_true", help="add: only the files directly in it")
    p = sub.add_parser("shortcut", help="the key that opens Sift: show, set, pick, off")
    p.add_argument("action", nargs="?", default="show", choices=["show", "set", "pick", "off", "refresh"])
    p.add_argument("combo", nargs="*", help='set: e.g. "SUPER + S"')
    p = sub.add_parser("setup", help="the first-run questions again: folders, shortcut, people")
    p.add_argument("only", nargs="?", choices=["folders", "shortcut", "people"])
    p = sub.add_parser("doctor", help="what Sift runs on, and what's missing")
    p.add_argument("--bench", action="store_true", help="also time the model on this machine")
    p = sub.add_parser("person", help="name the people Sift should recognize")
    person = p.add_subparsers(dest="action", required=True)
    q = person.add_parser("add", help="teach Sift a face: photos of them, or a group from sift faces groups")
    q.add_argument("name")
    q.add_argument("photos", nargs="*")
    q.add_argument("--group", type=int, action="append", help="a group number (repeatable)")
    person.add_parser("list", help="the people Sift knows")
    q = person.add_parser("remove", help="forget a person")
    q.add_argument("name")
    args = parser.parse_args()
    # SIGTERM (systemctl stop) runs atexit handlers, which stop the llama-server children.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if args.command == "index":
        from . import indexer
        indexer.main()
    elif args.command == "serve":
        from . import daemon
        daemon.main()
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command in ("pause", "resume", "toggle"):
        cmd_pause(args)
    elif args.command == "open":
        cmd_open(args)
    elif args.command == "reveal":
        cmd_reveal(args)
    elif args.command == "faces":
        cmd_faces(args)
    elif args.command == "person":
        cmd_person(args)
    elif args.command == "folders":
        cmd_folders(args)
    elif args.command == "shortcut":
        cmd_shortcut(args)
    elif args.command == "setup":
        from . import setup
        setup.run(args.only)
    elif args.command == "doctor":
        from . import setup
        setup.doctor(args.bench)
    elif args.command == "config":
        config.ensure_config()
        os.execvp(os.environ.get("EDITOR", "nvim"), [os.environ.get("EDITOR", "nvim"), str(config.CONFIG_FILE)])
