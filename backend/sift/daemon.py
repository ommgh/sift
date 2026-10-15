"""The search daemon: answers the panel over a Unix socket, one JSON object per line.

Requests: {"id": 1, "op": "search", "q": "...", "limit": 30} · {"op": "warm"} · {"op": "status"} · {"op": "ping"}
Replies carry the request's id.

Queries are embedded by the text model alone (310 MB), on the CPU: a query is a dozen tokens (~20 ms), so searching
never wakes the discrete GPU. The model and the vectors are only held while searching: both are dropped after
IDLE_RELEASE seconds without a query."""

import hashlib
import json
import logging
import os
import socketserver
import threading
import time
from pathlib import Path

from . import config, extract, store
from .llama import LlamaServer
from .search import Searcher

log = logging.getLogger("sift.daemon")

IDLE_RELEASE = 10 * 60
THUMB_WIDTH = 480


class Engine:
    def __init__(self):
        self.db = store.connect()
        self.searcher = Searcher(self.db)
        self.server = LlamaServer(media=False, device=None, threads=2, name="query")
        self.lock = threading.Lock()
        self.last_used = 0.0

    def warm(self) -> None:
        with self.lock:
            self.last_used = time.time()
            self.server.start()
            self.searcher.refresh()

    def search(self, q: str, limit: int) -> list:
        with self.lock:
            self.last_used = time.time()
            q = q.strip()
            if not q:
                return []
            vector = self.server.embed_texts([extract.query_prompt(q)])[0]
            hits = self.searcher.search(q, vector, limit=limit)
        for hit in hits[:12]:
            if hit["kind"] == "pdf" and hit["page"]:
                hit["thumb"] = pdf_thumb(hit["path"], hit["page"], hit["mtime"])
        return hits

    def release_when_idle(self) -> None:
        while True:
            time.sleep(30)
            with self.lock:
                if self.last_used and time.time() - self.last_used > IDLE_RELEASE:
                    self.server.stop()
                    self.searcher.release()
                    self.last_used = 0.0


def pdf_thumb(path: str, page: int, mtime: float) -> str | None:
    """A PNG of the page a PDF hit points at, cached by path, page and mtime."""
    key = hashlib.sha1(f"{path}\0{page}\0{mtime}".encode()).hexdigest()
    out = config.THUMBS_DIR / f"{key}.png"
    if out.exists():
        return str(out)
    try:
        import pymupdf

        with pymupdf.open(path) as pdf:
            p = pdf[page - 1]
            zoom = THUMB_WIDTH / max(p.rect.width, 1)
            pix = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            config.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
            pix.save(str(out))
        return str(out)
    except Exception:
        return None


def read_status() -> dict:
    try:
        return json.loads(config.STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"state": "stopped"}


class Handler(socketserver.StreamRequestHandler):
    engine: Engine

    def handle(self):
        for line in self.rfile:
            try:
                request = json.loads(line)
            except ValueError:
                continue
            reply = {"id": request.get("id")}
            started = time.time()
            try:
                op = request.get("op")
                if op == "search":
                    reply["results"] = self.engine.search(request.get("q", ""), int(request.get("limit", 30)))
                    reply["q"] = request.get("q", "")
                elif op == "warm":
                    self.engine.warm()
                elif op == "status":
                    reply["status"] = read_status()
                elif op != "ping":
                    raise ValueError(f"unknown op {op!r}")
                reply["ok"] = True
            except Exception as e:
                log.exception("request failed")
                reply.update(ok=False, error=str(e)[:300])
            reply["ms"] = round((time.time() - started) * 1000)
            try:
                self.wfile.write((json.dumps(reply) + "\n").encode())
                self.wfile.flush()
            except OSError:
                return


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    config.SOCKET.unlink(missing_ok=True)
    Handler.engine = Engine()
    threading.Thread(target=Handler.engine.release_when_idle, daemon=True).start()
    with Server(str(config.SOCKET), Handler) as server:
        os.chmod(config.SOCKET, 0o600)
        log.info("listening on %s", config.SOCKET)
        server.serve_forever()
