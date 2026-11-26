# Decision record (service grant S1, 2026-11-26):
# - A device publishes the public half of its encryption key in its own
#   directory row and in nothing else.  The private half never leaves the key
#   store; this module only shapes what the store already published as public.
# - The reader is injected, exactly like ``read_own_address``: the service
#   stays testable over a recording file system and the production reader is
#   ``encryption_key_reader``.
# - Tri-state outcome (S1 fix, 2026-11-26): distinguish "definitely no key"
#   (``None``) from "temporarily unavailable" (``KEY_UNAVAILABLE`` or a raising
#   reader).  When temporarily unavailable, ``published_pair`` retains the pair
#   already on the device's current directory row so a busy lock or transient
#   I/O error never erases a published public key; on a row without an existing
#   pair it returns ``{}`` and never fails the publish.  Only ``None`` produces
#   a row without the pair when a pair was previously published.
# - The answer is shaped into the keyword arguments of ``DirectoryDevice`` so
#   that a row without a key is built exactly as before, with no new key.
# - The service settles whether this device may write its row in the org
#   before it asks for the key, so a publish that is refused never touches the
#   key store.  Otherwise a daemon whose principal was unbound would still
#   read, and possibly create, a key on every refresh of every org it lists.
"""Shape the local encryption key for this device's own directory row."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["KEY_UNAVAILABLE", "published_pair"]


class _KeyUnavailable:
    """Sentinel returned when the local encryption key cannot be read right now."""

    def __repr__(self) -> str:
        return "KEY_UNAVAILABLE"


KEY_UNAVAILABLE = _KeyUnavailable()


def _retained_pair(current: Any) -> dict[str, str]:
    if current is None:
        return {}
    key_id = getattr(current, "encryption_key_id", None)
    public_key = getattr(current, "encryption_public_key", None)
    if isinstance(key_id, str) and isinstance(public_key, str):
        return {"encryption_key_id": key_id, "encryption_public_key": public_key}
    return {}


def published_pair(
    reader: Callable[[], Any] | None,
    *,
    current: Any = None,
) -> dict[str, str]:
    """Return the ``DirectoryDevice`` keyword arguments for the published pair.

    ``None`` means the device definitely has no key, producing ``{}``.
    ``KEY_UNAVAILABLE`` or an exception from ``reader`` means the key could not
    be read right now, so any pair already published on ``current`` is kept
    intact instead of being erased.
    """

    if reader is None:
        return {}
    try:
        record = reader()
    except Exception:  # noqa: BLE001 - a raising reader is temporarily unavailable
        return _retained_pair(current)
    if record is KEY_UNAVAILABLE:
        return _retained_pair(current)
    if record is None or not isinstance(record, Mapping):
        return {}
    key_id, public_key = record.get("keyId"), record.get("publicKey")
    if not isinstance(key_id, str) or not isinstance(public_key, str):
        return {}
    return {"encryption_key_id": key_id, "encryption_public_key": public_key}
