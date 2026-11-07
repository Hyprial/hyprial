"""U0b (PR #332 fixes, Allen 2026-09-03): start writes desired state only
after the process is up; failed is persisted and never auto-retried.

The three spec tests, plus the pre-U0b-database migration:

  ①  a start whose process never came up leaves NO desired-state trace --
      not when the retry budget cleans up after it (that path was already
      covered by ``test_harness_permanent_failure_rolls_back_and_is_terminal``)
      but DURING the attempt: the row must not exist while the process is
      still starting, so a daemon crash inside the retry window cannot
      leave a ghost either.
  ②  a row whose last known result was ``running`` is re-run on restart;
      ``failed`` is not (the application-level restart gate feeds exactly
      the running rows to both restore halves).
  ③  a harness that ran before and did not come back is persisted as
      ``failed``, displayed, and not retried -- neither by reconcile in
      this generation nor by restore after the next restart -- until an
      explicit start succeeds and re-earns ``running``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow, _SCHEMA
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
    HarnessRestoreProjection,
)
from hyprial.kernel import LifecycleMutationCompleted

from tests.daemon.lifecycle.lifecycle.ports.support import _Events, _Launcher, _request

NODE = "u0b-node"
OWNER = "tester"


# ---------------------------------------------------------------------------
# ①  no trace until the process is up
# ---------------------------------------------------------------------------


def test_start_leaves_no_desired_row_while_the_process_is_starting(
    tmp_path: Path,
) -> None:
    """The crash-window half of ①: mid-attempt, the row must not exist.

    ``_on_lifecycle`` durably applies the resource fence and receipt BEFORE
    spawning (idempotency and rollback custody); the harnesses row is what
    must NOT be there until the start settles successfully, so a daemon
    that dies anywhere inside the retry window leaves no ghost.
    """

    entered = threading.Event()
    release = threading.Event()

    class _BlockingLauncher(_Launcher):
        def start(self, spec: HarnessLaunchSpec) -> Any:
            self.start_calls += 1
            entered.set()
            assert release.wait(5.0)
            return super().start(spec)

    store = DesiredStateStore(tmp_path / "desired-state.json")
    events = _Events()
    actor = HarnessRuntimeActor(
        _BlockingLauncher(), event_sink=events, desired_state=store
    )
    request = _request(
        "u0b-midflight",
        0,
        EnsureHarnessCommand(
            "u0b-midflight-payload",
            HarnessLaunchProjection("codex", "midflight", True),
        ),
    )
    assert actor.submit(request) is PortAdmission.ACCEPTED
    assert entered.wait(2.0), "launcher was never asked to start"

    mid_flight = store.load()
    assert not mid_flight.harnesses, (
        "desired state gained a harnesses row while the process was still "
        "starting -- a daemon crash here would leave exactly the ghost U0b "
        "exists to eliminate"
    )
    # The fence and receipt ARE there: replay idempotency and rollback
    # custody still work exactly as before.
    assert mid_flight.lifecycle_receipts
    assert mid_flight.lifecycle_resources

    release.set()
    completed = events.wait(request.correlation_id)
    assert isinstance(completed, LifecycleMutationCompleted)
    settled = store.load()
    assert [item.name for item in settled.harnesses] == ["midflight"]
    assert settled.harnesses[0].status == "running"
    assert settled.lifecycle_receipts[0].completed
    assert actor.close_runtime(2.0)


def test_applied_ensure_without_a_settlement_writes_no_row(tmp_path: Path) -> None:
    """The store-level half of ①: apply alone (crash before any settlement)
    leaves the fence and receipt but no harnesses row."""

    store = DesiredStateStore(tmp_path / "desired-state.json")
    request = _request(
        "u0b-crashed",
        0,
        EnsureHarnessCommand(
            "u0b-crashed-payload",
            HarnessLaunchProjection("codex", "crashed", True),
        ),
    )
    provenance, replayed = store.apply_harness_lifecycle(request)
    assert provenance.changed and not replayed

    # What a restart would read: receipt pending, resource fenced, NO row.
    state = store.load()
    assert not state.harnesses
    assert not state.lifecycle_receipts[0].completed
    assert any(
        resource.resource_key == "harness:codex:crashed"
        for resource in state.lifecycle_resources
    )


# ---------------------------------------------------------------------------
# ②/③  restore re-runs running rows; failed is persisted and not retried
# ---------------------------------------------------------------------------


def test_restore_failure_persists_failed_and_never_retries(
    tmp_path: Path,
) -> None:
    """Spec test ③, producer side: ran before, did not come back.

    One restore attempt fails -> the row's status becomes ``failed`` in
    desired state, reconcile does not restart it in this generation, the
    next restart's gate does not include it, and an explicit start that
    succeeds re-earns ``running``.
    """

    store = DesiredStateStore(tmp_path / "desired-state.json")
    # The row exists because the harness ran before (written by a prior
    # successful start or by declaration); it is what restore reads.
    store.upsert_harness(HarnessLaunchSpec("codex", "veteran", True))
    launcher = _Launcher(start_failures=1)
    events = _Events()
    actor = HarnessRuntimeActor(
        launcher, event_sink=events, desired_state=store, failure_budget=3
    )
    facade = HarnessRuntimeFacade(actor)

    summary = facade.restore(
        (spec for spec in store.load().harnesses if spec.status == "running")
    )
    assert summary.attempted == 1
    assert summary.failed == 1

    veteran = next(
        item for item in store.load().harnesses if item.name == "veteran"
    )
    assert veteran.status == "failed", (
        "a harness that ran before and did not come back must be persisted "
        "as failed -- displayed, waiting for a human"
    )

    # Not retried by reconcile in this generation.
    facade.reconcile()
    facade.reconcile()
    assert launcher.start_calls == 1

    # Not re-run by the next restart: the application-level gate (asserted
    # directly below) filters status, but the same predicate applied to a
    # fresh load of this store must select nothing.
    assert [
        spec for spec in store.load().harnesses if spec.status == "running"
    ] == []

    # An explicit start that succeeds re-earns running.
    revival = _request(
        "u0b-revival",
        0,
        EnsureHarnessCommand(
            "u0b-revival-payload",
            HarnessLaunchProjection("codex", "veteran", True),
        ),
    )
    assert actor.submit(revival) is PortAdmission.ACCEPTED
    assert isinstance(events.wait(revival.correlation_id), LifecycleMutationCompleted)
    revived = next(
        item for item in store.load().harnesses if item.name == "veteran"
    )
    assert revived.status == "running"
    assert actor.read_harness("codex:veteran") is not None
    assert actor.read_harness("codex:veteran").running  # type: ignore[union-attr]
    assert actor.close_runtime(2.0)


def _provider(harness: str, name: str, *, status: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "provider": harness,
        "name": name,
        "headless": True,
        "args": [],
        "ownership": "managed",
    }
    if status is not None:
        record["status"] = status
    return record


def _application(
    tmp_path: Path, providers: list[dict[str, Any]]
) -> DaemonApplication:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state_dir = home / "state"
    state_dir.mkdir(parents=True)
    # Seed BEFORE the store constructs: construction imports the .v1 file.
    Path(state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": providers,
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    return DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id=NODE,
        hyprial_home=home,
        owner=OWNER,
    **_deep_module_daemon_dependencies())


class _RecordingHarnesses:
    def __init__(self) -> None:
        self.restored: list[tuple[str, str]] = []

    def restore(self, desired: tuple[Any, ...]) -> Any:
        self.restored.extend((spec.harness, spec.name) for spec in desired)
        # The real summary type, not a fabricated shape: restore's contract
        # is HarnessRestoreProjection, and the daemon log line reads its
        # fields (now including ``deferred``).  A stub built from the real
        # type is the ruling F2 wrote down for launch results, applied here.
        return HarnessRestoreProjection(
            attempted=len(desired), restored=len(desired), failed=0, deferred=0
        )


class _RecordingManager:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, operation: Any) -> PortAdmission:
        self.submitted.append(str(operation.operation_id))
        return PortAdmission.ACCEPTED

    def drain(self, _timeout: float) -> bool:
        return True


def test_restart_gate_restores_running_rows_and_skips_failed(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    'Spec test ② at the restart gate: only ``running`` is re-run.\n\n    One running and one failed provider, both non-Lark: restore is asked\n    for exactly the running one (the failed one is the human\'s business).\n\n    U0BRACE: this test USED to also assert that bootstrap submits a create\n    saga for exactly the running row ("either half alone would leave the\n    other as a retry path").  That expectation pinned the double owner this\n    change deletes: bootstrap\'s saga harness step is itself a process\n    start, so submitting it for the same rows the restore batch owns gave\n    every non-Lark running row two submitters and made the no-auto-retry\n    ruling depend on two identical filters staying identical.  The ruling\n    is now carried by the restore predicate ALONE -- there is no second\n    start path to filter -- and ``tests/daemon/application/supervision/test_daemon_restart_ownership.py``\n    pins that no startup-side submitter exists at all.  The failed-row\n    assertion below is unchanged in strength: nothing restarts it.\n    '

    app = _application(
        tmp_path,
        [
            _provider("pi", "healthy"),
            _provider("codex", "sickly", status="failed"),
        ],
    )
    def close_application() -> None:
        try:
            app._close()
        finally:
            app._close_home_lease()

    request.addfinalizer(close_application)
    harnesses = _RecordingHarnesses()
    app._harnesses = harnesses  # type: ignore[assignment]
    manager = _RecordingManager()
    app._lifecycle_manager = manager  # type: ignore[assignment]

    app._restore_harnesses()
    assert harnesses.restored == [("pi", "healthy")], (
        "restart must re-run exactly the rows whose last result was "
        "running; failed rows are displayed and left for a human"
    )
    assert manager.submitted == [], (
        "the restart gate has exactly one owner per row (the restore "
        "batch); a lifecycle submission here would be the second owner "
        "U0BRACE deletes -- and the retry path the old assertion guarded "
        "against"
    )


# U0BRACE: ``test_bootstrap_with_only_failed_rows_needs_no_lifecycle_manager``
# retired.  It pinned that the (now deleted) construction-phase submitter
# skipped failed rows BEFORE demanding a lifecycle manager -- a property of
# a path that no longer exists.  Its subject is covered twice over now:
# failed rows are not restored (test above) and no submission happens at
# all (tests/test_daemon_restart_ownership.py).


# ---------------------------------------------------------------------------
#  pre-U0b database migration: the status column and ghost resolution
# ---------------------------------------------------------------------------


def _old_schema() -> str:
    """The harnesses DDL as the pre-U0b build wrote it (no status column)."""

    needle = (
        "    status TEXT NOT NULL DEFAULT 'running' "
        "CHECK (status IN ('running', 'failed')),\n"
    )
    assert needle in _SCHEMA, "schema drifted; update the migration fixture"
    return _SCHEMA.replace(needle, "")


def _receipt_row(
    key: str,
    attempt: str,
    *,
    completed: bool,
    changed: bool = True,
) -> tuple[Any, ...]:
    return (
        "harness",
        attempt,
        f"op-{attempt}",
        key,
        None,
        int(changed),
        int(changed),
        f"token-{attempt}",
        0,
        int(completed),
        0,
        None,
        None,
        None,
    )


def _old_database(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        db.executescript(_old_schema())
        # The durable journal, as the pre-U0b build wrote it (same schema
        # family as lifecycle_manager._LifecycleStore; never pruned).
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_operations (
                operation_id TEXT PRIMARY KEY,
                request_digest TEXT NOT NULL,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_effects (
                operation_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                effect_name TEXT NOT NULL,
                direction TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                attempt_token TEXT NOT NULL,
                status TEXT NOT NULL,
                changed INTEGER NOT NULL DEFAULT 1,
                created_by_operation INTEGER,
                resource_token TEXT,
                receipt_retired INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(operation_id, effect_name, direction)
            );
            """
        )
        db.execute(
            "INSERT INTO root_state(id, schema_version, as_mailbox) VALUES(1, 1, 0)"
        )
        for name in ("ghost", "plain", "recent", "resilient", "veteran"):
            db.execute(
                "INSERT INTO harnesses(harness, name, headless, args_json, "
                "ownership, command_json, containerized) "
                "VALUES('codex', ?, 1, '[]', 'managed', '[]', 0)",
                (name,),
            )
        # ghost: crashed mid-start -- an incomplete receipt and nothing else.
        db.execute(
            "INSERT INTO lifecycle_receipts(domain, attempt_token, operation_id, "
            "resource_key, expected_resource_token, created_by_operation, changed, "
            "resource_token, retired, completed, attempts, correlation_id, "
            "generation, version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _receipt_row("harness:codex:ghost", "attempt-ghost", completed=False),
        )
        # recent: completed receipt not yet retired.
        db.execute(
            "INSERT INTO lifecycle_receipts(domain, attempt_token, operation_id, "
            "resource_key, expected_resource_token, created_by_operation, changed, "
            "resource_token, retired, completed, attempts, correlation_id, "
            "generation, version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _receipt_row("harness:codex:recent", "attempt-recent", completed=True),
        )
        # resilient: came up once (completed), now mid-restart (incomplete).
        db.execute(
            "INSERT INTO lifecycle_receipts(domain, attempt_token, operation_id, "
            "resource_key, expected_resource_token, created_by_operation, changed, "
            "resource_token, retired, completed, attempts, correlation_id, "
            "generation, version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _receipt_row("harness:codex:resilient", "attempt-old", completed=True),
        )
        db.execute(
            "INSERT INTO lifecycle_receipts(domain, attempt_token, operation_id, "
            "resource_key, expected_resource_token, created_by_operation, changed, "
            "resource_token, retired, completed, attempts, correlation_id, "
            "generation, version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _receipt_row(
                "harness:codex:resilient", "attempt-new", completed=False
            ),
        )
        # veteran: ran for weeks (the journal proves completed starts from
        # every past bootstrap), then a restart attempt crashed mid-flight --
        # incomplete receipt, completed receipts long retired.  The receipt
        # rule alone would delete it and lose real user intent; the journal
        # must rescue it.
        db.execute(
            "INSERT INTO lifecycle_receipts(domain, attempt_token, operation_id, "
            "resource_key, expected_resource_token, created_by_operation, changed, "
            "resource_token, retired, completed, attempts, correlation_id, "
            "generation, version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _receipt_row("harness:codex:veteran", "attempt-vet", completed=False),
        )
        _journal_op(db, "op-veteran-old", "veteran", effect_status="completed")
        # ghost's own saga crashed before the harness step settled: the
        # journal row exists but never completed -- it must NOT rescue.
        _journal_op(db, "op-ghost", "ghost", effect_status="running")
        db.commit()
    finally:
        db.close()


def _journal_op(
    db: sqlite3.Connection, operation_id: str, name: str, *, effect_status: str
) -> None:
    """One durable create-saga record whose harness step reached
    ``effect_status`` (the shape ``lifecycle_manager.reserve`` writes)."""

    request_json = json.dumps(
        {
            "operationId": operation_id,
            "kind": "create",
            "target": {
                "agentName": name,
                "actor": f"agent://tester/u0b-node/{name}",
                "harness": {
                    "provider": "codex",
                    "name": name,
                    "headless": True,
                    "args": [],
                    "ownership": "managed",
                },
                "route": {"routeId": name},
                "session": None,
            },
            "source": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    db.execute(
        "INSERT INTO lifecycle_operations(operation_id, request_digest, "
        "request_json, state, error, updated_at) VALUES(?, ?, ?, ?, NULL, 0)",
        (operation_id, f"digest-{operation_id}", request_json, "running"),
    )
    db.execute(
        "INSERT INTO lifecycle_effects(operation_id, ordinal, effect_name, "
        "direction, correlation_id, attempt_token, status, changed, "
        "created_by_operation, resource_token, receipt_retired) "
        "VALUES(?, 0, 'harness.ensure', 'forward', ?, ?, ?, 1, 1, NULL, 0)",
        (operation_id, f"corr-{operation_id}", f"attempt-{operation_id}", effect_status),
    )


def _harness_names(db_path: Path) -> dict[str, str]:
    db = sqlite3.connect(db_path)
    try:
        return {
            row[0]: row[1]
            for row in db.execute("SELECT name, status FROM harnesses")
        }
    finally:
        db.close()


def test_migration_backfills_running_and_deletes_never_confirmed_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lifecycle-operations.sqlite3"
    _old_database(db_path)

    DesiredStateSqliteShadow(db_path).migrate_harness_status()

    names = _harness_names(db_path)
    assert names == {
        "plain": "running",
        "recent": "running",
        "resilient": "running",
        "veteran": "running",
    }, (
        "rows whose last attempt never settled (no completed receipt, an "
        "incomplete one) are the ghosts U0b eliminates: no trace -- UNLESS "
        "the durable journal proves a completed past start, which rescues "
        "the ran-for-weeks-then-crashed-mid-restart row; every other row "
        "-- no receipts (the retired steady state), a completed receipt, or "
        "an earlier success behind an in-flight attempt -- is a "
        "previously-running harness and backfills to running"
    )
    # The ghost's receipt is deliberately untouched: the saga journal's
    # replay idempotency still reads it (U0c owns retiring that machinery).
    db = sqlite3.connect(db_path)
    try:
        ghosts = db.execute(
            "SELECT COUNT(*) FROM lifecycle_receipts "
            "WHERE resource_key = 'harness:codex:ghost'"
        ).fetchone()[0]
    finally:
        db.close()
    assert ghosts == 1

    # Exactly-once: the column's presence is the sentinel.
    DesiredStateSqliteShadow(db_path).migrate_harness_status()
    assert _harness_names(db_path) == names


def test_migration_still_rescues_a_veteran_whose_only_evidence_is_bootstrap(
    tmp_path: Path,
) -> None:
    "U0BRACE companion: the veteran rescue survives the partition.\n\n    On real homes most completed ``harness.ensure`` evidence in the journal\n    was written by restart-time bootstrap sagas (on this machine's\n    production copy: 808 of 835 completed effects, and 23 of 39 desired\n    rows carry bootstrap-written evidence ONLY).  The partition deletes\n    the resubmission, so no NEW bootstrap effects will ever be written --\n    the rescue must keep working off the historical ones, which the\n    retention tripwire (``tests/daemon/lifecycle/journal/test_lifecycle_journal_retention_gate.py``)\n    keeps permanent.  This fixture recreates exactly that shape: a row\n    whose ONLY completed effect belongs to a ``bootstrap:*`` operation,\n    with an incomplete receipt in front of it (the ran-for-weeks,\n    crashed-mid-restart row).\n    "

    db_path = tmp_path / "lifecycle-operations.sqlite3"
    db = sqlite3.connect(db_path)
    try:
        db.executescript(_old_schema())
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_operations (
                operation_id TEXT PRIMARY KEY,
                request_digest TEXT NOT NULL,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_effects (
                operation_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                effect_name TEXT NOT NULL,
                direction TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                attempt_token TEXT NOT NULL,
                status TEXT NOT NULL,
                changed INTEGER NOT NULL DEFAULT 1,
                created_by_operation INTEGER,
                resource_token TEXT,
                receipt_retired INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(operation_id, effect_name, direction)
            );
            """
        )
        db.execute(
            "INSERT INTO root_state(id, schema_version, as_mailbox) "
            "VALUES(1, 1, 0)"
        )
        db.execute(
            "INSERT INTO harnesses(harness, name, headless, args_json, "
            "ownership, command_json, containerized) "
            "VALUES('codex', 'weeks', 1, '[]', 'managed', '[]', 0)"
        )
        db.execute(
            "INSERT INTO lifecycle_receipts(domain, attempt_token, operation_id, "
            "resource_key, expected_resource_token, created_by_operation, changed, "
            "resource_token, retired, completed, attempts, correlation_id, "
            "generation, version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _receipt_row("harness:codex:weeks", "attempt-weeks", completed=False),
        )
        db.commit()
    finally:
        db.close()

    # The evidence: a completed forward harness.ensure under a restart-time
    # bootstrap operation id -- the exact producer the partition removed.
    db = sqlite3.connect(db_path)
    try:
        _journal_op(
            db,
            "bootstrap:1725400000:codex:weeks",
            "weeks",
            effect_status="completed",
        )
        db.commit()
    finally:
        db.close()

    DesiredStateSqliteShadow(db_path).migrate_harness_status()

    assert _harness_names(db_path) == {"weeks": "running"}, (
        "a veteran whose only completed evidence was written by a "
        "bootstrap-era saga must still be rescued as running: the journal "
        "is permanent (retention tripwire), and the partition writes no new "
        "evidence for it to depend on"
    )


def test_store_construction_upgrades_an_old_database(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle-operations.sqlite3"
    _old_database(db_path)

    store = DesiredStateStore(tmp_path / "desired-state.json")
    state = store.load()
    assert sorted(item.name for item in state.harnesses) == [
        "plain",
        "recent",
        "resilient",
        "veteran",
    ]
    assert all(item.status == "running" for item in state.harnesses)
    # Round-trip keeps failed out of the byte form when running (absent
    # == running), so pre-U0b documents stay byte-stable.
    document = store.load().to_json()
    assert all("status" not in item for item in document["providers"])

    store.mark_harness_failed("codex", "plain")
    failed = next(
        item for item in store.load().harnesses if item.name == "plain"
    )
    assert failed.status == "failed"
    serialized = store.load().to_json()["providers"]
    plain = next(item for item in serialized if item["name"] == "plain")
    assert plain["status"] == "failed"


def test_migration_on_missing_database_creates_no_file(tmp_path: Path) -> None:
    shadow = DesiredStateSqliteShadow(tmp_path / "absent.sqlite3")
    shadow.migrate_harness_status()
    assert not (tmp_path / "absent.sqlite3").exists()


def test_mark_harness_failed_without_a_row_leaves_no_trace(
    tmp_path: Path,
) -> None:
    """failed is only ever a demotion of an earned row: marking a harness
    that never succeeded must not create one (① holds through this path)."""

    store = DesiredStateStore(tmp_path / "desired-state.json")
    assert store.mark_harness_failed("codex", "never-was") is False
    assert not store.load().harnesses
