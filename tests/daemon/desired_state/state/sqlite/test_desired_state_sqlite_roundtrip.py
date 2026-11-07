"""Round-trip: DesiredState -> SQLite store -> DesiredState (U0a-2).

Pins the PRODUCT reverse mapping
``hyprial.daemon.impl.desired_state_sqlite.DesiredStateSqliteShadow.read_document``:
every assertion here goes through that src symbol, never through a
test-local reimplementation.  That is what makes a mutation to the product
reverse mapping (a dropped column, a renamed JSON key) turn these tests red.

Two comparisons per spike/u0a1-reconstruction-audit @3ac8d2b:

  1. the rebuilt document vs the document ``save`` committed -- byte for
     byte in the canonical encoding (the store's own ``to_json()`` payload;
     since Allen's no-projection ruling there is no JSON file to diff
     against -- the file-side equivalence lives on in the migration
     equivalence test, which asserts read_document() == the parsed pre-
     migration JSON)
  2. ``DesiredState.from_json(rebuilt)`` vs ``store.load()`` -- dataclass
     equality, which includes the receipt tuple order

Plus the receipt ordering assertion (comparison 3 of the spike).

The receipt order assertion depends on one premise and is its tripwire: the
store rewrites the whole document on every save (one full DELETE + INSERT
per save), so rowid order equals the last committed document's order.  If
the write strategy ever changes (e.g. in-transaction upsert), rowid order
stops tracking the document and this assertion goes red -- at that point an
explicit ``ordinal`` column must be DECIDED, not silently skipped.  (The
U0a-2 grep found no reader whose result depends on receipt order, so no
ordinal column was added; revisit that verdict if any ordered reader
appears.)

Fixture provenance -- do not restate or "improve" the fixture list here:
``populate()`` below is the verbatim ``populate()`` from
spike/u0a1-reconstruction-audit @3ac8d2b, the audit whose conclusions this
step independently re-checks.  Each fixture there carries a comment naming
the class of round-trip loss it guards (full-field rows, NULL columns,
unicode, a NULL-vs-value three-state column, non-canonical receipt order).
The sha is pinned ON PURPOSE: fixtures added to the spike branch later do
NOT automatically extend this test -- extending coverage is a deliberate
act against that pinned audit, not a moving target.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.desired_state import (
    DesiredState,
    DesiredStateStore,
    InteractiveSession,
    PendingSessionAgentEffect,
)

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessLaunchProjection
from hyprial.kernel import LifecycleMutationRequest

# Canonical encoding of the authoritative file: exactly what
# DesiredStateStore._atomic_json_write does (json.dump with indent=2,
# sort_keys=True, trailing newline).  Restated here so the byte comparison
# stays independently checkable.
_CANONICAL = dict(indent=2, sort_keys=True)


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, **_CANONICAL) + "\n").encode("utf-8")


def _receipt_rows(db_path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute(
                "SELECT domain, attempt_token FROM lifecycle_receipts "
                "ORDER BY rowid"
            )
        )
    finally:
        connection.close()


def populate(store: DesiredStateStore) -> None:
    # Verbatim from spike/u0a1-reconstruction-audit @3ac8d2b populate();
    # per-fixture loss-class comments live in the original -- see module
    # docstring for why this is pinned rather than restated.
    store.set_mailbox_role(True)
    store.upsert_harness(
        HarnessLaunchSpec(
            harness="codex",
            name="worker-1",
            headless=True,
            args=("--full-auto", "--flag=中文"),
            nickname="w1",
            cwd="/tmp/w1",
            endpoint="tcp://127.0.0.1:9101",
            session_ref="thread-123",
            command=("/usr/local/bin/codex", "proto"),
            turn_timeout_seconds=90.5,
            idle_timeout_seconds=30.0,
            containerized=True,
            pinned_owner="owner-1",
            container_image="hyprial-worker:latest",
            model_provider="openai",
            model="gpt-5.2",
        )
    )
    store.register_interactive(
        InteractiveSession(
            actor="actor-1",
            cwd="/tmp/actor-1",
            command=("pi", "--repl"),
            source="repl",
            session_ref="sess-abc",
            runtime="pi_repl",
            channel_confirmed=True,
            channel_build_version="0.4.4",
            channel_protocol_version=3,
            owner_fence=True,
            channel_lease_digest="a" * 64,
            tmux_session="actor-1",
        )
    )
    # Minimal session: exercises NULL columns (session_ref/runtime/lease
    # digest/tmux NULL, owner_fence stays the None third state).
    store.register_interactive(
        InteractiveSession(
            actor="actor-2",
            cwd="/tmp/actor-2",
            command=("pi",),
            source="repl",
        )
    )
    store.record_session_agent_effects(
        (
            PendingSessionAgentEffect(
                effect_id="effect-1",
                correlation_id="corr-e1",
                operation="bind",
                actor="actor-1",
                harness="codex",
                runtime="pi_worker",
                session_id="worker-1",
            ),
            PendingSessionAgentEffect(
                effect_id="effect-2",
                correlation_id="corr-e2",
                operation="release",
                actor="actor-2",
            ),
        )
    )
    # Receipt m: create, fail once, complete, then roll back (receipt leaves
    # the table, resource stays behind as an inactive token fence).
    provenance_m, _ = store.apply_harness_lifecycle(
        LifecycleMutationRequest(
            correlation_id="corr-h1",
            attempt_token="attempt-m",
            operation_id="op-h1",
            expected_resource_token=None,
            payload=EnsureHarnessCommand(
                correlation_id="corr-h1",
                spec=HarnessLaunchProjection(
                    harness="jev", name="relay-1", headless=True
                ),
            ),
        ),
        generation=7,
        version=3,
    )
    store.record_harness_lifecycle_failure(
        "attempt-m", provenance_m.resource_token
    )
    store.complete_harness_lifecycle(
        "attempt-m", provenance_m.resource_token, generation=9, version=4
    )
    state = store.load()
    resource = next(
        r
        for r in state.lifecycle_resources
        if r.resource_key == "harness:jev:relay-1"
    )
    assert store.rollback_harness_lifecycle(
        "attempt-m", provenance_m.resource_token
    )
    # Receipt c: re-apply behind the rolled-back token fence -- survives with
    # a NON-NULL expected_resource_token; ordered BEFORE a non-PK-ordered
    # survivor below.
    provenance_c, _ = store.apply_harness_lifecycle(
        LifecycleMutationRequest(
            correlation_id="corr-h2",
            attempt_token="attempt-c",
            operation_id="op-h2",
            expected_resource_token=resource.resource_token,
            payload=EnsureHarnessCommand(
                correlation_id="corr-h2",
                spec=HarnessLaunchProjection(
                    harness="jev", name="relay-1", headless=True
                ),
            ),
        ),
        generation=10,
        version=2,
    )
    store.record_harness_lifecycle_failure(
        "attempt-c", provenance_c.resource_token
    )
    store.complete_harness_lifecycle(
        "attempt-c", provenance_c.resource_token, generation=11, version=5
    )
    assert store.retire_lifecycle_receipt(
        "harness", "attempt-c", provenance_c.resource_token
    )
    # Receipt a: inserted LAST with a token that sorts FIRST -- file order
    # [c, a] must differ from PK order [a, c].  No generation/version kwargs:
    # exercises the NULL third state on both columns; left incomplete.
    store.apply_harness_lifecycle(
        LifecycleMutationRequest(
            correlation_id="corr-h3",
            attempt_token="attempt-a",
            operation_id="op-h3",
            expected_resource_token=None,
            payload=EnsureHarnessCommand(
                correlation_id="corr-h3",
                spec=HarnessLaunchProjection(
                    harness="jev", name="relay-2", headless=True
                ),
            ),
        )
    )
    store.set_channel_pin("chan-2", "agent-2")
    store.set_channel_pin("chan-1", "agent-1")
    store.update_zenoh_endpoints(
        listen=("tcp://127.0.0.1:9001", "quic://127.0.0.1:9002"),
        connect=("tcp://peer.example:9003",),
    )


def test_document_rebuilds_byte_for_byte_and_state_round_trips(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)
    populate(store)

    shadow = DesiredStateSqliteShadow(
        tmp_path / "lifecycle-operations.sqlite3"
    )
    rebuilt = shadow.read_document()
    assert rebuilt is not None

    # 1. The rebuilt document is exactly what the last save committed,
    #    byte for byte in the canonical encoding.  The committed document
    #    is observed through the store's own serialization (save validates
    #    via from_json(to_json(...)) first, so load().to_json() IS the
    #    committed payload).
    committed = store.load().to_json()
    assert _canonical_bytes(rebuilt) == _canonical_bytes(committed)

    # 2. Feeding the rebuilt document through the product parser yields the
    #    state the store itself reports (dataclass equality includes the
    #    receipt tuple, so this already fails if receipts come back
    #    reordered).
    assert DesiredState.from_json(rebuilt) == store.load()


def test_receipt_rowid_order_equals_document_order(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)
    populate(store)

    document = DesiredState.from_json(
        DesiredStateSqliteShadow(
            tmp_path / "lifecycle-operations.sqlite3"
        ).read_document()
    )

    # Premise (see module docstring): rowid order == document order holds
    # only under the full-replace write strategy.  The populate fixtures
    # deliberately make PK order differ from document order
    # ([c, a] vs [a, c]), so an implementation that reads receipts in PK
    # order -- or a write strategy that loses document order -- goes red
    # here instead of passing silently.
    rowid_order = [
        (row["domain"], row["attempt_token"])
        for row in _receipt_rows(tmp_path / "lifecycle-operations.sqlite3")
    ]
    document_order = [
        (receipt.domain, receipt.attempt_token)
        for receipt in document.lifecycle_receipts
    ]
    assert rowid_order == document_order
    # ...and the fixture really does have order teeth (PK order differs):
    assert sorted(rowid_order) != rowid_order
