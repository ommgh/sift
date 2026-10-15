"""The index: one SQLite file. Files, their segments (a page, a passage, a picture) with a float16 vector each, and an
FTS5 keyword index over file names and segment text. WAL mode, so the search daemon reads while the indexer writes."""

import json
import sqlite3
import time
from pathlib import Path

import numpy as np

from . import config

DIM = 768
# Bumped when the vectors change (model files, llama.cpp, prompts, image sizing): every file is then read again.
VECTOR_VERSION = "embeddinggemma-2-q8_0/llama-b11461/v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,
  mtime REAL NOT NULL,
  size INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | error
  error TEXT,
  info TEXT,                                -- JSON: pages, width, height
  indexed_at REAL
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  file INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  loc INTEGER NOT NULL DEFAULT 0,           -- page (from 1) or passage (from 0)
  modality TEXT NOT NULL,                   -- text | image
  sets_scale INTEGER NOT NULL DEFAULT 1,    -- 0 for the later pages of long files (see search.meaning)
  text TEXT,
  vector BLOB
);
CREATE INDEX IF NOT EXISTS segments_file ON segments(file);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, body, tokenize = 'porter unicode61 remove_diacritics 2');
"""


def connect(path: Path = config.INDEX_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    if get_meta(db, "vector_version") != VECTOR_VERSION:
        if get_meta(db, "vector_version") is not None:
            db.execute("DELETE FROM fts")
            db.execute("DELETE FROM segments")
            db.execute("UPDATE files SET status = 'pending'")
        set_meta(db, "vector_version", VECTOR_VERSION)
    return db


def get_meta(db, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key: str, value) -> None:
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, str(value)))


def bump_generation(db) -> None:
    """Tells the search daemon its loaded vectors are stale."""
    set_meta(db, "generation", time.time())


def name_text(path: Path) -> str:
    """What keyword search matches a file's name by: its name, with separators as spaces, and its folder's."""
    stem = path.stem
    for sep in "_-.+":
        stem = stem.replace(sep, " ")
    return f"{path.name} {stem} {path.parent.name}"


def to_blob(vector) -> bytes:
    return np.asarray(vector, dtype=np.float16).tobytes()


def upsert_file(db, path: Path, kind: str, mtime: float, size: int) -> bool:
    """Records a file found on disk. True if it's new or changed (and so needs reading)."""
    row = db.execute("SELECT id, mtime, size, kind FROM files WHERE path = ?", (str(path),)).fetchone()
    if row and row[1] == mtime and row[2] == size and row[3] == kind:
        return False
    if row:
        clear_segments(db, row[0])
        db.execute("UPDATE files SET kind = ?, mtime = ?, size = ?, status = 'pending', error = NULL WHERE id = ?",
                   (kind, mtime, size, row[0]))
    else:
        db.execute("INSERT INTO files (path, kind, mtime, size) VALUES (?, ?, ?, ?)", (str(path), kind, mtime, size))
    return True


def clear_segments(db, file_id: int) -> None:
    db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE file = ?)", (file_id,))
    db.execute("DELETE FROM segments WHERE file = ?", (file_id,))


def remove_file(db, path: str) -> None:
    row = db.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
    if row:
        clear_segments(db, row[0])
        db.execute("DELETE FROM files WHERE id = ?", (row[0],))


def add_segment(db, file_id: int, path: Path, loc: int, modality: str, text: str | None, vector,
                sets_scale: bool = True) -> None:
    cur = db.execute(
        "INSERT INTO segments (file, loc, modality, sets_scale, text, vector) VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, loc, modality, int(sets_scale), text, to_blob(vector) if vector is not None else None))
    db.execute("INSERT INTO fts (rowid, name, body) VALUES (?, ?, ?)", (cur.lastrowid, name_text(path), text or ""))


def finish_file(db, file_id: int, info: dict | None = None, error: str | None = None) -> None:
    db.execute("UPDATE files SET status = ?, error = ?, info = ?, indexed_at = ? WHERE id = ?",
               ("error" if error else "done", error, json.dumps(info or {}), time.time(), file_id))


def counts(db) -> dict:
    rows = db.execute("SELECT status, count(*) FROM files GROUP BY status").fetchall()
    return {status: n for status, n in rows}
