"""U0c per-fix tests: the startup receipt sweep at the store/registry layer.

Every fix in the U0c sweep gets its own test here:

* the desired-state expiry rolls orphan/incomplete harness admissions
  back (fence flip, row state restored -- the U0b ghost-receipt shape)
  and deletes every remaining receipt, while the journal tables -- the
  compensation input and the U0b rescue evidence -- are never pruned;
* the claims reader turns receipts into backfill attestation;
* the registry expiry deletes receipts but preserves agents rows and
  fences, and reports them as claims;
* the registry's bind keeps the agent-record fence payload in sync, so
  the saga's own bind does not fence the create's compensation off as
  an external replacement (the real-domain ④ failure) -- while a REAL
  external replacement still fences.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
    RemoveHarnessCommand,
)
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.lifecycle_manager import (
    backfill_domain_attested_effects,
)


def _store(tmp_path: Path) -> DesiredStateStore:
    return DesiredStateStore(tmp_path / "desired-state.json")


def _ensure_request(
    operation_id: str, name: str = "w", ordinal: int = 3
) -> LifecycleMutationRequest:
    attempt = f"{operation_id}:forward:{ordinal}"
    return LifecycleMutationRequest(
        f"lifecycle:{attempt}",
        attempt,
        operation_id,
        None,
        EnsureHarnessCommand(
            f"lifecycle:{attempt}", HarnessLaunchProjection("pi", name, True)
        ),
    )


def _remove_request(operation_id: str, ordinal: int = 3) -> LifecycleMutationRequest:
    attempt = f"{operation_id}:forward:{ordinal}"
    return LifecycleMutationRequest(
        f"lifecycle:{attempt}",
        attempt,
        operation_id,
        None,
        RemoveHarnessCommand(f"lifecycle:{attempt}", "pi", "w"),
    )


def _journal_create_op(
    db_path: Path, operation_id: str, *, effect_status: str = "completed"
) -> None:
    """One durable create-saga row whose harness step reached effect_status."""

    import json

    request_json = json.dumps(
        {
            "operationId": operation_id,
            "kind": "create",
            "target": {
                "agentName": "w",
                "actor": "agent:tester/u0c-node/w",
                "harness": {
                    "provider": "pi",
                    "name": "w",
                    "headless": True,
                    "args": [],
                    "ownership": "managed",
                },
                "route": {
                    "route_id": "r",
                    "liveliness_key": "live/r",
                    "inbox_key": "inbox/r",
                    "advertise": True,
                },
                "session": None,
            },
            "source": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(db_path) as db:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS lifecycle_operations("
            "operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, "
            "request_json TEXT NOT NULL, state TEXT NOT NULL, error TEXT, "
            "error_code TEXT, updated_at REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS lifecycle_effects("
            "operation_id TEXT NOT NULL, ordinal INTEGER NOT NULL, "
            "effect_name TEXT NOT NULL, direction TEXT NOT NULL, "
            "correlation_id TEXT NOT NULL, attempt_token TEXT NOT NULL, "
            "status TEXT NOT NULL, changed INTEGER NOT NULL DEFAULT 1, "
            "created_by_operation INTEGER, resource_token TEXT, "
            "receipt_retired INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(operation_id, effect_name, direction));"
        )
        db.execute(
            "INSERT INTO lifecycle_operations(operation_id, request_digest, "
            "request_json, state, error, updated_at) VALUES(?, ?, ?, 'running', "
            "NULL, 0)",
            (operation_id, f"digest-{operation_id}", request_json),
        )
        db.execute(
            "INSERT INTO lifecycle_effects(operation_id, ordinal, effect_name, "
            "direction, correlation_id, attempt_token, status, changed, "
            "created_by_operation, resource_token, receipt_retired) "
            "VALUES(?, 3, 'harness.ensure', 'forward', ?, ?, ?, 1, 1, ?, 0)",
            (
                operation_id,
                f"corr-{operation_id}",
                f"{operation_id}:forward:3",
                effect_status,
                None,
            ),
        )


def test_ghost_admission_is_rolled_back_and_every_receipt_expires(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    # A half-admitted ensure: fence active, incomplete receipt, no harness
    # row (U0b: rows land on success only).  The journal has no settled
    # effect for the attempt, so the backfill cannot adopt it either.
    store.apply_harness_lifecycle(_ensure_request("ghost-op"))
    # A retirement crash window on another harness: completed but never
    # retired, and no journal at all for its operation.
    settled = store.apply_harness_lifecycle(_ensure_request("settled-op", "w2"))
    store.complete_harness_lifecycle(
        "settled-op:forward:3", settled[0].resource_token
    )
    state_before = store.load()
    assert len(state_before.lifecycle_receipts) == 2

    rolled_back, deleted = store.expire_interrupted_lifecycle_receipts()

    # Both are journal-unaccounted (orphan receipts): the ghost's fence is
    # rolled back, the completed-but-unretired one has nothing to undo and
    # is simply deleted; every receipt is gone either way.
    assert (rolled_back, deleted) == (1, 1)
    state = store.load()
    assert state.lifecycle_receipts == ()
    fence = next(
        resource
        for resource in state.lifecycle_resources
        if resource.resource_key == "harness:pi:w"
    )
    assert fence.active is False


def test_receipt_expiry_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_harness_lifecycle(_ensure_request("ghost-op"))
    first = store.expire_interrupted_lifecycle_receipts()
    second = store.expire_interrupted_lifecycle_receipts()
    # The rollback deletes the receipt it undid; the final batch delete
    # had nothing left to remove.
    assert first == (1, 0)
    assert second == (0, 0)
    assert store.load().lifecycle_receipts == ()


def test_receipt_expiry_never_prunes_the_journal(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle-operations.sqlite3"
    _journal_create_op(db_path, "journal-op", effect_status="dispatched")
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.apply_harness_lifecycle(_ensure_request("journal-op"))
    with sqlite3.connect(db_path) as db:
        before = db.execute(
            "SELECT COUNT(*) FROM lifecycle_effects"
        ).fetchone()[0]

    rolled_back, _deleted = store.expire_interrupted_lifecycle_receipts()

    # The admission could not be backfilled (effect never completed by the
    # dead generation AND the receipt cannot adopt it) -- wait: the
    # backfill is a separate step; here only the expiry runs, so the
    # journal-incomplete admission is rolled back domain-locally.
    assert rolled_back == 1
    with sqlite3.connect(db_path) as db:
        after = db.execute(
            "SELECT COUNT(*) FROM lifecycle_effects"
        ).fetchone()[0]
        operations = db.execute(
            "SELECT COUNT(*) FROM lifecycle_operations"
        ).fetchone()[0]
    assert after == before  # nothing pruned -- the U0b tripwire's subject
    assert operations == 1


def test_claims_reader_and_backfill_journal_the_domain_attestation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lifecycle-operations.sqlite3"
    _journal_create_op(db_path, "attested-op", effect_status="dispatched")
    store = DesiredStateStore(tmp_path / "desired-state.json")
    provenance, _replayed = store.apply_harness_lifecycle(
        _ensure_request("attested-op")
    )
    assert provenance.created_by_operation is True

    claims = store.lifecycle_effect_claims()
    assert [claim.attempt_token for claim in claims] == ["attested-op:forward:3"]

    backfilled = backfill_domain_attested_effects(db_path, claims)
    assert backfilled == ("attested-op:forward:3",)
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT status, created_by_operation, resource_token "
            "FROM lifecycle_effects WHERE operation_id = 'attested-op' "
            "AND effect_name = 'harness.ensure' AND direction = 'forward'"
        ).fetchone()
    assert row == ("completed", 1, provenance.resource_token)

    # Already-completed attestation is not rewritten; orphans and
    # unparseable tokens are skipped, not invented.
    assert backfill_domain_attested_effects(db_path, claims) == ()
    assert (
        backfill_domain_attested_effects(
            db_path,
            [
                claims[0].__class__(
                    "no-such-operation", "no-such-operation:forward:0",
                    True, True, "token",
                ),
                claims[0].__class__(
                    "attested-op", "not-a-saga-token", True, True, "token"
                ),
            ],
        )
        == ()
    )


def test_registry_expiry_preserves_agents_and_reports_claims(
    tmp_path: Path,
) -> None:
    from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner="o", machine="m")
    request = LifecycleMutationRequest(
        "lifecycle:op:forward:0",
        "op:forward:0",
        "op",
        None,
        CreateAgentCommand("lifecycle:op:forward:0", "w"),
    )
    provenance, _operation, _replayed = registry.apply_lifecycle(request)
    assert registry.get("w") is not None

    claims = registry.lifecycle_effect_claims()
    assert [(claim.attempt_token, claim.created_by_operation) for claim in claims] == [
        ("op:forward:0", True)
    ]

    deleted = registry.expire_lifecycle_receipts()

    assert deleted == 1
    assert registry.get("w") is not None  # the durable world survives
    assert registry.lifecycle_effect_claims() == []
    assert registry.expire_lifecycle_receipts() == 0
    registry.close()


def test_saga_bind_does_not_fence_off_the_creates_compensation(
    tmp_path: Path,
) -> None:
    """④'s real-domain failure: bind drift fenced destroy off.

    The create saga's own ``agent.bind`` step updates the agents row
    (last_harness / preferred_harness).  The agent-record fence compares
    its payload against that row, so without the payload sync the destroy
    that compensates ``agent.create`` (carrying the CREATE token) hit a
    rotated fence and no-opped -- the half-built agent survived the
    compensation.
    """

    from hyprial.identity.impl.agents.actor.ports import BindAgentCommand, CreateAgentCommand, DestroyAgentCommand

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner="o", machine="m")
    create = LifecycleMutationRequest(
        "lifecycle:op:forward:0", "op:forward:0", "op", None,
        CreateAgentCommand("lifecycle:op:forward:0", "w"),
    )
    provenance, _operation, _replayed = registry.apply_lifecycle(create)
    bind = LifecycleMutationRequest(
        "lifecycle:op:forward:2", "op:forward:2", "op", None,
        BindAgentCommand("lifecycle:op:forward:2", registry.get("w").uri, "pi", "headless", None),
    )
    registry.apply_lifecycle(bind)
    assert registry.get("w").last_harness == "pi"

    destroy = LifecycleMutationRequest(
        "lifecycle:op:compensation:0", "op:compensation:0", "op",
        provenance.resource_token,  # the journal's CREATE token
        DestroyAgentCommand("lifecycle:op:compensation:0", "w"),
    )
    destroy_provenance, _operation, _replayed = registry.apply_lifecycle(destroy)

    assert destroy_provenance.changed is True
    assert registry.get("w") is None  # the half-built agent is gone
    registry.close()


def test_external_replacement_still_fences_the_compensation(
    tmp_path: Path,
) -> None:
    """The compare-and-delete contract survives the bind sync: a REAL
    external mutation of the agents row (a plain ``update``, outside any
    saga) still drifts the agent-record fence payload, rotates the token
    and fences the destroy carrying the old token off."""

    from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand, DestroyAgentCommand

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner="o", machine="m")
    create = LifecycleMutationRequest(
        "lifecycle:op:forward:0", "op:forward:0", "op", None,
        CreateAgentCommand("lifecycle:op:forward:0", "w"),
    )
    provenance, _operation, _replayed = registry.apply_lifecycle(create)
    registry.update("w", model="replaced-model")  # external, non-saga

    destroy = LifecycleMutationRequest(
        "lifecycle:op:compensation:0", "op:compensation:0", "op",
        provenance.resource_token,
        DestroyAgentCommand("lifecycle:op:compensation:0", "w"),
    )
    destroy_provenance, _operation, _replayed = registry.apply_lifecycle(destroy)

    assert destroy_provenance.changed is False
    assert registry.get("w") is not None  # the external world is preserved
    registry.close()
