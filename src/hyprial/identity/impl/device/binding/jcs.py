"""RFC 8785 JCS canonicalization for the restricted binding-claim profile.

Only the two encodings the profile needs are supported: UTF-8 strings and JSON
integers.  Anything else is rejected instead of guessed, so no float, boolean or
implementation-specific number formatting can reach a digest.
"""

from __future__ import annotations

from collections.abc import Mapping

MAX_SAFE_INTEGER = 9007199254740991

_SHORT_ESCAPES = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
}


class JcsError(ValueError):
    """A schema-restricted canonicalization rejection."""


def canonical_bytes(value: Mapping[str, object]) -> bytes:
    """Return RFC 8785 canonical UTF-8 bytes for one claim object."""

    return _object(value).encode("utf-8")


def _object(value: Mapping[str, object]) -> str:
    if not isinstance(value, Mapping):
        raise JcsError("canonical value must be an object")
    keys = list(value)
    for key in keys:
        if not isinstance(key, str) or not key:
            raise JcsError("canonical keys must be non-empty strings")
        _check_text(key)
    keys.sort(key=_utf16_order)
    parts = [f'"{_string(key)}":{_member(value[key])}' for key in keys]
    return "{" + ",".join(parts) + "}"


def _member(value: object) -> str:
    if isinstance(value, str):
        return f'"{_string(value)}"'
    if isinstance(value, bool) or not isinstance(value, int):
        raise JcsError("canonical member must be a string or an integer")
    if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        raise JcsError("canonical integer exceeds the interoperable range")
    return str(value)


def _utf16_order(key: str) -> bytes:
    return key.encode("utf-16-be")


def _check_text(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise JcsError("canonical text is not valid Unicode") from None


def _string(value: str) -> str:
    _check_text(value)
    parts: list[str] = []
    for character in value:
        code = ord(character)
        if character == '"':
            parts.append('\\"')
        elif character == "\\":
            parts.append("\\\\")
        elif code < 0x20:
            parts.append(_SHORT_ESCAPES.get(code, f"\\u{code:04x}"))
        else:
            parts.append(character)
    return "".join(parts)
