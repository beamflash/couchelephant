"""Sealed boxes: reports are readable only with the owner's private key."""
import base64
import json
from pathlib import Path

import pytest

from app import sealing


def test_round_trip():
    priv, pub = sealing.generate_keypair()
    assert sealing.open_sealed(sealing.seal(b"hello report", pub), priv) == b"hello report"


def test_wrong_key_fails_with_sealerror():
    _, pub = sealing.generate_keypair()
    other_priv, _ = sealing.generate_keypair()
    with pytest.raises(sealing.SealError):
        sealing.open_sealed(sealing.seal(b"x", pub), other_priv)


def test_two_seals_of_same_text_differ():
    _, pub = sealing.generate_keypair()
    assert sealing.seal(b"same", pub) != sealing.seal(b"same", pub)


def test_tampered_blob_fails():
    priv, pub = sealing.generate_keypair()
    blob = bytearray(sealing.seal(b"x", pub))
    blob[-1] ^= 1
    with pytest.raises(sealing.SealError):
        sealing.open_sealed(bytes(blob), priv)


def test_key_id_is_stable_8_hex():
    _, pub = sealing.generate_keypair()
    kid = sealing.key_id(pub)
    assert kid == sealing.key_id(pub)
    assert len(kid) == 8 and int(kid, 16) >= 0 and kid == kid.lower()


def test_vector_file_opens():
    v = json.loads((Path(__file__).parent / "data" / "seal_vector.json").read_text())
    priv, pub = base64.b64decode(v["private"]), base64.b64decode(v["public"])
    assert sealing.key_id(pub) == v["keyId"]
    out = sealing.open_sealed(base64.b64decode(v["sealed"]), priv)
    assert out == base64.b64decode(v["plaintext"])
