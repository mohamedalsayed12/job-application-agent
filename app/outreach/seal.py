"""Seal Gmail refresh tokens at rest with a secret-derived key (stdlib AEAD-style)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


def seal(secret: str, plaintext: str) -> str:
    if not secret:
        raise ValueError("seal secret is empty")
    key = hashlib.sha256(f"jaa-gmail:{secret}".encode()).digest()
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    stream = _stream(key, nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream, strict=True))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")


def unseal(secret: str, sealed: str) -> str:
    if not secret:
        raise ValueError("seal secret is empty")
    raw = base64.urlsafe_b64decode(sealed.encode("ascii"))
    if len(raw) < 48:
        raise ValueError("sealed token is truncated")
    nonce, tag, cipher = raw[:16], raw[16:48], raw[48:]
    key = hashlib.sha256(f"jaa-gmail:{secret}".encode()).digest()
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("sealed token is altered")
    stream = _stream(key, nonce, len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, stream, strict=True)).decode("utf-8")


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:length])
