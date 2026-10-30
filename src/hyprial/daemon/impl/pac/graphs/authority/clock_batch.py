"""Clock batch execution for one tick.

Extracted from ``generation`` to keep that module inside the 1000-line gate;
the behaviour, and the reason for it, live here.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.graphs.authority.commands import ClockTickMany

CLOCK_BATCH_FAILURE_STREAK = 3
"""Consecutive identical failures that make a clock batch doomed."""


def run_clock_batch(authority: Any, command: ClockTickMany) -> dict[str, Any]:
    """Tick a batch of clock jobs with per-graph rollback.

    Two savings are kept separable.  ``shared_connection`` removes the
    per-graph store connect/close; the single round trip that carries this
    command removes the per-graph mailbox hop.  Failure policy is
    per-graph by default (production shape): a bad graph rolls back only
    itself, and the batch continues.  ``stop_on_error`` is for tests and
    API callers after a deterministic ordering.
    """

    results: list[dict[str, Any]] = []
    aborted_after: int | None = None
    abort_reason: str | None = None
    streak = 0
    last_code: str | None = None
    notifications = 0
    shared = command.shared_connection
    shared_store = PacGraphStore(authority.database) if shared else None
    if shared_store is not None:
        shared_store.hook_publisher = authority.owner.hook_publisher
    try:
        for graph_id in command.graph_ids:
            store = shared_store
            if store is None:
                store = PacGraphStore(authority.database)
                store.hook_publisher = authority.owner.hook_publisher
            try:
                # Each graph keeps its own transaction on the shared
                # connection: the reactor owns BEGIN/COMMIT, so a failure
                # rolls back that graph alone and leaves its predecessors
                # committed instead of discarding the batch.
                planned = PacReactor(store).tick_clocks(graph_id)
            except PacError as error:
                # A missing or invalid graph is that graph's problem: it must
                # not feed the systemic counter (three broken graphs in a row
                # are not evidence that the batch is doomed).
                results.append(
                    {"graphId": graph_id, "ok": False, "code": error.code}
                )
                streak = 0
                last_code = None
            except Exception as error:  # noqa: BLE001 - isolate ONE graph, never the batch
                # Per-graph isolation is the production promise: the old
                # per-graph loop caught Exception, so a corrupt launch document
                # (JSONDecodeError) or an unexpected shape (TypeError) rolled
                # back only its own graph. Catching only the database errors
                # would let one such graph leave every later, healthy graph
                # unticked for that tick.
                results.append({
                    "graphId": graph_id, "ok": False,
                    "code": "PAC_CLOCK_TICK_FAILED",
                    "detail": type(error).__name__,
                })
                # Only a store that cannot be USED dooms the batch: locked,
                # I/O error, corrupt image, unable to open. A rejected row is
                # still that graph's problem -- three IntegrityErrors in a row
                # (journal trigger, constraint) say nothing about the graphs
                # behind them.
                if isinstance(error, (sqlite3.OperationalError, OSError)):
                    streak = streak + 1 if last_code == "PAC_CLOCK_TICK_FAILED" else 1
                    last_code = "PAC_CLOCK_TICK_FAILED"
                else:
                    streak = 0
                    last_code = None
            else:
                results.append(
                    {"graphId": graph_id, "ok": True, "notifications": len(planned)}
                )
                notifications += len(planned)
                streak = 0
                last_code = None
            finally:
                if not shared:
                    store.close()
            if command.stop_on_error and not results[-1]["ok"]:
                break
            if streak >= CLOCK_BATCH_FAILURE_STREAK:
                # K identical failures in a row mean the batch is doomed,
                # not unlucky: stop and report the skipped tail as
                # unattempted instead of multiplying cost and log noise.
                aborted_after = len(results)
                abort_reason = last_code
                break
    finally:
        if shared_store is not None:
            shared_store.close()
    if notifications:
        authority.wake_delivery()
    attempted = len(results)
    return {
        "ok": True,
        "graphs": len(command.graph_ids),
        "attempted": attempted,
        "results": results,
        "notifications": notifications,
        "unattempted": len(command.graph_ids) - attempted,
        "aborted_after": aborted_after,
        "abort_reason": abort_reason,
    }
