"""People: finds faces in photos and recognizes the ones you've named. Opt-in (`sift faces on`), off by default.

Two small models from OpenCV's model zoo (Apache 2.0), on the CPU: YuNet finds faces (0.2 MB), SFace turns each face
into a 128-number faceprint (37 MB); faceprints of the same person sit close together whatever the photo. A person is
the faceprints you gave Sift for them (reference photos, or a group of found faces), and a photo shows them when one of
its faces is close enough to one of those. Everything stays in the local index; `sift faces off` deletes all of it."""

import json
import os
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from . import config, store

ZOO = "https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/models"
DETECTOR = config.MODELS_DIR / "face_detection_yunet_2023mar.onnx"
RECOGNIZER = config.MODELS_DIR / "face_recognition_sface_2021dec.onnx"
MODELS = [
    (DETECTOR, f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
     "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    (RECOGNIZER, f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
     "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
]

MAX_SIDE = 1600        # photos are scaled down to this for detection: small faces in wide shots still show
MIN_FACE = 28          # px (after scaling): smaller faces are too blurry to recognize
MIN_SCORE = 0.8        # detector confidence
GROUP_SIMILARITY = 0.5 # stricter than matching: a group should be one person

SCHEMA = """
CREATE TABLE IF NOT EXISTS faces (
  id INTEGER PRIMARY KEY,
  file INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  x INTEGER, y INTEGER, w INTEGER, h INTEGER,   -- in the original photo's pixels
  score REAL,
  vector BLOB NOT NULL                          -- 128 float32, L2-normalized
);
CREATE INDEX IF NOT EXISTS faces_file ON faces(file);
CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS person_refs (
  person INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  vector BLOB NOT NULL
);
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(files)")}
    if "faces_done" not in columns:
        db.execute("ALTER TABLE files ADD COLUMN faces_done INTEGER NOT NULL DEFAULT 0")


def ensure_models() -> None:
    import hashlib

    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for path, url, sha in MODELS:
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha:
            continue
        print(f"Downloading {path.name}")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        if hashlib.sha256(tmp.read_bytes()).hexdigest() != sha:
            tmp.unlink()
            raise RuntimeError(f"{path.name}: checksum mismatch")
        tmp.replace(path)


def wipe(db) -> None:
    """Deletes every faceprint and person: what turning faces off means."""
    ensure_schema(db)
    db.execute("DELETE FROM faces")
    db.execute("DELETE FROM person_refs")
    db.execute("DELETE FROM people")
    db.execute("UPDATE files SET faces_done = 0")
    store.set_meta(db, "face_groups", "{}")
    store.bump_generation(db)


class FaceReader:
    """YuNet + SFace. Loaded only when faces are on (OpenCV costs ~50 MB)."""

    def __init__(self, threads: int = 2):
        import cv2

        cv2.setNumThreads(threads)
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        except AttributeError:
            pass
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(str(DETECTOR), "", (320, 320), MIN_SCORE, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER), "")

    def read(self, path: Path) -> list[dict]:
        """Every recognizable face in a photo: {"box": (x, y, w, h), "score", "vector"}."""
        image = Image.open(path)
        image.seek(0)
        image = ImageOps.exif_transpose(image).convert("RGB")
        scale = min(1.0, MAX_SIDE / max(image.size))
        if scale < 1:
            image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.BILINEAR)
        bgr = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
        self.detector.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, found = self.detector.detect(bgr)
        faces = []
        for row in found if found is not None else []:
            x, y, w, h = row[:4]
            if min(w, h) < MIN_FACE:
                continue
            vector = self.recognizer.feature(self.recognizer.alignCrop(bgr, row)).flatten().astype(np.float32)
            vector /= max(np.linalg.norm(vector), 1e-12)
            faces.append({"box": tuple(int(v / scale) for v in (x, y, w, h)), "score": float(row[-1]),
                          "vector": vector})
        return faces


def add_faces(db, file_id: int, faces: list[dict]) -> None:
    db.execute("DELETE FROM faces WHERE file = ?", (file_id,))
    for face in faces:
        x, y, w, h = face["box"]
        db.execute("INSERT INTO faces (file, x, y, w, h, score, vector) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (file_id, x, y, w, h, face["score"], face["vector"].tobytes()))
    db.execute("UPDATE files SET faces_done = 1 WHERE id = ?", (file_id,))


# ---------------------------------------------------------------- people

def people(db) -> dict[str, np.ndarray]:
    """Each named person's reference faceprints, by name."""
    out: dict[str, list] = {}
    for name, vector in db.execute("SELECT p.name, r.vector FROM people p JOIN person_refs r ON r.person = p.id"):
        out.setdefault(name, []).append(np.frombuffer(vector, np.float32))
    return {name: np.stack(vectors) for name, vectors in out.items()}


def add_person(db, name: str, vectors: list[np.ndarray]) -> None:
    db.execute("INSERT INTO people (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    person = db.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()[0]
    for vector in vectors:
        db.execute("INSERT INTO person_refs (person, vector) VALUES (?, ?)", (person, vector.astype(np.float32).tobytes()))
    store.bump_generation(db)


def remove_person(db, name: str) -> bool:
    gone = db.execute("DELETE FROM people WHERE name = ?", (name,)).rowcount > 0
    store.bump_generation(db)
    return gone


class FaceIndex:
    """All faceprints in memory (128 floats each), for matching people at search time."""

    def __init__(self, db):
        self.db = db
        self.generation = None
        self.load()

    def load(self) -> None:
        ensure_schema(self.db)
        rows = self.db.execute("SELECT f.id, f.file, f.vector FROM faces f").fetchall()
        self.face_ids = np.fromiter((r[0] for r in rows), np.int64, len(rows))
        self.face_files = np.fromiter((r[1] for r in rows), np.int64, len(rows))
        self.vectors = np.frombuffer(b"".join(r[2] for r in rows), np.float32).reshape(len(rows), 128) \
            if rows else np.zeros((0, 128), np.float32)
        self.people = people(self.db)
        self.generation = store.get_meta(self.db, "generation")

    def refresh(self) -> None:
        if store.get_meta(self.db, "generation") != self.generation:
            self.load()

    def matches(self, name: str, threshold: float) -> dict[int, float]:
        """The files showing this person, with how close the best face is."""
        refs = self.people.get(name)
        if refs is None or not len(self.vectors):
            return {}
        similarity = (self.vectors @ refs.T).max(axis=1)
        out: dict[int, float] = {}
        for i in np.nonzero(similarity >= threshold)[0]:
            f = int(self.face_files[i])
            out[f] = max(out.get(f, 0.0), float(similarity[i]))
        return out

    def names_in(self, query: str) -> tuple[list[str], str]:
        """The named people a query mentions, and the query without their names ("om at a mountain" → ["om"],
        "at a mountain"). Longer names first, so "om mishra" wins over "om"."""
        found, rest = [], " " + query + " "
        for name in sorted(self.people, key=len, reverse=True):
            needle = " " + name.lower() + " "
            lowered = rest.lower()
            i = lowered.find(needle)
            if i >= 0:
                found.append(name)
                rest = rest[:i] + " " + rest[i + len(needle):]
        rest = " ".join(w for w in rest.split() if w.lower() not in ("and", "with", "&", ",", "of"))
        return found, rest


# ---------------------------------------------------------------- groups of unnamed faces

def groups(db, threshold: float) -> list[list[int]]:
    """Faces nobody has been named for, grouped by who they look like (biggest group first). Kept in the index's meta
    so `sift person add NAME --group N` refers to what `sift faces groups` showed."""
    index = FaceIndex(db)
    if not len(index.vectors):
        return []
    named = np.zeros(len(index.vectors), bool)
    for refs in index.people.values():
        named |= (index.vectors @ refs.T).max(axis=1) >= threshold
    rows = np.nonzero(~named)[0]
    vectors = index.vectors[rows]
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for start in range(0, len(rows), 512):
        sims = vectors[start:start + 512] @ vectors.T
        for a, b in zip(*np.nonzero(sims >= GROUP_SIMILARITY)):
            a += start
            if a < b:
                parent[find(a)] = find(b)
    clusters: dict[int, list[int]] = {}
    for i in range(len(rows)):
        clusters.setdefault(find(i), []).append(int(index.face_ids[rows[i]]))
    ordered = sorted((c for c in clusters.values() if len(c) >= 2), key=len, reverse=True)
    store.set_meta(db, "face_groups", json.dumps({str(n + 1): c for n, c in enumerate(ordered)}))
    return ordered


def group_vectors(db, number: int) -> list[np.ndarray]:
    saved = json.loads(store.get_meta(db, "face_groups") or "{}")
    ids = saved.get(str(number))
    if not ids:
        return []
    marks = ",".join(str(i) for i in ids)
    return [np.frombuffer(v, np.float32) for (v,) in db.execute(f"SELECT vector FROM faces WHERE id IN ({marks})")]


def contact_sheet(db, face_ids: list[int], out: Path, limit: int = 8) -> Path:
    """A strip of the group's faces, to see who it is."""
    marks = ",".join(str(i) for i in face_ids[:limit])
    rows = db.execute(f"SELECT fl.path, f.x, f.y, f.w, f.h FROM faces f JOIN files fl ON fl.id = f.file "
                      f"WHERE f.id IN ({marks})").fetchall()
    size = 160
    sheet = Image.new("RGB", (size * max(len(rows), 1), size), (24, 24, 24))
    for i, (path, x, y, w, h) in enumerate(rows):
        try:
            image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        except OSError:
            continue
        pad = int(max(w, h) * 0.3)
        crop = image.crop((max(x - pad, 0), max(y - pad, 0), x + w + pad, y + h + pad))
        crop.thumbnail((size, size))
        sheet.paste(crop, (i * size + (size - crop.width) // 2, (size - crop.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=88)
    return out
