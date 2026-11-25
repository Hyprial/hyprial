"""Compose the conversation recorder of a daemon generation (plan §2.6, S6).

Decision notes (phase A, S6; S5 and S7a open issues):

- ``compose_conversation_recorder(app)`` runs once at daemon start, after
  the link service.  It never raises: without the delivery facade
  operations on ``app._inbox`` it logs ``conversation.recorder_unavailable``
  and returns None.
- The resolver comes first and stands alone: ``ConversationBodies`` needs
  only the placement and the segment reader, so it is bound with
  ``app._inbox.bind_body_retention(resolver=...)`` before, and
  independently of, the local ``conversation-records.sqlite3``.  A corrupt
  or unopenable records database stops new records (and so new drops) but
  never makes an already dropped body unavailable.
- Revertible switch: ``HYPRIAL_CONVERSATION_RECORDER`` set to ``0``,
  ``off``, ``false`` or ``no`` keeps the resolver bound but composes no
  recorder, so nothing new is granted and S5 drops nothing new; bodies
  dropped by an earlier run still hydrate.  Unset (or any other value) the
  recorder runs, which is the phase A default (plan §3 "Drop gating").
- The recording authority is reached through ``placement.authority()`` only
  by a pass that has a row to record or a grant to retry, so an idle daemon
  composes nothing; the recorder never touches the cell runtime (a
  conversation is not a cell).  A grant still resolves its per-agent
  retention through ``cell_params(app)``, which composes the cell runtime
  on the recorder thread as before, never on a delivery path; without it
  the code default applies.
- Retention is resolved per row with S7a's resolver:
  ``resolve_param(cell_params(app), DELIVERY_BODY_RETENTION_MS, subject)``,
  the subject being the local agent of the row (the inbox recipient, the
  sender of a sender copy), never None.  ``retention_for=`` replaces it.
- Threads come from ``app._conversation_stamp``.  It answers None both for a
  pair with no thread and when the thread authority failed, so a None is
  confirmed against ``threads_of(app)``: a raise there is
  ``RecordUnavailable`` (the row is retried), a None is a real unthreaded
  row (skipped, body kept).
- UA2: the recorder's observer is ``UserRowConsumer``, which acknowledges a
  recorded row addressed to ``user:<owner>`` (``user_rows``).
- ``close_conversation_recorder`` is the ``conversation-recorder`` shutdown
  step and runs before ``cell-runtime`` and ``inbox``; it marks the app
  closed first, so a late call cannot start a second recorder, and closes
  the placement, so the still-bound resolver answers None instead of
  reviving OrgFS after shutdown cleared it.
- Slice GC4: with ``start``, composition also starts group delivery's
  restart pass (``group_delivery``) once the placement is set, whatever the
  recorder switch says; ``close_conversation_recorder`` stops it.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.params import DELIVERY_BODY_RETENTION_MS, resolve_param
from hyprial.daemon.impl.composition.cells import cell_params
from hyprial.daemon.impl.composition.conversations.group_delivery import (
    close_group_catch_up,
    start_group_catch_up,
)
from hyprial.daemon.impl.composition.conversations.placement import OwnerHomePlacement
from hyprial.daemon.impl.composition.conversations.user_rows import UserRowConsumer
from hyprial.daemon.impl.conversations import (
    RECORDS_DATABASE,
    ConversationBodies,
    ConversationRecorder,
    RecordStore,
    RecordUnavailable,
    Redaction,
    RetentionFor,
    SegmentStore,
    direct_key,
    no_redaction,
)
from hyprial.daemon.impl.delivery.ports import SOURCE_INBOX, InteractionRow

RECORDER_CLOSE_TIMEOUT_S = 5.0
RECORDER_ENV = "HYPRIAL_CONVERSATION_RECORDER"
_RECORDER_OFF = frozenset({"0", "off", "false", "no"})
FACADE_OPERATIONS = (
    "interaction_rows_after",
    "interaction_row_at",
    "grant_body_release",
    "bind_body_retention",
)
_GUARD = threading.Lock()


def _app_logger(app: Any) -> Callable[..., object] | None:
    logger = getattr(app, "_log", None)
    return logger if callable(logger) else None


def _log(logger: Callable[..., object] | None, level: str, event: str, **fields: Any) -> None:
    if logger is None:
        return
    try:
        logger(level, "conversations", event, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks composition
        pass


def default_retention(app: Any) -> RetentionFor:
    """Per-row ``delivery.bodyRetentionMs`` for the row's local agent (S7a)."""

    def retention_for(row: InteractionRow) -> int | None:
        message = row.message
        subject = message.recipient if row.source == SOURCE_INBOX else message.sender
        if not subject:
            return None
        value = resolve_param(cell_params(app), DELIVERY_BODY_RETENTION_MS, subject)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return retention_for


def recorder_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """False when ``HYPRIAL_CONVERSATION_RECORDER`` switches the recorder off."""

    env = os.environ if environ is None else environ
    return str(env.get(RECORDER_ENV, "")).strip().lower() not in _RECORDER_OFF


def _thread_ids(app: Any) -> Callable[[str, str], str | None]:
    derive = getattr(app, "_conversation_stamp", None)

    def thread_id_for(sender: str, recipient: str) -> str | None:
        if callable(derive):
            found = derive(sender, recipient)
            if isinstance(found, str) and found:
                return found
        try:
            key = direct_key(sender, recipient)
        except Exception as error:  # noqa: BLE001 - an unavailable authority only delays
            raise RecordUnavailable("thread-authority") from error
        return None if key is None else key.id

    return thread_id_for


def _version(inbox: Any) -> Callable[[], int]:
    return lambda: int(getattr(inbox, "version", 0))


def compose_conversation_recorder(
    app: Any,
    *,
    fs: Any = None,
    clock_ms: Callable[[], int] | None = None,
    start: bool = True,
    retention_for: RetentionFor | None = None,
    redact: Redaction | None = None,
    logger: Callable[..., object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ConversationRecorder | None:
    """The app's conversation recorder, composed once; never raises."""

    log = logger if logger is not None else _app_logger(app)
    with _GUARD:
        existing = getattr(app, "_conversation_recorder", None)
        if existing is not None:
            return existing
        if getattr(app, "_conversation_recorder_closed", False):
            return None
        inbox = getattr(app, "_inbox", None)
        missing = [name for name in FACADE_OPERATIONS if not callable(getattr(inbox, name, None))]
        if missing:
            _log(log, "info", "conversation.recorder_unavailable", reason="no-delivery-facade",
                 missing=missing)
            return None
        try:
            placement = OwnerHomePlacement(app, fs=fs)
            segments = SegmentStore(
                fs=placement.fs,
                space_for=placement.write_space,
                writer=str(getattr(app, "node_id", "") or "local"),
            )
            inbox.bind_body_retention(resolver=ConversationBodies(segments))
        except Exception as error:  # noqa: BLE001 - composition never breaks startup
            _log(log, "warn", "conversation.recorder_unavailable", reason="resolver-failed",
                 errorType=type(error).__name__, detail=str(error)[:300])
            return None
        app._conversation_placement = placement
        if start:
            start_group_catch_up(app)
        if not recorder_enabled(environ):
            _log(log, "info", "conversation.recorder_disabled", switch=RECORDER_ENV)
            return None
        try:
            store = RecordStore(Path(app.state_dir) / RECORDS_DATABASE)
        except Exception as error:  # noqa: BLE001 - composition never breaks startup
            _log(log, "warn", "conversation.recorder_unavailable", reason="records-db-failed",
                 errorType=type(error).__name__, detail=str(error)[:300])
            return None
        options: dict[str, Any] = {} if clock_ms is None else {"clock_ms": clock_ms}
        recorder = ConversationRecorder(
            feed=inbox,
            authority=placement.authority,
            segments=segments,
            store=store,
            thread_id_for=_thread_ids(app),
            retention_for=retention_for or default_retention(app),
            redact=redact or no_redaction,
            logger=log,
            version=_version(inbox),
            observer=UserRowConsumer(app),
            **options,
        )
        app._conversation_recorder = recorder
    if start:
        recorder.start()
    return recorder


def close_conversation_recorder(app: Any) -> None:
    """Mark the app closed, close the placement and stop its recorder; idempotent."""

    with _GUARD:
        app._conversation_recorder_closed = True
        recorder = getattr(app, "_conversation_recorder", None)
        app._conversation_recorder = None
        placement = getattr(app, "_conversation_placement", None)
        if isinstance(placement, OwnerHomePlacement):
            placement.close()
    close_group_catch_up(app)
    if recorder is not None and not recorder.close(RECORDER_CLOSE_TIMEOUT_S):
        raise RuntimeError("conversation recorder did not stop before deadline")


__all__ = [
    "FACADE_OPERATIONS",
    "RECORDER_CLOSE_TIMEOUT_S",
    "RECORDER_ENV",
    "close_conversation_recorder",
    "compose_conversation_recorder",
    "default_retention",
    "recorder_enabled",
]
