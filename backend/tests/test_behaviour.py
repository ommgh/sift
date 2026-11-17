"""Behaviour that's easy to break without noticing: how queries name people, which keys a shortcut accepts, and what
the daemon's socket takes."""

import json
import socket
import threading

import numpy as np
import pytest

from sift import daemon, faces, shortcut


@pytest.fixture
def people():
    index = faces.FaceIndex.__new__(faces.FaceIndex)
    index.people = {"me": np.zeros((1, 128)), "om mishra": np.zeros((1, 128)), "om": np.zeros((1, 128))}
    return index


@pytest.mark.parametrize("query, names, rest", [
    ("me at the beach", ["me"], "at the beach"),
    ("show me the lease", [], "show me the lease"),
    ("find me photos of me in the mountains", ["me"], "find me photos in the mountains"),
    ("om mishra and me", ["om mishra", "me"], ""),
    ("the omelette recipe", [], "the omelette recipe"),
])
def test_names_in_queries(people, query, names, rest):
    assert people.names_in(query) == (names, rest)


def test_keycodes_are_written_as_hyprland_reads_them():
    assert shortcut.normalize("super + code:39") == "SUPER + code:39"
    with pytest.raises(ValueError):
        shortcut.normalize("super + code:x")


def test_daemon_drops_oversized_requests(tmp_path):
    path = str(tmp_path / "sift.sock")
    server = daemon.Server(path, daemon.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(path)
            s.sendall(b'{"id": 1, "op": "ping"}\n')
            assert json.loads(s.makefile().readline())["ok"]
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(path)
            s.sendall(b'{"op": "ping", "pad": "' + b"x" * (daemon.MAX_REQUEST + 10) + b'"}\n')
            s.settimeout(5)
            assert s.recv(100) == b""   # closed, no reply
    finally:
        server.shutdown()
        server.server_close()
