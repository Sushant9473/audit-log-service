"""Opaque, signed pagination cursors for the List API.

A cursor is base64url(JSON) + "." + base64url(HMAC-SHA256 of that JSON, keyed with CURSOR_SECRET).
The signature stops a client from editing a cursor (its position or snapshot). The tenant and
filters inside it are compared with the request, so another tenant or query can't reuse it.
"""

import base64
import hashlib
import hmac
import json
import os


class InvalidCursor(Exception):
    pass


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> bytes:
    return hmac.new(os.environ["CURSOR_SECRET"].encode(), payload, hashlib.sha256).digest()


def encode(state: dict) -> str:
    payload = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    return f"{_b64encode(payload)}.{_b64encode(_sign(payload))}"


def decode(cursor: str) -> dict:
    try:
        payload_part, signature_part = cursor.split(".")
        payload, signature = _b64decode(payload_part), _b64decode(signature_part)
    except ValueError:
        raise InvalidCursor("malformed cursor")
    # compare_digest takes the same time whether the first or last byte differs
    if not hmac.compare_digest(signature, _sign(payload)):
        raise InvalidCursor("cursor signature doesn't match")
    return json.loads(payload)
