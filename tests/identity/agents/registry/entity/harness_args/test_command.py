"""Atomic harness-argument command, audit, and concurrency contracts."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from hyprial.daemon.impl.composition.events import DomainCommandError
from hyprial.identity.impl.agents import AgentRegistry

from ..support import (
    NODE,
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
)


def _audit_rows(database: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return list(connection.execute("SELECT * FROM agent_harness_args_audit ORDER BY seq"))


def _stored_args(database: Path) -> dict[str, list[str]]:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT harness_args FROM agents WHERE actor = 'foo'"
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def _record_session(registry: AgentRegistry, errors: list[BaseException]) -> None:
    try:
        registry.record_session("foo", harness="pi", session_id="session-1")
    except BaseException as error:
        errors.append(error)


def test_facade_set_replaces_one_group_and_records_redacted_audit(tmp_path: Path) -> None:
    from hyprial.kernel import redact_harness_args

    app = application(tmp_path)
    app.agents.create("foo", harness_args={"pi": ("--offline",)})
    result = app.agents.set_harness_args(
        "foo",
        harness="claude",
        args=("--settings", '{"token":"private"}'),
        expected_harness_args={"pi": ("--offline",)},
        operator="local-operator:test",
        reason="ticket\x00-1",
        correlation_id="set-1",
        confirmed_dangerous=True,
    )

    expected = {
        "claude": ("--settings", '{"token":"private"}'),
        "pi": ("--offline",),
    }
    assert result.changed is True
    assert result.audit_id == "agent_harness_args_audit:1"
    assert app.agents.require("foo").harness_args == expected
    row = _audit_rows(tmp_path / "agents.sqlite3")[0]
    assert row["after_digest"] == redact_harness_args(expected).digest
    assert "private" not in row["after_json"]
    assert json.loads(row["dangerous_json"]) == [
        {
            "category": "code-execution",
            "reason": "loads executable or trusted configuration",
            "token": "--settings",
        }
    ]
    assert row["confirmed_dangerous"] == 1
    assert row["operator"] == "local-operator:test"
    assert row["reason"] == "ticket-1"


def test_idempotent_set_and_missing_clear_do_not_write_audit(tmp_path: Path) -> None:
    app = application(tmp_path)
    current = {"pi": ("--offline",)}
    app.agents.create("foo", harness_args=current)

    same = app.agents.set_harness_args(
        "foo", harness="pi", args=("--offline",),
        expected_harness_args=current, operator="local-operator:test",
        reason=None, correlation_id="same",
    )
    missing = app.agents.set_harness_args(
        "foo", harness="claude", args=(),
        expected_harness_args=current, operator="local-operator:test",
        reason=None, correlation_id="missing",
    )

    assert same.changed is False and same.audit_id is None
    assert missing.changed is False and missing.audit_id is None
    assert _audit_rows(tmp_path / "agents.sqlite3") == []


def test_empty_groups_are_canonicalized_as_absent(tmp_path: Path) -> None:
    app = application(tmp_path)
    created = app.agents.create("foo", harness_args={"claude": ()})

    assert created.harness_args == {}
    result = app.agents.set_harness_args(
        "foo", harness="claude", args=(), expected_harness_args={},
        operator="local-operator:test", reason=None, correlation_id="empty",
    )
    assert result.changed is False
    assert app.agents.current("foo").harness_args == {}


def test_empty_args_delete_only_the_target_group(tmp_path: Path) -> None:
    app = application(tmp_path)
    current = {
        "claude": ("--model", "sonnet"),
        "pi": ("--offline",),
    }
    app.agents.create("foo", harness_args=current)

    result = app.agents.set_harness_args(
        "foo", harness="claude", args=(), expected_harness_args=current,
        operator="local-operator:test", reason=None, correlation_id="clear-one",
    )

    assert result.changed is True
    assert app.agents.current("foo").harness_args == {"pi": ("--offline",)}
    assert _audit_rows(tmp_path / "agents.sqlite3")[0]["action"] == "clear"


def test_clear_all_accepts_historical_groups_and_redacts_the_before_map(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    app.agents.create("foo")
    historical = {"legacy": ("--token=private",), "pi": ("--offline",)}
    with closing(sqlite3.connect(tmp_path / "agents.sqlite3")) as database, database:
        database.execute(
            "UPDATE agents SET harness_args = ? WHERE actor = 'foo'",
            (json.dumps({key: list(value) for key, value in historical.items()}),),
        )

    result = app.agents.set_harness_args(
        "foo", harness=None, args=None, expected_harness_args=historical,
        operator="local-operator:test", reason=None, correlation_id="clear-all",
    )

    assert result.changed is True
    assert app.agents.current("foo").harness_args == {}
    row = _audit_rows(tmp_path / "agents.sqlite3")[0]
    assert row["action"] == "clear-all"
    assert "private" not in row["before_json"]


def test_full_map_cas_conflict_preserves_row_and_writes_no_audit(tmp_path: Path) -> None:
    app = application(tmp_path)
    current = {"pi": ("--offline",)}
    app.agents.create("foo", harness_args=current)

    with pytest.raises(DomainCommandError) as failure:
        app.agents.set_harness_args(
            "foo", harness="claude", args=("--model", "sonnet"),
            expected_harness_args={}, operator="local-operator:test",
            reason=None, correlation_id="conflict",
        )

    assert failure.value.code == "HARNESS_ARGS_CONFLICT"
    assert app.agents.current("foo").harness_args == current
    assert _audit_rows(tmp_path / "agents.sqlite3") == []


@pytest.mark.parametrize(
    "trigger",
    [
        "CREATE TRIGGER fail_write BEFORE UPDATE OF harness_args ON agents "
        "BEGIN SELECT RAISE(ABORT, 'agent update failed'); END",
        "CREATE TRIGGER fail_audit BEFORE INSERT ON agent_harness_args_audit "
        "BEGIN SELECT RAISE(ABORT, 'audit insert failed'); END",
    ],
)
def test_agent_update_and_audit_failures_roll_back_both_sides(
    tmp_path: Path, trigger: str,
) -> None:
    app = application(tmp_path)
    app.agents.create("foo", harness_args={"pi": ("--offline",)})
    database_path = tmp_path / "agents.sqlite3"
    with closing(sqlite3.connect(database_path)) as database, database:
        database.execute(trigger)

    with pytest.raises(DomainCommandError) as failure:
        app.agents.set_harness_args(
            "foo", harness="claude", args=("--model", "sonnet"),
            expected_harness_args={"pi": ("--offline",)},
            operator="local-operator:test", reason=None, correlation_id="failure",
        )

    assert failure.value.code == "AGENT_ERROR"
    assert _stored_args(database_path) == {"pi": ["--offline"]}
    assert _audit_rows(database_path) == []


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--settings", "{}"), "HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED"),
        (("--json",), "HARNESS_ARGS_INVALID"),
    ],
)
def test_policy_rejection_is_typed_and_has_no_success_audit(
    tmp_path: Path, args: tuple[str, ...], code: str,
) -> None:
    app = application(tmp_path)
    app.agents.create("foo")

    with pytest.raises(DomainCommandError) as failure:
        app.agents.set_harness_args(
            "foo", harness="claude", args=args, expected_harness_args={},
            operator="local-operator:test", reason=None, correlation_id="policy",
        )

    assert failure.value.code == code
    assert app.agents.current("foo").harness_args == {}
    assert _audit_rows(tmp_path / "agents.sqlite3") == []


def test_concurrent_record_session_cannot_restore_stale_harness_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo")
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original_save = registry.save

    def delayed_save(agent, *, preserve_harness_args=False):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(2)
        return original_save(agent, preserve_harness_args=preserve_harness_args)

    monkeypatch.setattr(registry, "save", delayed_save)
    thread = threading.Thread(
        target=lambda: _record_session(registry, errors), daemon=True
    )
    try:
        thread.start()
        assert entered.wait(2)
        registry.set_harness_args(
            "foo", harness="pi", args=("--offline",),
            expected_harness_args={}, operator="local-operator:test",
            reason=None, correlation_id="concurrent", confirmed_dangerous=False,
        )
        release.set()
        thread.join(2)
        assert not thread.is_alive() and errors == []
        stored = registry.require("foo")
        assert stored.harness_args == {"pi": ("--offline",)}
        assert stored.last_session_id == "session-1"
    finally:
        release.set()
        registry.close()
