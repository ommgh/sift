"""Hybrid search: meaning (EmbeddingGemma's vectors) + words (FTS5). The weights and gates:

- Meaning: each vector's cosine minus its modality's mean for this query (text and image vectors sit at different
  levels), in standard deviations of all of them. Only files that stand out (z ≥ 2) count as meaning matches, which
  also gives an honest "nothing found".
- Words: FTS5 over file names (weighted ×3) and text (pages, passages, OCR). A file qualifies with half the query's
  words. Codes and numbers ("INV-4411") weigh words double and meaning half.
- Score: z + 2 × coverage^1.5, + 1 when the query names the kind ("screenshot of…")."""

import json
import re
import time
from pathlib import Path

import numpy as np

from . import store

Z_GATE = 2.0
WORD_WEIGHT = 2.0
STRONG_RATIO = 0.5
BLOCK = 8192

STOPWORDS = set("""a an and are as at be but by for from has have i in is it its of on or that the this to was were
with where what when who which how my me about into than then there these those they their them we our you your""".split())

KIND_WORDS = {
    "screenshot": "screenshot", "screenshots": "screenshot", "screen": "screenshot", "capture": "screenshot",
    "photo": "image", "photos": "image", "picture": "image", "pictures": "image", "image": "image", "images": "image",
    "pic": "image", "pics": "image",
    "pdf": "pdf", "pdfs": "pdf",
    "document": "doc", "documents": "doc", "doc": "doc", "docs": "doc", "note": "doc", "notes": "doc",
}
MODALITY = {"text": 0, "image": 1}


def terms(query: str) -> list[str]:
    words = re.findall(r"[\w][\w'.-]*[\w]|\w", query.lower())
    return [w for w in words if w not in STOPWORDS and w not in KIND_WORDS and len(w) >= 2]


def kind_hints(query: str) -> set[str]:
    return {KIND_WORDS[w] for w in re.findall(r"\w+", query.lower()) if w in KIND_WORDS}


def is_lookup(ts: list[str]) -> bool:
    """Codes, IDs and long numbers: an embedding can't hold them, keyword search can."""
    return bool(ts) and all(any(c.isdigit() for c in t) and (any(c.isalpha() for c in t) or len(t) >= 4) for t in ts)


def meaning(scores: np.ndarray, modality: np.ndarray, sets_scale: np.ndarray) -> np.ndarray:
    scale_scores = scores[sets_scale]
    scale_modality = modality[sets_scale]
    overall = float(scale_scores.mean()) if len(scale_scores) else 0.0
    centered = scores.astype(np.float64).copy()
    for m in np.unique(modality):
        group = scale_scores[scale_modality == m]
        centered[modality == m] -= float(group.mean()) if len(group) >= 20 else overall
    ref = centered[sets_scale]
    mean = float(ref.mean()) if len(ref) else 0.0
    deviation = max(float(ref.std()) if len(ref) else 0.0, 0.02)
    return (centered - mean) / deviation


class Searcher:
    def __init__(self, db):
        self.db = db
        self.generation = None
        self.matrix = None
        self.files: dict[int, dict] = {}

    # ------------------------------------------------------------ vectors in memory

    def load(self) -> None:
        rows = self.db.execute(
            "SELECT s.id, s.file, s.modality, s.sets_scale, s.vector FROM segments s WHERE s.vector IS NOT NULL"
        ).fetchall()
        n = len(rows)
        self.seg_ids = np.fromiter((r[0] for r in rows), np.int64, n)
        self.seg_files = np.fromiter((r[1] for r in rows), np.int64, n)
        self.modality = np.fromiter((MODALITY.get(r[2], 0) for r in rows), np.int8, n)
        self.sets_scale = np.fromiter((bool(r[3]) for r in rows), bool, n)
        self.matrix = np.frombuffer(b"".join(r[4] for r in rows), dtype=np.float16).reshape(n, store.DIM) \
            if n else np.zeros((0, store.DIM), np.float16)
        self.files = {}
        for fid, path, kind, mtime, info in self.db.execute(
                "SELECT id, path, kind, mtime, info FROM files WHERE status = 'done'"):
            self.files[fid] = {"path": path, "kind": kind, "mtime": mtime, "info": json.loads(info or "{}")}
        self.generation = store.get_meta(self.db, "generation")

    def refresh(self) -> None:
        if self.matrix is None or store.get_meta(self.db, "generation") != self.generation:
            self.load()

    def release(self) -> None:
        """Frees the vectors (~1.5 KB each); the next search loads them again."""
        self.matrix = None
        self.files = {}

    def scores(self, query: np.ndarray) -> np.ndarray:
        out = np.empty(len(self.matrix), np.float32)
        q = query.astype(np.float32)
        for start in range(0, len(self.matrix), BLOCK):
            out[start:start + BLOCK] = self.matrix[start:start + BLOCK].astype(np.float32) @ q
        return out

    # ------------------------------------------------------------ words

    def indexed(self, pattern: str) -> bool:
        try:
            return self.db.execute("SELECT 1 FROM fts WHERE fts MATCH ? LIMIT 1", (pattern,)).fetchone() is not None
        except Exception:
            return False

    def patterns(self, ts: list[str], prefix_last: bool) -> list[str]:
        out = []
        for i, t in enumerate(ts):
            p = '"' + t.replace('"', "") + '"'
            last = i == len(ts) - 1
            if (prefix_last and last) or (len(t) >= 3 and not self.indexed(p)):
                p += "*"
            out.append(p)
        return out

    # ------------------------------------------------------------ search

    def search(self, text: str, query_vector: np.ndarray | None, limit: int = 30, prefix_last: bool = True,
               only: dict[int, float] | None = None) -> list:
        """`only`: the files a search is limited to (the photos of the people it names), with their face match."""
        self.refresh()
        file_ids = set(self.files) if only is None else set(self.files) & set(only)

        # Meaning: each file's best segment on this query's scale.
        best: dict[int, tuple[float, int]] = {}
        if query_vector is not None and len(self.matrix):
            z = meaning(self.scores(query_vector), self.modality, self.sets_scale)
            order = np.argsort(z)   # ascending: later writes (higher z) win
            rows = order[-4000:] if only is None else order[np.isin(self.seg_files[order], list(file_ids))]
            for row in rows:
                best[int(self.seg_files[row])] = (float(z[row]), int(self.seg_ids[row]))

        # Words.
        ts = terms(text)
        patterns = self.patterns(ts, prefix_last)
        words: dict[int, tuple[int, str]] = {}
        coverage: dict[int, float] = {}
        if patterns:
            try:
                for fid, sid, snip in self.db.execute(
                        "SELECT s.file, s.id, snippet(fts, 1, '«', '»', '…', 14) FROM fts "
                        "JOIN segments s ON s.id = fts.rowid WHERE fts MATCH ? ORDER BY bm25(fts, 3.0, 1.0) LIMIT 1000",
                        (" OR ".join(patterns),)):
                    if fid in file_ids and fid not in words and len(words) < 100:
                        words[fid] = (sid, snip)
                if words:
                    ids = ",".join(str(f) for f in words)
                    for p in patterns:
                        for (fid,) in self.db.execute(
                                f"SELECT DISTINCT s.file FROM fts JOIN segments s ON s.id = fts.rowid "
                                f"WHERE fts MATCH ? AND s.file IN ({ids})", (p,)):
                            coverage[fid] = coverage.get(fid, 0) + 1 / len(patterns)
            except Exception:
                pass   # a query FTS5 can't parse: meaning only

        lookup = is_lookup(ts)
        meaning_w, words_w = (0.5, WORD_WEIGHT * 2) if lookup else (1.0, WORD_WEIGHT)
        hinted = kind_hints(text)
        if only is not None:
            # A person's photos: every one qualifies, ranked by the rest of the query; with nothing else asked,
            # newest first.
            candidates = file_ids
        else:
            candidates = {f for f, (z, _) in best.items() if z >= Z_GATE} | {f for f, c in coverage.items() if c >= 0.5}
        scored = []
        for f in candidates:
            if f not in self.files:
                continue
            if only is not None and query_vector is None and not coverage:
                scored.append((self.files[f]["mtime"], f))
                continue
            z = best.get(f, (0, 0))[0]
            score = meaning_w * (z if only is not None else max(z, 0)) + words_w * coverage.get(f, 0) ** 1.5
            if self.files[f]["kind"] in hinted or ("image" in hinted and self.files[f]["kind"] == "screenshot"):
                score += 1
            scored.append((score, f))
        scored.sort(key=lambda x: (-x[0], x[1]))

        listing = only is not None and query_vector is None and not coverage
        hits = []
        for score, f in scored[:limit]:
            if listing:
                score = only[f]   # sorted newest first; shown as how close the face is
            sem, kw = best.get(f), words.get(f)
            use_meaning = sem is not None and (sem[0] >= Z_GATE or kw is None or only is not None)
            if use_meaning:
                segment = sem[1]
            elif kw:
                segment = kw[0]
            else:
                segment = self.db.execute("SELECT id FROM segments WHERE file = ? ORDER BY id LIMIT 1", (f,)).fetchone()[0]
            hits.append(self.hit(f, segment, score, kw[1] if kw and kw[0] == segment else None))
        top = hits[0]["score"] if hits else 0
        for h in hits:
            h["strong"] = only is not None or top <= 0 or h["score"] >= top * STRONG_RATIO
        return hits

    def hit(self, file_id: int, segment_id: int, score: float, word_snippet: str | None) -> dict:
        f = self.files[file_id]
        loc, modality, text = self.db.execute(
            "SELECT loc, modality, text FROM segments WHERE id = ?", (segment_id,)).fetchone()
        path = Path(f["path"])
        snippet = " ".join(word_snippet.split()) if word_snippet else ""
        if not snippet and text:
            snippet = " ".join(text.split())[:240]   # a passage, a page, or a screenshot's OCR
        home = str(Path.home())
        folder = str(path.parent)
        return {
            "path": f["path"], "name": path.name, "folder": "~" + folder[len(home):] if folder.startswith(home) else folder,
            "kind": f["kind"], "page": loc if f["kind"] == "pdf" else None, "score": round(score, 3),
            "snippet": snippet or "", "mtime": f["mtime"], "info": f["info"],
        }
