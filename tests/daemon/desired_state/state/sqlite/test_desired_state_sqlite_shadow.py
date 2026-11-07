'Reconciliation between the desired-state SQLite rows and the document.\n\nStep U0a-1 established the invariant "after the same operation, SQLite\ncontent matches the committed document".  Under U0a-2 (Allen\'s ruling)\nSQLite is the only storage, so the independently-derived side of these\ncomparisons is the committed document itself: the expected rows are built\nHERE, field by field, from ``store.load().to_json()`` -- never through the\nshadow\'s row mapping.  The division of labor with\n``tests/daemon/desired_state/state/sqlite/test_desired_state_sqlite_roundtrip.py``: that file pins the\ndocument-level equivalence (read_document vs the committed serialization,\nplus mutation evidence); this file pins row-level reconciliation (a\nmutated INSERT path lands wrong rows here even when the document round-\ntrips).\n'

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

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

# Must match the canonical encoding used for composite shadow columns; the
# options are restated here on purpose so this file stays independently
# checkable against the JSON document.
_JSON_TEXT = dict(sort_keys=True, separators=(",", ":"))


def _json_text(value: object) -> str:
    return json.dumps(value, **_JSON_TEXT)


def test_legacy_interactive_rows_gain_process_columns_without_a_verdict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)
    store.save(
        DesiredState(
            interactive_sessions=(
                InteractiveSession(
                    actor="agent:owner:node:legacy",
                    cwd=str(tmp_path),
                    command=("codex", "--remote"),
                    source="codex-app-server",
                    session_ref="legacy-session",
                ),
            )
        )
    )
    database = tmp_path / "lifecycle-operations.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE interactive_sessions DROP COLUMN process_pid")
        connection.execute(
            "ALTER TABLE interactive_sessions DROP COLUMN process_identity"
        )

    shadow = DesiredStateSqliteShadow(database)
    shadow.migrate_interactive_process_identity()

    with sqlite3.connect(database) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(interactive_sessions)")
        }
    session = store.load().interactive_sessions[0]
    assert {"process_pid", "process_identity"}.issubset(columns)
    assert session.process_pid is None
    assert session.process_identity is None


_TABLE_KEY: dict[str, tuple[str, ...]] = {
    "desired_state_meta": ("key",),
    "root_state": ("id",),
    "harnesses": ("harness", "name"),
    "adapters": ("name",),
    "adapter_model_backup": ("id",),
    "interactive_sessions": ("actor",),
    "pending_session_agent_effects": ("effect_id",),
    "lifecycle_resources": ("domain", "resource_key"),
    "lifecycle_receipts": ("domain", "attempt_token"),
    "channel_pins": ("channel",),
    "deprecated_shared_channels": ("channel",),
    "zenoh_endpoints": ("direction", "ordinal"),
    "service_connections": ("name",),
    "service_registry": ("id",),
    "service_registry_entries": ("ordinal",),
}


def _read_shadow(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        tables = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name != 'harness_restore_settlements'"
            )
        ]
        return {
            table: [
                dict(row)
                for row in connection.execute(f'SELECT * FROM "{table}"')
            ]
            for table in tables
        }
    finally:
        connection.close()


def _expected_tables(state: DesiredState) -> dict[str, list[dict[str, Any]]]:
    """Derive every expected shadow row from the authoritative JSON state."""

    return {
        "adapters": [],
        "adapter_model_backup": [],
        "desired_state_meta": [
            {"key": "shadow_schema_version", "value": "1"},
        ],
        "root_state": [
            {
                "id": 1,
                "schema_version": state.schema_version,
                "as_mailbox": int(state.as_mailbox),
            }
        ],
        "harnesses": [
            {
                "harness": item.harness,
                "name": item.name,
                "headless": int(item.headless),
                "args_json": _json_text(list(item.args)),
                "ownership": item.ownership,
                "nickname": item.nickname,
                "cwd": item.cwd,
                "endpoint": item.endpoint,
                "session_ref": item.session_ref,
                "command_json": _json_text(list(item.command)),
                "turn_timeout_seconds": item.turn_timeout_seconds,
                "idle_timeout_seconds": item.idle_timeout_seconds,
                "containerized": int(item.containerized),
                "pinned_owner": item.pinned_owner,
                "container_image": item.container_image,
                "execution_runtime_json": _json_text(item.execution_runtime.to_json()) if item.execution_runtime is not None else None,
                "model_vendor": item.model_provider,
                "model": item.model,
                # U0b (design ①): the harnesses table gained a status column;
                # the derivation follows the deliberate schema addition.  The
                # assertion's observation method (test-local forward mapping
                # vs the shadow's rows) is unchanged.
                "status": item.status,
            }
            for item in state.harnesses
        ],
        "interactive_sessions": [
            {
                "actor": item.actor,
                "cwd": item.cwd,
                "command_json": _json_text(list(item.command)),
                "source": item.source,
                "session_ref": item.session_ref,
                "runtime": item.runtime,
                "channel_confirmed": int(item.channel_confirmed),
                "channel_build_version": item.channel_build_version,
                "channel_protocol_version": item.channel_protocol_version,
                "owner_fence": (
                    None if item.owner_fence is None else int(item.owner_fence)
                ),
                "channel_lease_digest": item.channel_lease_digest,
                "tmux_session": item.tmux_session,
                "process_pid": item.process_pid,
                "process_identity": item.process_identity,
            }
            for item in state.interactive_sessions
        ],
        "pending_session_agent_effects": [
            {
                "effect_id": item.effect_id,
                "correlation_id": item.correlation_id,
                "operation": item.operation,
                "actor": item.actor,
                "harness": item.harness,
                "runtime": item.runtime,
                "session_id": item.session_id,
            }
            for item in state.pending_session_agent_effects
        ],
        "lifecycle_resources": [
            {
                "domain": item.domain,
                "resource_key": item.resource_key,
                "resource_token": item.resource_token,
                "active": int(item.active),
                "payload_json": _json_text(item.payload),
            }
            for item in state.lifecycle_resources
        ],
        "lifecycle_receipts": [
            {
                "domain": item.domain,
                "attempt_token": item.attempt_token,
                "operation_id": item.operation_id,
                "resource_key": item.resource_key,
                "expected_resource_token": item.expected_resource_token,
                "expected_entity_token": item.expected_entity_token,
                "created_by_operation": int(item.provenance.created_by_operation),
                "changed": int(item.provenance.changed),
                "resource_token": item.provenance.resource_token,
                "retired": int(item.retired),
                "completed": int(item.completed),
                "attempts": item.attempts,
                "correlation_id": item.correlation_id,
                "generation": item.generation,
                "version": item.version,
            }
            for item in state.lifecycle_receipts
        ],
        "channel_pins": [
            {"channel": channel, "agent": agent}
            for channel, agent in state.channel_pins
        ],
        "deprecated_shared_channels": [
            {"channel": channel} for channel in state.deprecated_shared_channels
        ],
        "zenoh_endpoints": [
            {"direction": direction, "ordinal": ordinal, "endpoint": endpoint}
            for direction, endpoints in (
                ("listen", state.zenoh.listen),
                ("connect", state.zenoh.connect),
            )
            for ordinal, endpoint in enumerate(endpoints)
        ],
        "service_connections": [
            {"name": item.name, "local_port": item.local_port}
            for item in state.service_connections
        ],
        "service_registry": (
            []
            if state.service_registry is None
            else [
                {
                    "id": 1,
                    "schema_version": state.service_registry.schema_version,
                    "space_id": state.service_registry.space_id,
                    "owner": state.service_registry.owner,
                    "cached_at_ms": state.service_registry.cached_at_ms,
                }
            ]
        ),
        "service_registry_entries": (
            []
            if state.service_registry is None
            else [
                {
                    "ordinal": ordinal,
                    "name": item.name,
                    "device_id": item.device_id,
                    "remote_port": item.remote_port,
                    "protocol": item.protocol,
                    "local_port": item.local_port,
                    "usage": item.usage,
                    "env_json": _json_text(dict(item.env)),
                    "auth": item.auth,
                    "guide": item.guide,
                }
                for ordinal, item in enumerate(state.service_registry.entries)
            ]
        ),
    }


def _assert_reconciled(store: DesiredStateStore, state_dir: Path) -> None:
    shadow = _read_shadow(state_dir / "lifecycle-operations.sqlite3")
    # The committed document, serialized by the store (save validated it
    # through from_json(to_json(...)) first); the row derivation below is
    # test-local and independent of the shadow's row mapping.
    committed = store.load().to_json()
    expected = _expected_tables(DesiredState.from_json(committed))
    assert set(shadow) == set(expected)
    for table, expected_rows in expected.items():
        key = _TABLE_KEY[table]
        actual_rows = sorted(shadow[table], key=lambda row: tuple(
            row[column] for column in key
        ))
        expected_sorted = sorted(expected_rows, key=lambda row: tuple(
            row[column] for column in key
        ))
        assert actual_rows == expected_sorted, (
            f"shadow table {table!r} diverges from the JSON authority"
        )


def _populate(store: DesiredStateStore) -> str:
    """Drive one representative write through every mirrored collection."""

    store.set_mailbox_role(True)
    store.upsert_harness(
        HarnessLaunchSpec(
            harness="codex",
            name="worker-1",
            headless=True,
            args=("--full-auto",),
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
            source="codex-app-server",
            session_ref="sess-abc",
            runtime="pi_repl",
            channel_confirmed=True,
            channel_build_version="0.4.4",
            channel_protocol_version=3,
            owner_fence=True,
            channel_lease_digest="a" * 64,
            tmux_session="actor-1",
            process_pid=4321,
            process_identity="proc-starttime:99",
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
        )
    )
    provenance, _completed = store.apply_harness_lifecycle(
        LifecycleMutationRequest(
            correlation_id="corr-h1",
            attempt_token="attempt-h1",
            operation_id="op-h1",
            expected_resource_token=None,
            payload=EnsureHarnessCommand(
                correlation_id="corr-h1",
                spec=HarnessLaunchProjection(
                    harness="jev",
                    name="relay-1",
                    headless=True,
                    args=("--token", "t"),
                ),
            ),
        ),
        generation=7,
        version=3,
    )
    store.record_harness_lifecycle_failure("attempt-h1", provenance.resource_token)
    store.complete_harness_lifecycle(
        "attempt-h1", provenance.resource_token, generation=9, version=4
    )
    store.set_channel_pin("chan-1", "agent-1")
    store.update_zenoh_endpoints(
        listen=("tcp://127.0.0.1:9001", "quic://127.0.0.1:9002"),
        connect=("tcp://peer.example:9003",),
    )
    return provenance.resource_token


def test_sqlite_rows_reconcile_after_mutations(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)
    # Read-only use leaves no store file behind: the database is lazy.
    store.load()
    assert not (tmp_path / "lifecycle-operations.sqlite3").exists()

    _populate(store)

    assert (tmp_path / "lifecycle-operations.sqlite3").exists()
    _assert_reconciled(store, tmp_path)


def test_sqlite_shadow_tracks_updates_and_deletes(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)
    _populate(store)

    store.remove_channel_pin("chan-1")
    store.unregister_interactive("actor-1")
    store.remove_harness("codex", "worker-1")
    assert store.complete_session_agent_effect("effect-1")
    receipt = store.harness_lifecycle_receipt("attempt-h1")
    assert receipt is not None
    assert store.retire_lifecycle_receipt(
        "harness", "attempt-h1", receipt.provenance.resource_token
    )
    store.confirm_lifecycle_receipt_retired(
        "harness", "attempt-h1", receipt.provenance.resource_token
    )

    _assert_reconciled(store, tmp_path)


def test_transaction_failure_lands_nothing_and_fails_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U0a-2 contract: a failed write lands nothing and fails save().

    Retires ``test_shadow_failure_is_observable_and_never_fails_save``
    (U0a-1).  That test pinned U0a-1's best-effort contract -- a broken
    mirror must not fail save(), the JSON stays authoritative, the lag is
    counted -- and it pinned that contract CORRECTLY; step U0a-2 is exactly
    the step authorized to supersede it.  The superseding design (Allen's
    ruling) went further than the first cut: the JSON projection is gone
    entirely, SQLite is the only storage, and ``shadow_write_failures`` /
    ``last_shadow_error_at`` are retired with the shadow they counted --
    every write failure now raises, so "no failure is silently absorbed"
    is a structural fact, not a counter reading.  The retired counter's
    duty -- surfacing write failures -- moved into ``save()`` raising,
    including the rollback-sentinel write at its two critical moments
    (migration finish, first save onto an empty store).  Do not re-add a
    failure counter for the sentinel: its steady-state write was removed
    on purpose.  The old test's residual
    windows (compensation failure, first-save compensation gap) do not
    exist here: there is no cross-storage window because there is only one
    storage.
    """

    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)
    _populate(store)
    baseline_tables = _read_shadow(tmp_path / "lifecycle-operations.sqlite3")
    baseline_state = store.load()

    def _broken_insert(self, db, document):  # noqa: ANN001
        raise RuntimeError("injected mid-transaction failure")

    monkeypatch.setattr(DesiredStateSqliteShadow, "_insert_all", _broken_insert)
    with pytest.raises(RuntimeError, match="injected mid-transaction failure"):
        store.set_mailbox_role(False)

    # save() failed -- and nothing landed: the rows are exactly the
    # pre-failure ones (the deletes rolled back with the failed inserts).
    assert (
        _read_shadow(tmp_path / "lifecycle-operations.sqlite3")
        == baseline_tables
    )
    assert store.load() == baseline_state


def test_sqlite_shadow_survives_store_reopen(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    first = DesiredStateStore(path)
    _populate(first)

    # A second store instance (fresh connection onto the same WAL database)
    # must keep the shadow reconciled with the JSON document.
    second = DesiredStateStore(path)
    second.set_mailbox_role(False)
    _assert_reconciled(second, tmp_path)
