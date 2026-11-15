"""Noise XX handshake and frame helpers for Muse's Hatch transport.

The wire format was observed from the muse.ai web client; see
``docs/PROTOCOL.md``. Only the primitives live here: the client in
``client.py`` owns sockets and request routing.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"
MAX_PLAINTEXT_FRAME = 65489


def _sha256(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int = 2) -> list[bytes]:
    prk = hmac.new(chaining_key, ikm, hashlib.sha256).digest()
    result: list[bytes] = []
    last = b""
    for index in range(1, outputs + 1):
        last = hmac.new(prk, last + bytes([index]), hashlib.sha256).digest()
        result.append(last)
    return result


def _dh(private: X25519PrivateKey, public_bytes: bytes) -> bytes:
    result = private.exchange(X25519PublicKey.from_public_bytes(public_bytes))
    if result == b"\0" * 32:
        raise ValueError("all-zero X25519 DH")
    return result


class Cipher:
    def __init__(self, key: bytes | None = None) -> None:
        self.key = key
        self.n = 0

    def enc(self, ad: bytes, plain: bytes) -> bytes:
        if self.key is None:
            return plain
        nonce = b"\0" * 4 + self.n.to_bytes(8, "big")
        self.n += 1
        return AESGCM(self.key).encrypt(nonce, plain, ad)

    def dec(self, ad: bytes, cipher: bytes) -> bytes:
        if self.key is None:
            return cipher
        nonce = b"\0" * 4 + self.n.to_bytes(8, "big")
        self.n += 1
        return AESGCM(self.key).decrypt(nonce, cipher, ad)


class Symmetric:
    def __init__(self) -> None:
        self.ck = PROTOCOL.ljust(32, b"\0")
        self.h = self.ck
        self.c = Cipher()
        self.mix_hash(b"")

    def mix_hash(self, value: bytes) -> None:
        self.h = _sha256(self.h + value)

    def mix_key(self, value: bytes) -> None:
        self.ck, key = _hkdf(self.ck, value, 2)
        self.c = Cipher(key)

    def encrypt_hash(self, plain: bytes) -> bytes:
        cipher = self.c.enc(self.h, plain)
        self.mix_hash(cipher)
        return cipher

    def decrypt_hash(self, cipher: bytes) -> bytes:
        plain = self.c.dec(self.h, cipher)
        self.mix_hash(cipher)
        return plain

    def split(self) -> tuple[Cipher, Cipher]:
        first, second = _hkdf(self.ck, b"", 2)
        return Cipher(first), Cipher(second)


class NoiseXX:
    """Initiator side of Noise_XX_25519_AESGCM_SHA256.

    Muse's VM expects the first message to carry a protobuf field 1 holding a
    32-byte client nonce. The reference web client does the same.
    """

    def __init__(self) -> None:
        self.s = Symmetric()
        self.client_nonce = secrets.token_bytes(32)
        self.e: X25519PrivateKey
        self.re: bytes
        self.rs: bytes

    def msg1(self) -> bytes:
        self.e = X25519PrivateKey.generate()
        ephemeral = self.e.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.s.mix_hash(ephemeral)
        return ephemeral + self.s.encrypt_hash(b"\x0a\x20" + self.client_nonce)

    def read_msg2(self, message: bytes) -> bytes:
        if len(message) < 96:
            raise ValueError("short Noise message 2")
        self.re = message[:32]
        self.s.mix_hash(self.re)
        self.s.mix_key(_dh(self.e, self.re))
        self.rs = self.s.decrypt_hash(message[32:80])
        self.s.mix_key(_dh(self.e, self.rs))
        return self.s.decrypt_hash(message[80:])

    def msg3(self, payload: bytes = b"") -> bytes:
        static = X25519PrivateKey.generate()
        public = static.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        encrypted_static = self.s.encrypt_hash(public)
        self.s.mix_key(_dh(static, self.re))
        return encrypted_static + self.s.encrypt_hash(payload)

    def split(self) -> tuple[Cipher, Cipher]:
        return self.s.split()


@dataclass
class _Parts:
    total: int
    chunks: dict[int, bytes]


class Reassembler:
    """Reassemble ``NoiseTransportFrame`` chunks by ``chunk_id``."""

    def __init__(self) -> None:
        self.parts: dict[int, _Parts] = {}

    def push(self, chunk_id: int, chunk_index: int, total_chunks: int,
             payload: bytes) -> bytes | None:
        if not 1 <= total_chunks <= 256:
            raise ValueError("invalid chunks")
        if chunk_index >= total_chunks:
            raise ValueError("invalid chunk index")
        if len(payload) > MAX_PLAINTEXT_FRAME:
            raise ValueError("chunk too large")
        entry = self.parts.setdefault(chunk_id, _Parts(total_chunks, {}))
        if entry.total != total_chunks:
            raise ValueError("inconsistent chunks")
        if chunk_index in entry.chunks:
            raise ValueError("duplicate chunk")
        entry.chunks[chunk_index] = payload
        if len(entry.chunks) != entry.total:
            return None
        del self.parts[chunk_id]
        return b"".join(entry.chunks[i] for i in range(entry.total))
