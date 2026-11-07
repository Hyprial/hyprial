"""AT07 I5: the chain's call-boundary crash windows, on two real daemons.

The card's debt was that only one of the six windows had ever been injected.
The reachability study (``.artifacts/p2-c3/at07-i5-window-reachability-001.md``)
split them by shape: the land->activate window lives inside one call, so it is
measured in the driver's own rig (``p2/target/test_target_landing.py``); the
others are *call boundaries*, where a real process goes away between two IPC
calls and the chain is entered again.  This file measures those, and re-entering
it found the thing worth knowing:

**The chain's first step is not re-enterable, by design, and it says so.**
``transfer.quiesce`` mirrors ``down``: it stops the worker and *drops the
desired-state entry* (``transfer.py:187-195``), so a second call cannot find the
spec and is refused with ``TRANSFER_WORKER_NOT_FOUND`` -- the P2 branch is never
reached, the journal is never consulted.  That is a typed, readable refusal and
not a wedge, and it is what makes the honest re-entry point for a crashed
orchestrator ``transfer.receive`` + ``transfer.complete`` (or the audited
``transfer.custody.override`` + ``transfer.resume`` path), never a second
``transfer.quiesce``.  Every arm below reads it that way.

The audited recovery path itself -- a ``custody.override`` plus a ``resume``
landing on an ``activate-pending`` half -- cannot be produced from out here:
that boundary is inside one ``transfer.receive`` call, so no kill between two
IPC calls leaves the row.  The reading needs both owners in one process and is
taken in ``p2/recovery/test_pending_activation_recovery.py``; this file keeps
the windows a real daemon boundary can produce.

The invariants each arm asserts, from durable witnesses rather than from a
receipt the chain vouched for itself:

1. **receipt-once** -- re-entering writes no second row for the operation on
   either daemon (the ledgers are counted, not the responses);
2. **anti-dual-active** -- the source's worker stays down and the target's is not
   running before anyone launches it;
3. **no silent destruction** -- before completion the source still owns its own
   incarnation; the crash never retired it.

The helpers are imported from the I4 file on purpose: this has to be the *same*
harness, or the readings are not comparable with the ones it already carries.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .conftest import SOURCE_NODE, WORKER
from .test_two_daemon_chain import (
    PLAN_DIGEST,
    entity_token,
    ipc_raw,
    p2_params,
    running,
    seed_worker,
    ship,
    worker_spec,
)

BUNDLE_NAME = "at07-window-bundle"


def _rows(node, operation: str) -> list[tuple]:
    """This daemon's own ledger rows for the operation -- the durable witness."""

    with sqlite3.connect(node.state / "lifecycle-operations.sqlite3") as conn:
        return conn.execute(
            "SELECT operation_id, actor, phase, step, generation "
            "FROM transfer_p2_steps WHERE operation_id=? ORDER BY generation",
            (operation,),
        ).fetchall()


def _up(node) -> None:
    """Boot it only if it is not already up; ``start_daemon`` asserts on a live one."""

    if node.daemon is None:
        node.start_daemon()


def _open_chain(source, operation: str, harness: str = "pi") -> str:
    """Seed, boot and quiesce: the state every window below starts from."""

    seed_worker(source, harness=harness)
    _up(source)
    token = entity_token(source)
    quiesce = source.ipc("transfer.quiesce", p2_params(operation, harness))
    assert quiesce["stopped"] is True
    assert quiesce["chain"]["quiesce"]["state"] == "quiesced"
    return token


def _export(source, target, bundle: Path, harness: str = "pi") -> None:
    """One honest config artifact, then the bundle -- the export step alone.

    ``ship`` also stamps the source's signature and enrolls its key in the
    target's own home, so re-entry reads a bundle the target can attribute.
    """

    ship(source, target, bundle, harness)


def _refusal(node, method: str, params: dict) -> dict:
    """The typed error a refused call carries, without raising it."""

    document = ipc_raw(node, method, params)
    error = document.get("error")
    assert isinstance(error, dict), document
    return error


def _receive(
    target, source, operation: str, bundle: Path, harness: str = "pi"
) -> dict:
    """The target's own entry; it needs nothing but rows, so a dead source is fine."""

    _up(target)
    received = target.ipc(
        "transfer.receive",
        {
            "operationId": operation,
            "planDigest": PLAN_DIGEST,
            "sourceNode": SOURCE_NODE,
            "sourceActor": WORKER,
            "sourceEntityToken": entity_token(source),
            "targetActor": WORKER,
            "bundleDir": str(bundle),
            "spec": worker_spec(target, harness),
        },
    )
    assert received["ok"] is True
    return received


def _complete(source, operation: str, report: dict, harness: str = "pi") -> dict:
    _up(source)
    return source.ipc(
        "transfer.complete",
        {
            "name": WORKER,
            "provider": harness,
            "operationId": operation,
            "chain": {"report": report},
        },
    )


def _assert_receipt_once(source, target, operation: str) -> None:
    assert _rows(source, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ], "the source wrote a second row for one operation"
    assert _rows(target, operation) == [
        (operation, WORKER, "landed", "land", 1),
        (operation, WORKER, "active", "activate", 2),
    ], "the target wrote a second row for one landing"


def _assert_the_first_step_is_not_re_enterable(
    source, operation: str, harness: str = "pi"
) -> None:
    """The reading above, pinned: step one refuses by name and reaches no journal."""

    _up(source)
    error = _refusal(source, "transfer.quiesce", p2_params(operation, harness))
    assert error["code"] == "TRANSFER_WORKER_NOT_FOUND", error
    assert _rows(source, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ], "a refused re-entry still wrote a row"


def test_w2_w3_the_export_boundary_re_enters_after_the_first_step(
    source_node, target_node, tmp_path, harness
) -> None:
    """W2/W3 collapsed, and that collapse is itself the reading.

    The plan separates "after the export" from "after the ship".  In a
    same-filesystem harness the ship *is* a path, so both windows leave the
    identical durable state: a bundle on disk, an empty target ledger.  A real
    remote transport is what would separate them, and that is NOT_TESTED here.
    """

    operation = "at07-w2w3"
    token = _open_chain(source_node, operation, harness)
    source_node.stop_daemon()          # the orchestrator died with the bundle
    bundle = tmp_path / BUNDLE_NAME
    _export(source_node, target_node, bundle, harness)

    # the window, read: nothing was destroyed and the target has heard nothing
    assert _rows(source_node, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ]
    target_node.start_daemon()
    assert _rows(target_node, operation) == []
    assert entity_token(source_node) == token
    assert running(target_node) is False

    # re-entry, reading one: step one is a typed refusal, not a second quiesce
    _assert_the_first_step_is_not_re_enterable(source_node, operation, harness)

    # re-entry, reading two: from the target's entry on, the chain still closes.
    # The export stages a *fresh* directory -- the exporter refuses a used one
    # ("export never removes a destination"), which is why a re-entering
    # orchestrator stages again rather than re-using its failed attempt's dir.
    again = tmp_path / f"{BUNDLE_NAME}-again"
    _export(source_node, target_node, again, harness)
    received = _receive(target_node, source_node, operation, again, harness)
    assert received["replayed"] is False
    completed = _complete(source_node, operation, received["chain"]["report"], harness)

    assert completed["removedAgent"] is True
    assert completed["chain"]["action"] == "complete"
    _assert_receipt_once(source_node, target_node, operation)
    assert running(source_node) is False
    assert running(target_node) is False


def test_w5_after_the_target_answered_both_daemons_restart_and_the_report_holds(
    source_node, target_node, tmp_path, harness
) -> None:
    """The window the plan says is covered by the response body -- read again."""

    operation = "at07-w5"
    token = _open_chain(source_node, operation, harness)
    source_node.stop_daemon()
    bundle = tmp_path / BUNDLE_NAME
    _export(source_node, target_node, bundle, harness)
    first = _receive(target_node, source_node, operation, bundle, harness)
    report = first["chain"]["report"]
    assert first["replayed"] is False

    # both daemons go away here; the orchestrator has the report but no receipt
    target_node.stop_daemon()

    # a restarted target must serve the same half from its own durable rows
    again = _receive(target_node, source_node, operation, bundle, harness)
    assert again["replayed"] is True
    assert again["chain"]["report"] == report
    assert again["entityToken"] == first["entityToken"]

    # and the source is still held by its own row, not by anything it remembered
    assert _rows(source_node, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ]
    _assert_receipt_once(source_node, target_node, operation)
    assert entity_token(source_node) == token
    _up(source_node)
    assert running(source_node) is False
    assert running(target_node) is False


def test_w6_before_the_complete_the_chain_closes_on_the_report(
    source_node, target_node, tmp_path, harness
) -> None:
    """The orchestrator died one IPC short of the end; nothing was retired."""

    operation = "at07-w6"
    token = _open_chain(source_node, operation, harness)
    source_node.stop_daemon()
    bundle = tmp_path / BUNDLE_NAME
    _export(source_node, target_node, bundle, harness)
    received = _receive(target_node, source_node, operation, bundle, harness)

    # the window, read: both halves are durable, neither side was retired
    assert _rows(source_node, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ]
    assert _rows(target_node, operation) == [
        (operation, WORKER, "landed", "land", 1),
        (operation, WORKER, "active", "activate", 2),
    ]
    assert entity_token(source_node) == token
    assert entity_token(target_node) != token
    _up(source_node)
    assert running(source_node) is False
    assert running(target_node) is False

    # re-entry: step one refuses by name, the rest replays, the last call closes
    _assert_the_first_step_is_not_re_enterable(source_node, operation, harness)
    again = tmp_path / f"{BUNDLE_NAME}-again"
    _export(source_node, target_node, again, harness)
    served = _receive(target_node, source_node, operation, again, harness)
    assert served["replayed"] is True
    assert served["chain"]["report"] == received["chain"]["report"]

    completed = _complete(source_node, operation, served["chain"]["report"], harness)
    assert completed["removedAgent"] is True
    assert completed["chain"]["action"] == "complete"
    _assert_receipt_once(source_node, target_node, operation)
    assert running(source_node) is False
    assert running(target_node) is False
