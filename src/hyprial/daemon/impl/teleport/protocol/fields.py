"""Strict header field validators shared by the protocol decoders.

Decisions:

- Every failure is ``TELEPORT_BAD_FRAME``.  ``bool`` is never accepted where a
  number is expected and vice versa, matching the slice 2 frame codec.
- Unknown extra header fields are ignored (forward compatibility); only the
  fields a decoder reads are validated.
- Identifier strings must be non-empty; the public key must be canonical
  padded base64 of exactly 32 bytes.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.teleport.errors import TeleportError

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEY_BYTES = 32


def bad(message: str) -> TeleportError:
    return TeleportError("TELEPORT_BAD_FRAME", message)


def need(header: Mapping[str, Any], name: str) -> Any:
    if name not in header:
        raise bad(f"missing {name}")
    return header[name]


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise bad(f"{name} must be a non-empty string")
    return value


def plain_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise bad(f"{name} must be a string")
    return value


def boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise bad(f"{name} must be a boolean")
    return value


def optional_text(value: Any, name: str) -> str | None:
    return None if value is None else text(value, name)


def digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise bad(f"{name} must be 64 lowercase hex digits")
    return value


def positive_seconds(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise bad(f"{name} must be a positive number")
    return float(value)


def sequence(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise bad(f"{name} must be an array")
    return value


def mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise bad(f"{name} must be an object")
    return value


def texts(value: Any, name: str, *, allow_empty_items: bool) -> tuple[str, ...]:
    items = sequence(value, name)
    check = plain_text if allow_empty_items else text
    return tuple(check(item, f"{name} item") for item in items)


def decode_public_key(value: Any, name: str) -> bytes:
    if not isinstance(value, str):
        raise bad(f"{name} must be a base64 string")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise bad(f"{name} is not valid base64") from exc
    if len(raw) != _KEY_BYTES or base64.b64encode(raw).decode("ascii") != value:
        raise bad(f"{name} must be a canonical 32-byte key")
    return raw


def encode_public_key(raw: bytes) -> str:
    if len(raw) != _KEY_BYTES:
        raise bad("recipient public key must be 32 bytes")
    return base64.b64encode(raw).decode("ascii")
