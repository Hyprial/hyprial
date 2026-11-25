"""The restart pass of group delivery (slice GC4).

Decision notes (group chat design draft §4.3; brief GC4 item 2):

- A daemon that stopped between writing a group message and creating its
  rows creates them at the next start: ``start_group_catch_up`` runs the
  application's ``deliver_recent`` pass once, on its own thread, so the
  OrgFS reads never hold up startup.  Live entries are delivered where
  they are written (``inbox_surface.groups.messages.append_message``);
  nothing else watches the documents on one machine.
- The pass needs the group documents readable: until OrgFS is composed or
  while a read fails, it is retried after 1 s doubling to 60 s, until one
  pass completes or the daemon closes.  Repeats are harmless: the inbox's
  dedup answers duplicate for every row already created.
- It starts with the recorder composition (``start=True``), whatever the
  recorder switch says, and ``close_group_catch_up`` (called by the
  ``conversation-recorder`` shutdown step) stops and joins it.
"""

from __future__ import annotations

import threading
from typing import Any

CATCH_UP_THREAD = "hyprial-group-catch-up"
CATCH_UP_CLOSE_TIMEOUT_S = 5.0
_FIRST_DELAY_S = 1.0
_MAX_DELAY_S = 60.0


def _log(app: Any, level: str, event: str, **fields: Any) -> None:
    try:
        app._log(level, "conversations", event, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks the pass
        pass


def _run(app: Any, stop: threading.Event) -> None:
    from hyprial.daemon.impl.application.messaging.inbox_surface.groups import deliver_recent

    delay = _FIRST_DELAY_S
    while not stop.is_set():
        try:
            delivered = deliver_recent(app)
        except Exception as error:  # noqa: BLE001 - unreadable now; retried
            _log(app, "info", "conversation.group_catch_up_delayed",
                 errorType=type(error).__name__, retryInS=delay)
            stop.wait(delay)
            delay = min(delay * 2, _MAX_DELAY_S)
            continue
        _log(app, "info", "conversation.group_catch_up_done", delivered=delivered)
        return


def start_group_catch_up(app: Any) -> threading.Thread | None:
    """Start the restart pass once per application; None when it already started."""

    if getattr(app, "_group_catch_up", None) is not None:
        return None
    stop = threading.Event()
    thread = threading.Thread(target=_run, args=(app, stop), name=CATCH_UP_THREAD, daemon=True)
    app._group_catch_up = (thread, stop)
    thread.start()
    return thread


def close_group_catch_up(app: Any) -> None:
    """Stop and join the restart pass; idempotent."""

    started = getattr(app, "_group_catch_up", None)
    if started is None:
        return
    thread, stop = started
    stop.set()
    thread.join(CATCH_UP_CLOSE_TIMEOUT_S)


__all__ = ["CATCH_UP_THREAD", "close_group_catch_up", "start_group_catch_up"]
