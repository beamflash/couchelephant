"""Sealed boxes for issue reports. Anyone can seal to the owner's public key;
only the private key opens it. Each seal uses a fresh ephemeral key."""
import hashlib

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox


class SealError(Exception):
    pass


def generate_keypair() -> tuple[bytes, bytes]:
    """(private, public), 32 bytes each."""
    k = PrivateKey.generate()
    return bytes(k), bytes(k.public_key)


def key_id(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:8]


def seal(plaintext: bytes, public: bytes) -> bytes:
    try:
        return SealedBox(PublicKey(public)).encrypt(plaintext)
    except (CryptoError, ValueError, TypeError) as e:
        raise SealError(str(e)) from e


def open_sealed(blob: bytes, private: bytes) -> bytes:
    try:
        return SealedBox(PrivateKey(private)).decrypt(blob)
    except (CryptoError, ValueError, TypeError) as e:
        raise SealError("cannot open sealed blob") from e
