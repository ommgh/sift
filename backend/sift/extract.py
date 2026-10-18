"""Turns files into what the model reads: passages of text, and pictures sized for its vision encoder."""

import io
import math
import os
import re
import subprocess
import threading
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageOps

# Files come from anywhere (Downloads), so every decoder is bounded by what it may produce, not by the file's size.
MAX_PIXELS = 120_000_000        # a picture, decoded (a 200 MP phone photo is decoded at half size, see open_image)
MAX_RENDER_PIXELS = 16_000_000  # a PDF page, rendered (an A4 page at 150 dpi is 2.2 MP)
MAX_XML_BYTES = 64 << 20        # a DOCX/ODT's text, unpacked (a whole book is a few MB)
# Only the decoders for the types Sift reads: Pillow picks a decoder by content, and some (EPS) run Ghostscript.
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF")
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

CHUNK_CHARS = 1800      # ~450 tokens
CHUNK_OVERLAP = 200
MAX_CHUNKS = 5000
MIN_PAGE_TEXT = 200     # a PDF page with less text than this is embedded as a picture (a scan)
MAX_PAGES = 5000
SCALE_PAGES = 30        # pages past this don't move a query's scale (see search.meaning)

# Vision soft tokens per picture: more for text-dense screenshots and pages.
PHOTO_BUDGET = 140
SCREENSHOT_BUDGET = 280
PAGE_BUDGET = 280


# ---------------------------------------------------------------- text

def query_prompt(text: str) -> str:
    return f"task: search result | query: {text}"


def document_prompt(title: str, text: str) -> str:
    return f"title: {title or 'none'} | text: {text}"


def has_words(text: str) -> bool:
    """Whether text is worth a vector: mostly letters, not a dump of numbers (that's matched by its exact text)."""
    letters = sum(c.isalpha() for c in text)
    visible = sum(not c.isspace() for c in text)
    return visible > 0 and letters >= 20 and letters / visible >= 0.3


def chunks(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP, limit: int = MAX_CHUNKS) -> list[str]:
    clean = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    out, start = [], 0
    while start < len(clean) and len(out) < limit:
        end = min(start + size, len(clean))
        if end < len(clean):
            space = clean.rfind(" ", start + size // 2, end)
            if space > 0:
                end = space
        piece = clean[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(clean):
            break
        nxt = max(end - overlap, start + 1)
        space = clean.find(" ", nxt, end)
        start = space + 1 if space > 0 else nxt
    return out


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _zipped_xml_text(path: Path, member: str, paragraph_tag: str) -> str:
    """The paragraphs of an XML file inside a zip (a DOCX or ODT), unpacked no further than MAX_XML_BYTES whatever the
    zip claims, and without a DOCTYPE (Office files never have one; it's how entity expansion starts)."""
    with zipfile.ZipFile(path) as z:
        if z.getinfo(member).file_size > MAX_XML_BYTES:
            raise ValueError("too large once unpacked")
        with z.open(member) as f:
            data = f.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ValueError("too large once unpacked")
    if b"<!DOCTYPE" in data:
        raise ValueError("unexpected DOCTYPE")
    paragraphs = []
    for _, element in ElementTree.iterparse(io.BytesIO(data)):
        if element.tag == paragraph_tag:
            paragraphs.append("".join(element.itertext()))
            element.clear()
    return "\n".join(paragraphs)


def _rtf_text(raw: str) -> str:
    text = re.sub(r"\\'[0-9a-f]{2}", " ", raw)
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\{\\\*[^{}]*\}", "", text)
    text = re.sub(r"\\[a-z]+-?\d* ?", "", text)
    return text.replace("{", "").replace("}", "")


def doc_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return _zipped_xml_text(path, "word/document.xml",
                                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
    if ext == ".odt":
        return _zipped_xml_text(path, "content.xml", "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p")
    raw = path.read_bytes().decode("utf-8", errors="replace")
    if ext in (".html", ".htm"):
        parser = _HTMLText()
        parser.feed(raw)
        return "".join(parser.parts)
    if ext == ".rtf":
        return _rtf_text(raw)
    return raw


# ---------------------------------------------------------------- pictures

def target_size(width: int, height: int, budget: int) -> tuple[int, int] | None:
    """transformers' Gemma image processor: scale to fill `budget` soft tokens (each 3 × 3 patches of 16 px) keeping
    the aspect ratio, each side rounded down to a multiple of 48 px. (Checked against Pillow.)"""
    patch, pooling = 16, 3
    max_patches = budget * pooling * pooling
    factor = math.sqrt(max_patches * patch * patch / (height * width))
    side = pooling * patch
    th = int(factor * height / side) * side
    tw = int(factor * width / side) * side
    if th == 0 and tw == 0:
        return None
    max_side = (max_patches // (pooling * pooling)) * side
    if th == 0:
        th, tw = side, min(width // height * side, max_side)
    elif tw == 0:
        tw, th = side, min(height // width * side, max_side)
    return tw, th


def prepare_image(image: Image.Image, budget: int) -> bytes:
    """The picture as PNG bytes at exactly the size the vision encoder takes for `budget`."""
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[3])
        image = canvas
    else:
        image = image.convert("RGB")
    size = target_size(image.width, image.height, budget)
    if size is None:
        raise ValueError("too small to read")
    image = image.resize(size, Image.Resampling.BICUBIC)
    out = io.BytesIO()
    image.save(out, "PNG", compress_level=1)
    return out.getvalue()


def open_image(path: Path, draft: bool = False) -> Image.Image:
    """A picture, decoded only if it's one of IMAGE_FORMATS and at most MAX_PIXELS. draft: a big JPEG may be decoded at
    1/2, 1/4 or 1/8 size, still at least 2000 px a side (more than the vision encoder or face detection use)."""
    image = Image.open(path, formats=IMAGE_FORMATS)
    image.seek(0)   # a GIF's first frame
    image.info["full_size"] = image.size
    if draft and image.format == "JPEG":
        image.draft("RGB", (2000, 2000))
    if image.width * image.height > MAX_PIXELS:
        raise ValueError(f"too large to read ({image.width} × {image.height})")
    image.load()
    return image


def ocr_start(source: Path | bytes) -> subprocess.Popen | None:
    """Starts tesseract on one thread, so it reads a picture while the GPU embeds it (see ocr_finish)."""
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    target = "stdin" if isinstance(source, bytes) else str(source)
    try:
        proc = subprocess.Popen(["tesseract", target, "stdout", "-l", "eng", "--psm", "3"], env=env,
                                stdin=subprocess.PIPE if isinstance(source, bytes) else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if isinstance(source, bytes):
        threading.Thread(target=_feed, args=(proc, source), daemon=True).start()
    return proc


def _feed(proc: subprocess.Popen, data: bytes) -> None:
    try:
        proc.stdin.write(data)
        proc.stdin.close()
    except OSError:
        pass


def ocr_finish(proc: subprocess.Popen | None) -> str:
    """Text in a picture (for exact words: codes, errors, names), lines without a real word dropped as noise."""
    if proc is None:
        return ""
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        return ""
    text = out.decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if re.search(r"[A-Za-z]{3,}", line)]
    return "\n".join(lines)


def ocr(source: Path | bytes) -> str:
    return ocr_finish(ocr_start(source))


# ---------------------------------------------------------------- PDFs

def pdf_pages(path: Path):
    """Yields (page number from 1, text, rendered page or None) for each page; a page with little text is rendered
    for the vision encoder and OCR (a scan, a slide, a chart)."""
    import pymupdf

    with pymupdf.open(path, filetype="pdf") as pdf:
        if pdf.needs_pass:
            raise ValueError("password protected")
        for number, page in enumerate(pdf, start=1):
            if number > MAX_PAGES:
                break
            text = page.get_text("text") or ""
            image = None
            if len(text.strip()) < MIN_PAGE_TEXT:
                pix = render(page, 150 / 72)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            yield number, text, image


def render(page, zoom: float, max_pixels: int = MAX_RENDER_PIXELS):
    """A PDF page as a pixmap at zoom (1 = 72 dpi), smaller if that would be more than max_pixels: a page can claim to
    be any size."""
    import pymupdf

    area = max(page.rect.width * page.rect.height, 1) * zoom * zoom
    if area > max_pixels:
        zoom *= math.sqrt(max_pixels / area)
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))


def pdf_page_count(path: Path) -> int:
    import pymupdf

    with pymupdf.open(path, filetype="pdf") as pdf:
        return pdf.page_count
