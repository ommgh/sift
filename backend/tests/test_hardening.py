"""The guarantees in the README's "Privacy and safety" and AGENTS.md's "Hardening": a hostile file can't make a reader
run away with memory, and what Sift keeps is readable by the user alone."""

import io
import os
import stat
import struct
import subprocess
import tomllib
import zipfile
import zlib
from pathlib import Path

import pytest
from PIL import Image

from sift import config, extract, llama, rules, shortcut, store

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(path: Path, xml: str | bytes) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return path


# ---------------------------------------------------------------- readers

def test_docx_text(tmp_path):
    path = docx(tmp_path / "ok.docx", f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>The lease clause</w:t></w:r>'
                                      f'</w:p><w:p><w:t>about pets</w:t></w:p></w:body></w:document>')
    assert extract.doc_text(path) == "The lease clause\nabout pets"


def test_docx_zip_bomb_is_refused(tmp_path):
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z, z.open("word/document.xml", "w", force_zip64=True) as f:
        f.write(f'<w:document xmlns:w="{W}"><w:p><w:t>'.encode())
        for _ in range((extract.MAX_XML_BYTES >> 20) + 8):
            f.write(b" " * (1 << 20))
        f.write(b"</w:t></w:p></w:document>")
    assert path.stat().st_size < 1 << 20
    with pytest.raises(ValueError, match="too large"):
        extract.doc_text(path)


def test_docx_doctype_is_refused(tmp_path):
    laughs = ('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
              f'<w:document xmlns:w="{W}"><w:p><w:t>&b;</w:t></w:p></w:document>')
    with pytest.raises(ValueError, match="DOCTYPE"):
        extract.doc_text(docx(tmp_path / "laughs.docx", laughs))


def test_image_decoders_are_limited(tmp_path):
    fake = tmp_path / "fake.jpg"   # Pillow would hand EPS to Ghostscript
    fake.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n")
    with pytest.raises(Exception):
        extract.open_image(fake)


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\0")) + chunk(b"IEND", b"")


@pytest.mark.parametrize("side", [11_000, 40_000])   # past MAX_PIXELS; past Pillow's own bomb limit
def test_huge_image_is_refused_before_decoding(tmp_path, side):
    path = tmp_path / "huge.png"
    path.write_bytes(_png(side, side))
    with pytest.raises((ValueError, Image.DecompressionBombError)):
        extract.open_image(path)


def test_big_jpeg_is_decoded_smaller_but_keeps_its_size(tmp_path):
    path = tmp_path / "big.jpg"
    Image.new("RGB", (6000, 4500), (120, 160, 200)).save(path, quality=80)
    image = extract.open_image(path, draft=True)
    assert image.info["full_size"] == (6000, 4500)
    assert image.width * image.height < 6000 * 4500
    assert extract.prepare_image(image, extract.PHOTO_BUDGET)


def test_pdf_page_render_is_capped(tmp_path):
    import pymupdf

    pdf = pymupdf.open()
    pdf.new_page(width=14400, height=14400)   # 200 inches square, no text: rendered as a picture
    pdf.save(tmp_path / "giant.pdf")
    (number, text, image), = list(extract.pdf_pages(tmp_path / "giant.pdf"))
    assert image.width * image.height <= extract.MAX_RENDER_PIXELS * 1.01


def test_chunks_are_capped():
    assert len(extract.chunks("word " * 200_000, limit=10)) == 10


# ---------------------------------------------------------------- what gets read

def test_secrets_are_never_read():
    for name in ("id_ed25519", ".env", "AuthKey_X.p8", "server.pem", "credentials.txt", "secret-notes.md"):
        assert rules.kind_of(Path("/home/u/Documents") / name, False) is None


def test_symlinks_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("private")
    folder = tmp_path / "Documents"
    folder.mkdir()
    (folder / "real.txt").write_text("hello")
    (folder / "link.txt").symlink_to(outside / "notes.txt")
    (folder / "linked-folder").symlink_to(outside)
    assert [p.name for p, _, _ in rules.walk(folder, [])] == ["real.txt"]


# ---------------------------------------------------------------- what Sift keeps

def test_storage_is_private(tmp_path):
    for folder in (config.DATA_DIR, config.CACHE_DIR, config.STATE_DIR, config.CONFIG_DIR):
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o755)
    before = os.umask(0o022)
    try:
        config.make_private()
        assert os.umask(0o077) == 0o077
        for folder in (config.DATA_DIR, config.CACHE_DIR, config.STATE_DIR, config.CONFIG_DIR):
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        db_path = tmp_path / "index.db"
        db_path.touch(mode=0o644)
        db_path.chmod(0o644)
        store.connect(db_path).close()
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    finally:
        os.umask(before)


def test_model_server_listens_on_a_private_unix_socket(tmp_path, monkeypatch):
    fake = tmp_path / "llama-server"
    fake.touch()
    monkeypatch.setattr(config, "LLAMA_SERVER", fake)
    seen = {}

    def popen(args, **kwargs):
        seen["args"] = args
        raise RuntimeError("not started")

    monkeypatch.setattr(subprocess, "Popen", popen)
    server = llama.LlamaServer(media=False, device=None, threads=1, name="test")
    with pytest.raises(RuntimeError):
        server.start()
    args = seen["args"]
    host = args[args.index("--host") + 1]
    assert host.endswith(".sock") and Path(host).parent == config.RUNTIME_DIR
    assert "--port" not in args and "--no-slots" in args


# ---------------------------------------------------------------- what Sift writes into other programs' files

@pytest.mark.parametrize("value", ['say "hi"', "back\\slash", "new\nline", "brack]et", "~/Ünïcode folder"])
def test_settings_strings_round_trip(value):
    text = config._write_top_level('folders = ["~/a"]\n\n[power]\nac_only = true\n', "folders", [value, "~/b"])
    assert tomllib.loads(text)["folders"] == [value, "~/b"]
    again = config._write_top_level(text, "folders", ["~/c"])
    assert tomllib.loads(again) == {"folders": ["~/c"], "power": {"ac_only": True}}


@pytest.mark.parametrize("combo", ['SUPER + S")os.execute("x', "SUPER + S\n--", "SUPER + 'S'", "SUPER +"])
def test_shortcut_keys_cant_inject_lua(combo):
    with pytest.raises(ValueError):
        shortcut.normalize(combo)


def test_shortcut_keys():
    assert shortcut.normalize("super+s") == "SUPER + S"
    assert shortcut.normalize("ctrl alt super f1") == "SUPER + CTRL + ALT + F1"
