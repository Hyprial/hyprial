# Decision record (service grant S1, 2026-11-26):
# - A device publishes the public half of its encryption key in its own
#   directory row and in nothing else.  The private half never leaves the key
#   store; this module only shapes what the store already published as public.
# - The reader is injected, exactly like ``read_own_address``: the service
#   stays testable over a recording file system and the production reader is
#   ``encryption_key_reader``.
# - A reader that raises is the same as a reader without a key.  The reader is
#   the daemon's own code, and a refusal there (a quarantined or unsafe store)
#   must not stop the device from registering in the org.
# - The answer is shaped into the keyword arguments of ``DirectoryDevice`` so
#   that a row without a key is built exactly as before, with no new key.
# - The service settles whether this device may write its row in the org
#   before it asks for the key, so a publish that is refused never touches the
#   key store.  Otherwise a daemon whose principal was unbound would still
#   read, and possibly create, a key on every refresh of every org it lists.
"""Shape the local encryption key for this device's own directory row."""

from __future__ import annotations

from collections.abc import Callable, Mapping

__all__ = ["published_pair"]


def published_pair(
    reader: Callable[[], Mapping[str, str] | None] | None,
) -> dict[str, str]:
    """Return the ``DirectoryDevice`` keyword arguments for the published pair.

    An empty mapping means the row carries neither key.  The mapping is
    read once per publish, so a publish never mixes two reads of the store.
    """

    if reader is None:
        return {}
    try:
        record = reader()
    except Exception:  # noqa: BLE001 - a raising reader publishes no pair
        return {}
    if not isinstance(record, Mapping):
        return {}
    key_id, public_key = record.get("keyId"), record.get("publicKey")
    if not isinstance(key_id, str) or not isinstance(public_key, str):
        return {}
    return {"encryption_key_id": key_id, "encryption_public_key": public_key}
