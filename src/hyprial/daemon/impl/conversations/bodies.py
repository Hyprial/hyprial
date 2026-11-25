"""``ConversationBodies``: the delivery ``BodyResolver`` over conversation records.

Decision notes (phase A, S6; plan §1.1, §2.0 "Body hydration"):

- Delivery calls ``resolve(BodyRef)`` for a dropped row while it hydrates
  ``message.read_body`` and ``message.history``.  The ref is the
  ``RecordRef`` the recorder granted, so the resolver reads exactly one
  segment of one space and returns the recorded body bytes.
- A body is returned only when the entry is unredacted and both the entry's
  ``payloadSha256`` and the bytes' own digest equal the grant's digest;
  anything else answers None and delivery keeps the residue (its existing
  ``MESSAGE_BODY_UNAVAILABLE``).  Delivery checks the digest again.
- It never raises and never writes; with OrgFS unreachable a dropped body is
  unavailable until OrgFS returns.
"""

from __future__ import annotations

import hashlib

from hyprial.daemon.impl.conversations.record.contracts import RecordRef
from hyprial.daemon.impl.conversations.record.segments import SegmentStore
from hyprial.daemon.impl.delivery.ports import BodyRef


class ConversationBodies:
    """Resolves dropped delivery bodies from conversation records."""

    def __init__(self, segments: SegmentStore) -> None:
        self._segments = segments

    def resolve(self, ref: BodyRef) -> bytes | None:
        record = RecordRef.parse(ref.ref)
        if record is None:
            return None
        entry = self._segments.read(record)
        if entry is None or entry.redacted:
            return None
        if entry.payload_sha256 != ref.payload_sha256:
            return None
        if hashlib.sha256(entry.body).hexdigest() != ref.payload_sha256:
            return None
        return entry.body


__all__ = ["ConversationBodies"]
