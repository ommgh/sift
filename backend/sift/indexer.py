"""The indexer: walks the chosen folders, reads new and changed files into the index, and watches for more.

It runs as its own systemd user service (sift-indexer) at idle CPU and disk priority, so it only uses what the rest
of the desktop leaves free. It waits while on battery (ac_only) and while paused. The model runs in a llama-server
child that's stopped whenever there's nothing to read."""

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from stat import S_ISREG

from inotify_simple import INotify, flags as F

from . import config, extract, faces, power, rules, store
from .llama import LlamaError, LlamaServer, pick_device, threads_for

log = logging.getLogger("sift.indexer")

TEXT_BATCH = 8
RESCAN_EVERY = 30 * 60
IDLE_STOP = 60          # stop llama-server after this long with nothing to do
KIND_ORDER = {"screenshot": 0, "doc": 1, "pdf": 2, "image": 3}


class Status:
    """What the bar widget and panel show, written atomically to $XDG_RUNTIME_DIR/sift/status.json."""

    def __init__(self):
        self.data = {"state": "starting", "done": 0, "total": 0, "errors": 0, "current": "", "rate": 0.0,
                     "eta": None}
        self.recent: list[float] = []

    def update(self, **values) -> None:
        self.data.update(values)
        self.data["updated"] = time.time()
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(config.STATUS_FILE)

    def finished_one(self) -> None:
        now = time.time()
        self.recent = [t for t in self.recent if now - t < 300] + [now]


class Watcher(threading.Thread):
    """inotify on every folder Sift walks; reports changed and removed file paths."""

    MASK = F.CLOSE_WRITE | F.MOVED_TO | F.MOVED_FROM | F.DELETE | F.CREATE | F.DELETE_SELF

    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True, name="watcher")
        self.events = events
        self.inotify = INotify()
        self.watches: dict[int, Path] = {}
        self.lock = threading.Lock()

    def watch_tree(self, root: Path, skip: list[str], recursive: bool = True) -> None:
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                with self.lock:
                    wd = self.inotify.add_watch(folder, self.MASK)
                    self.watches[wd] = folder
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries if recursive else []:
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".") \
                        and entry.name not in rules.SKIPPED_FOLDERS and entry.name not in skip:
                    stack.append(Path(entry.path))

    def run(self) -> None:
        while True:
            for event in self.inotify.read():
                with self.lock:
                    folder = self.watches.get(event.wd)
                if folder is None:
                    continue
                if event.mask & F.IGNORED:
                    with self.lock:
                        self.watches.pop(event.wd, None)
                    continue
                path = folder / event.name if event.name else folder
                if event.mask & F.ISDIR:
                    if event.mask & (F.CREATE | F.MOVED_TO):
                        self.events.put(("dir", path))
                    elif event.mask & (F.DELETE | F.MOVED_FROM):
                        self.events.put(("gone-dir", path))
                elif event.mask & (F.CLOSE_WRITE | F.MOVED_TO):
                    self.events.put(("file", path))
                elif event.mask & (F.DELETE | F.MOVED_FROM):
                    self.events.put(("gone", path))


class Indexer:
    def __init__(self):
        self.cfg = config.load()
        self.cfg_mtime = config.CONFIG_FILE.stat().st_mtime
        self.db = store.connect()
        self.status = Status()
        device = pick_device(self.cfg.power.device)
        self.server = LlamaServer(media=True, device=device, threads=threads_for(self.cfg.power.threads, device),
                                  name="indexer")
        log.info("model runs on %s, %d threads", device or "the CPU", self.server.threads)
        self.events: queue.Queue = queue.Queue()
        self.watcher = Watcher(self.events)
        self.last_scan = 0.0
        self.dirty = False      # files read since the search daemon was last told
        self.face_reader = None

    # ------------------------------------------------------------ files on disk

    def scan(self) -> None:
        """Brings the files table in line with the folders: new and changed files become pending, gone ones go."""
        self.status.update(state="scanning")
        seen: set[str] = set()
        self.db.execute("BEGIN")
        for root in self.cfg.folders:
            for path, kind, stat in rules.walk(root, self.cfg.skip, root not in self.cfg.shallow):
                seen.add(str(path))
                store.upsert_file(self.db, path, kind, stat.st_mtime, stat.st_size)
        for (path,) in self.db.execute("SELECT path FROM files").fetchall():
            if path not in seen:
                store.remove_file(self.db, path)
        store.bump_generation(self.db)
        self.db.execute("COMMIT")
        self.last_scan = time.time()

    def handle_events(self) -> None:
        changed = False
        while True:
            try:
                what, path = self.events.get_nowait()
            except queue.Empty:
                break
            changed = True
            if what == "dir":
                if not rules.is_skipped_path(path / "x", self.cfg.folders, self.cfg.skip, self.cfg.shallow):
                    self.watcher.watch_tree(path, self.cfg.skip)
                    for file, kind, stat in rules.walk(path, self.cfg.skip):
                        store.upsert_file(self.db, file, kind, stat.st_mtime, stat.st_size)
            elif what == "gone-dir":
                prefix = f"{path}/"
                for (p,) in self.db.execute("SELECT path FROM files WHERE substr(path, 1, ?) = ?",
                                            (len(prefix), prefix)).fetchall():
                    store.remove_file(self.db, p)
            elif what == "gone":
                store.remove_file(self.db, str(path))
            elif what == "file":
                if path.name.startswith(".") or rules.is_skipped_path(path, self.cfg.folders, self.cfg.skip, self.cfg.shallow):
                    continue
                kind = rules.kind_of(path, rules.project_root_of(path, self.cfg.folders))
                try:
                    stat = path.lstat()   # a symlink isn't followed, as in rules.walk
                except OSError:
                    continue
                if kind and S_ISREG(stat.st_mode) and 0 < stat.st_size <= rules.MAX_BYTES[kind]:
                    store.upsert_file(self.db, path, kind, stat.st_mtime, stat.st_size)
        if changed:
            store.bump_generation(self.db)

    def next_pending(self):
        """Screenshots first (they're what's looked for soonest), then folder order, newest first."""
        rows = self.db.execute("SELECT id, path, kind, mtime FROM files WHERE status = 'pending'").fetchall()
        if not rows:
            return None

        def folder_rank(path: str) -> int:
            for i, root in enumerate(self.cfg.folders):
                if path.startswith(str(root) + "/"):
                    return i
            return len(self.cfg.folders)

        return min(rows, key=lambda r: (KIND_ORDER.get(r[2], 9), folder_rank(r[1]), -r[3]))

    # ------------------------------------------------------------ reading files

    def embed_texts(self, texts: list[str]):
        vectors = []
        for i in range(0, len(texts), TEXT_BATCH):
            vectors += self.server.embed_texts(texts[i:i + TEXT_BATCH])
        return vectors

    def add_text_segments(self, file_id: int, path: Path, loc_texts: list[tuple[int, str, bool]]) -> None:
        """loc_texts: (loc, text, sets_scale). Text with words gets a vector; the rest is matched by its words only."""
        worded = [(loc, text, scale) for loc, text, scale in loc_texts if extract.has_words(text)]
        vectors = self.embed_texts([extract.document_prompt(path.stem, text) for _, text, _ in worded])
        by_key = {(loc, text): v for (loc, text, _), v in zip(worded, vectors)}
        for loc, text, scale in loc_texts:
            store.add_segment(self.db, file_id, path, loc, "text", text, by_key.get((loc, text)), scale)

    def read_doc(self, file_id: int, path: Path) -> dict:
        pieces = extract.chunks(extract.doc_text(path))
        self.add_text_segments(file_id, path, [(i, text, i < 40) for i, text in enumerate(pieces)])
        if not pieces:
            store.add_segment(self.db, file_id, path, 0, "text", "", None)
        return {"chunks": len(pieces)}

    def read_pdf(self, file_id: int, path: Path) -> dict:
        pending: list[tuple[int, str, bool]] = []
        pages = passages = 0
        for number, text, image in extract.pdf_pages(path):
            pages = number
            scale = number <= extract.SCALE_PAGES
            if image is not None:
                reading = extract.ocr_start(_png(image))
                vector = self.server.embed_image(extract.prepare_image(image, extract.PAGE_BUDGET))
                ocr = extract.ocr_finish(reading)
                store.add_segment(self.db, file_id, path, number, "image", (text.strip() + "\n" + ocr).strip(),
                                  vector, scale)
            elif passages < extract.MAX_CHUNKS:   # the same cap for a whole PDF as for a whole document
                pieces = extract.chunks(text, limit=extract.MAX_CHUNKS - passages)
                passages += len(pieces)
                pending += [(number, piece, scale) for piece in pieces]
            if len(pending) >= TEXT_BATCH * 4:
                self.add_text_segments(file_id, path, pending)
                pending = []
            self.status.update(current=f"{path.name} · page {number}")
        self.add_text_segments(file_id, path, pending)
        return {"pages": pages}

    def read_image(self, file_id: int, path: Path, kind: str) -> dict:
        image = extract.open_image(path, draft=True)
        width, height = image.info["full_size"]
        info = {"width": width, "height": height}
        if min(image.width, image.height) < 64:
            store.add_segment(self.db, file_id, path, 0, "image", None, None)   # an icon: found by name only
            return info
        budget = extract.SCREENSHOT_BUDGET if kind == "screenshot" else extract.PHOTO_BUDGET
        # Text in screenshots and other PNGs (diagrams, designs, scans) is read for exact words; photos skip it.
        # tesseract (CPU) reads while the model (GPU) embeds.
        reading = extract.ocr_start(path) if kind == "screenshot" or path.suffix.lower() == ".png" else None
        vector = self.server.embed_image(extract.prepare_image(image, budget))
        text = extract.ocr_finish(reading)
        store.add_segment(self.db, file_id, path, 0, "image", text or None, vector)
        if extract.has_words(text) and len(text) > 60:
            # The screenshot's words by meaning too ("payment declined error"), next to what it shows.
            self.add_text_segments(file_id, path, [(0, text[:extract.CHUNK_CHARS], True)])
        return info

    def read(self, file_id: int, path: str, kind: str) -> None:
        p = Path(path)
        self.status.update(current=p.name)
        # No transaction around the whole file: reading a long PDF takes minutes, and holding the write lock that
        # long blocks everything else. A file's segments are invisible to search until it's marked done, and a file
        # that's interrupted is still pending, so it's cleared and read again.
        try:
            store.clear_segments(self.db, file_id)
            if kind == "doc":
                info = self.read_doc(file_id, p)
            elif kind == "pdf":
                info = self.read_pdf(file_id, p)
            else:
                info = self.read_image(file_id, p, kind)
            store.finish_file(self.db, file_id, info)
        except LlamaError:
            store.clear_segments(self.db, file_id)
            raise
        except Exception as e:   # a file that can't be read is recorded, not retried until it changes
            store.clear_segments(self.db, file_id)
            log.warning("can't read %s: %s", path, e)
            store.finish_file(self.db, file_id, error=str(e)[:300])

    # ------------------------------------------------------------ the loop

    def reload_config(self) -> None:
        try:
            mtime = config.CONFIG_FILE.stat().st_mtime
        except OSError:
            return
        if mtime != self.cfg_mtime:
            log.info("config changed: rescanning")
            self.cfg_mtime = mtime
            old_device = self.server.device
            try:
                self.cfg = config.load()
            except (OSError, ValueError) as e:   # tomllib.TOMLDecodeError is a ValueError
                log.error("can't read %s, keeping the settings it had: %s", config.CONFIG_FILE, e)
                return
            device = pick_device(self.cfg.power.device)
            threads = threads_for(self.cfg.power.threads, device)
            if device != old_device or threads != self.server.threads:
                self.server.stop()
                self.server = LlamaServer(media=True, device=device, threads=threads, name="indexer")
                log.info("model runs on %s, %d threads", device or "the CPU", threads)
            for root in self.cfg.folders:
                self.watcher.watch_tree(root, self.cfg.skip, root not in self.cfg.shallow)
            self.scan()

    def gate(self) -> str | None:
        """Why not to index right now, or None."""
        if power.paused():
            return "paused"
        if self.cfg.power.ac_only and not power.on_ac():
            return "battery"
        return None

    def counts(self) -> dict:
        c = store.counts(self.db)
        total = sum(c.values())
        return {"total": total, "done": c.get("done", 0) + c.get("error", 0), "errors": c.get("error", 0)}

    def face_pass(self) -> bool:
        """Finds faces in a few photos that haven't been looked at yet (after everything is embedded: the model is
        done, so this is CPU only). False when there's none left."""
        rows = self.db.execute("SELECT id, path FROM files WHERE kind = 'image' AND status = 'done' AND faces_done = 0 "
                               "ORDER BY mtime DESC LIMIT 8").fetchall()
        if not rows:
            if self.face_reader is not None:
                self.face_reader = None   # frees OpenCV's models until there are new photos
                store.bump_generation(self.db)
            return False
        self.server.stop_if_idle(IDLE_STOP)
        if self.face_reader is None:
            faces.ensure_models()
            self.face_reader = faces.FaceReader()
        left, total = self.db.execute("SELECT sum(faces_done = 0), count(*) FROM files "
                                      "WHERE kind = 'image' AND status = 'done'").fetchone()
        self.status.update(state="faces", done=total - left, total=total, current="", eta=None)
        for file_id, path in rows:
            try:
                found = self.face_reader.read(Path(path))
            except Exception as e:
                log.warning("faces in %s: %s", path, e)
                found = []
            self.db.execute("BEGIN")
            faces.add_faces(self.db, file_id, found)
            self.db.execute("COMMIT")
        store.bump_generation(self.db)
        return True

    def run(self) -> None:
        for root in self.cfg.folders:
            self.watcher.watch_tree(root, self.cfg.skip, root not in self.cfg.shallow)
        self.watcher.start()
        self.scan()
        idle_since = time.time()
        while True:
            self.reload_config()
            self.handle_events()
            if time.time() - self.last_scan > RESCAN_EVERY:
                self.scan()
            counts = self.counts()
            reason = self.gate()
            pending = self.next_pending() if reason is None else None
            if reason is None and pending is None and self.cfg.faces.enabled and self.face_pass():
                continue
            if reason or pending is None:
                self.server.stop_if_idle(0 if reason else IDLE_STOP)
                self.status.update(state=reason or "idle", current="", rate=0, eta=None,
                                   **counts)
                if self.dirty:
                    store.bump_generation(self.db)
                    self.dirty = False
                time.sleep(2)
                continue
            file_id, path, kind, _ = pending
            remaining = counts["total"] - counts["done"]
            rate = len(self.status.recent) / max(min(300, time.time() - self.status.recent[0]), 1) * 60 \
                if len(self.status.recent) > 3 else 0
            self.status.update(state="indexing", rate=round(rate, 1),
                               eta=round(remaining / rate * 60) if rate else None, **counts)
            try:
                self.read(file_id, path, kind)
            except LlamaError as e:
                log.error("model: %s", e)
                self.server.stop()
                self.status.update(state="error", current=str(e)[:200])
                time.sleep(30)
                continue
            self.status.finished_one()
            self.dirty = True
            if counts["done"] % 25 == 0:
                store.bump_generation(self.db)
                self.dirty = False


def _png(image) -> bytes:
    import io
    out = io.BytesIO()
    image.save(out, "PNG", compress_level=1)
    return out.getvalue()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    Indexer().run()
