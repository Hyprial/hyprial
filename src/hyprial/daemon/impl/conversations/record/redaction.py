"""The redaction hook applied to every draft before it is written (plan §2.6).

Decision notes (phase A, S6):

- The hook is pluggable and runs on the recorder thread before any OrgFS
  write.  Phase A's default, ``no_redaction``, returns every draft unchanged.
- A hook that returns None skips the row: nothing is written and the body
  stays in the local queue.  A hook that changes ``body`` gets a record
  marked ``redacted``; such a record can never hydrate the original, so
  its row is never granted and keeps its body too.
- A raising hook is treated like a skip, never like "write it anyway":
  redaction exists to keep content out of shared spaces.
"""

from __future__ import annotations

from hyprial.daemon.impl.conversations.record.contracts import RecordDraft


def no_redaction(draft: RecordDraft) -> RecordDraft | None:
    """The phase A default: record every draft as it is."""

    return draft


__all__ = ["no_redaction"]
