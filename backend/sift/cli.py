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
    elif args.command == "config":
        config.ensure_config()
        os.execvp(os.environ.get("EDITOR", "nvim"), [os.environ.get("EDITOR", "nvim"), str(config.CONFIG_FILE)])
