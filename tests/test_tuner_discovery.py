"""Finding boxes: the ones Plex names and the ones that answer on the network."""
import socket
import struct
import threading
import zlib

from app import db, tunerbox
from tests import fake_tuner


class FakePlex:
    def __init__(self, *uris):
        self.uris = uris

    def grabber_devices(self):
        return [{"uri": u} for u in self.uris]


def _box(**kw):
    url, stop, state = fake_tuner.start_box(**kw)
    return url, stop, state


def test_plex_named_boxes_are_added_once(monkeypatch):
    url, stop, _ = _box(device_id="FAKE000A", total=4)
    try:
        monkeypatch.setattr(tunerbox, "broadcast", lambda *a, **k: [])
        port = url.rsplit(":", 1)[1]
        # The same box by IP and by a name that reaches it.
        plex = FakePlex(f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        out = tunerbox.discover(plex)
        rows = tunerbox.rows()
        assert len(rows) == 1
        assert rows[0]["found_by"] == "plex"
        assert rows[0]["address"] == url
        assert rows[0]["tuner_count"] == 4
        assert out["found"] == 1
    finally:
        stop()


def test_discovery_refreshes_a_typed_row_but_keeps_its_address(monkeypatch):
    url, stop, state = _box(device_id="FAKE000A", total=4)
    try:
        monkeypatch.setattr(tunerbox, "broadcast", lambda *a, **k: [])
        box_id = tunerbox.add_typed(url)
        with db.tx() as c:
            c.execute("UPDATE tuner_boxes SET name = 'old', last_seen = 1, error = 'x' "
                      "WHERE id = ?", (box_id,))
        tunerbox.discover()
        row = tunerbox.get(box_id)
        assert row["address"] == url
        assert row["name"] != "old"
        assert row["error"] is None
        assert row["last_seen"] > 1
    finally:
        stop()


def test_a_silent_row_gets_its_error_written(monkeypatch):
    monkeypatch.setattr(tunerbox, "broadcast", lambda *a, **k: [])
    box_id = tunerbox.add_typed("http://192.0.2.1")
    tunerbox.discover()
    assert "did not answer" in tunerbox.get(box_id)["error"]


def test_the_discover_packet_is_what_hdhomerun_expects():
    pkt = tunerbox.discover_packet()
    body = bytes.fromhex("0002000c" "0104ffffffff" "0204ffffffff")
    assert pkt[:-4] == body
    # CRC32 of everything before it, low byte first (hdhomerun_pkt_seal_frame).
    assert pkt[-4:] == struct.pack("<I", zlib.crc32(body))
    assert pkt.hex() == "0002000c0104ffffffff0204ffffffff73cc7d8f"


def test_a_reply_from_a_box_is_read_back(monkeypatch):
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    got = []

    def answer():
        data, addr = srv.recvfrom(2048)
        got.append(data)
        srv.sendto(b"reply", addr)

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    monkeypatch.setattr(tunerbox, "DISCOVER_ADDR", "127.0.0.1")
    monkeypatch.setattr(tunerbox, "DISCOVER_PORT", port)
    try:
        assert tunerbox.broadcast(timeout=1) == ["127.0.0.1"]
    finally:
        t.join(2)
        srv.close()
    assert got == [tunerbox.discover_packet()]


def test_a_network_reply_becomes_a_found_row(monkeypatch):
    url, stop, _ = _box(device_id="FAKE000A", total=4)
    try:
        monkeypatch.setattr(tunerbox, "broadcast", lambda *a, **k: ["127.0.0.1"])
        # The fake answers on its own port, so aim the probe at it.
        real = tunerbox._probe
        monkeypatch.setattr(tunerbox, "_probe",
                            lambda a: real(url) if a == "http://127.0.0.1" else real(a))
        tunerbox.discover()
        assert [r["found_by"] for r in tunerbox.rows()] == ["network"]
        assert db.get_setting("tuner_broadcast_ok") == "1"
    finally:
        stop()


def test_no_broadcast_says_so_and_breaks_nothing(monkeypatch):
    def boom(*a, **k):
        raise OSError("Network is unreachable")
    monkeypatch.setattr(tunerbox, "broadcast", boom)
    out = tunerbox.discover()
    assert out["can_find_on_network"] is False
    assert db.get_setting("tuner_broadcast_ok") == "0"


def test_silence_on_a_docker_bridge_says_the_network_cannot_be_searched(monkeypatch):
    monkeypatch.setattr(tunerbox, "broadcast", lambda *a, **k: [])
    monkeypatch.setattr(tunerbox, "_on_docker_bridge", lambda: True)
    assert tunerbox.discover()["can_find_on_network"] is False
    monkeypatch.setattr(tunerbox, "_on_docker_bridge", lambda: False)
    assert tunerbox.discover()["can_find_on_network"] is True
