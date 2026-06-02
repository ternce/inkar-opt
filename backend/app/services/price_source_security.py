from __future__ import annotations

import base64
import hashlib
import os


def _key(secret: str | None) -> bytes:
    raw = (secret or os.getenv("PRICE_SOURCE_SECRET") or "aptekaopt-local-dev-key").encode("utf-8")
    return hashlib.sha256(raw).digest()


def encrypt_secret(value: str, secret: str | None = None) -> str:
    data = value.encode("utf-8")
    key = _key(secret)
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.urlsafe_b64encode(out).decode("ascii")


def decrypt_secret(value: str, secret: str | None = None) -> str:
    if not value:
        return ""
    data = base64.urlsafe_b64decode(value.encode("ascii"))
    key = _key(secret)
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return out.decode("utf-8")
