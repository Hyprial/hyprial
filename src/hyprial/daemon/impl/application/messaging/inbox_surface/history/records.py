"""The record side of ``conversations.list`` and ``conversations.history`` (plan slice RM1).

Decision notes (plan §11.1, R13, R15; brief RM1):

- The conversation record is the read model; the delivery queue supplies
  what the record does not hold yet (rows the asynchronous recorder has
  not reached), never held (rows before the recorder covered them) or
  refused (cell faults, and rows refused before placeholders existed; a
  row refused by redaction or size since slice S2 is a bodiless
  placeholder of the record).  The merge rule is ``conversations.merge``.
- The documents reader is built once per application from the
  conversation placement the recorder composition leaves on
  ``app._conversation_placement`` and kept on
  ``app._conversation_documents``, so its segment caches survive between
  requests.  It reads the owner homes of the same authority the queue
  heads key their cells under.
- A read never composes OrgFS and needs not: daemon start composes the
  OrgFS runtime in the delivery plane (``_require_orgfs_runtime`` inside
  ``_startup_compose_delivery_plane``, a failure fails the start) and the
  recorder with its placement right after, in
  ``_startup_adopt_domain_services``.  So after a restart the first read,
  before any new message, already reads the record on disk.
- Every failure to read the record (no placement because the recorder
  composition did not run, OrgFS not composed or failing, the placement
  closed) answers ``available`` False with no record data: the caller
  answers from the queue and publishes ``recordAvailable`` false.  The
  first failure after a success logs ``conversation.record_read_unavailable``
  and the first success after a failure logs
  ``conversation.record_read_resumed``, so a polling GUI does not flood the
  log.
- The conversations package is imported where it is used, so composing the
  application imports no conversations module.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any


def _documents(app: Any, authority: str) -> Any:
    from hyprial.daemon.impl.conversations import (
        RECORDS_DATABASE,
        ConversationDocuments,
        RecordUnavailable,
    )

    placement = getattr(app, "_conversation_placement", None)
    if placement is None:
        raise RecordUnavailable("no-placement")
    documents = getattr(app, "_conversation_documents", None)
    if documents is None or getattr(app, "_conversation_documents_for", None) is not placement:
        documents = ConversationDocuments(
            fs=placement.read_fs,
            spaces=lambda fs: placement.read_spaces(fs, authority),
            index=Path(app.state_dir) / RECORDS_DATABASE,
        )
        app._conversation_documents = documents
        app._conversation_documents_for = placement
    return documents


def _note(app: Any, available: bool, reason: str = "") -> None:
    failing = bool(getattr(app, "_conversation_record_failing", False))
    if failing == (not available):
        return
    app._conversation_record_failing = not available
    logger = getattr(app, "_log", None)
    if not callable(logger):
        return
    try:
        if available:
            logger("info", "conversations", "conversation.record_read_resumed")
        else:
            logger("warn", "conversations", "conversation.record_read_unavailable",
                   reason=reason[:200])
    except Exception:  # noqa: BLE001 - logging never breaks a read
        pass


def record_heads_for(
    app: Any,
    *,
    keys: Collection[str],
    thread_key: Callable[[str, str], Any],
    authority: str,
) -> tuple[tuple[Any, ...], bool]:
    """The caller's record-backed conversation heads and whether the record was read."""

    from hyprial.daemon.impl.conversations import record_heads, visible_messages

    try:
        cells = _documents(app, authority).known_cells()
    except Exception as error:  # noqa: BLE001 - the list answers from the queue
        _note(app, False, str(error) or type(error).__name__)
        return (), False
    _note(app, True)
    visible = {cell_id: visible_messages(entries, keys) for cell_id, entries in cells.items()}
    return record_heads(visible, keys=keys, thread_key=thread_key), True


def record_messages_for(
    app: Any,
    cell_id: str,
    *,
    keys: Collection[str],
    authority: str,
) -> tuple[tuple[Any, ...], bool]:
    """The caller's messages of one conversation record and whether it was read."""

    from hyprial.daemon.impl.conversations import visible_messages

    try:
        entries = _documents(app, authority).cell_entries(cell_id)
    except Exception as error:  # noqa: BLE001 - the history answers from the queue
        _note(app, False, str(error) or type(error).__name__)
        return (), False
    _note(app, True)
    return visible_messages(entries, keys), True


__all__ = ["record_heads_for", "record_messages_for"]
