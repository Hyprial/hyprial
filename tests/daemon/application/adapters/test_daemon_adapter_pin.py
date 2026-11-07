"""Adapter pin semantics: sqlite-backed storage, 1:1 binding, loud migration.

A pin binds one adapter to one existing agent on this machine.  The record of
truth is the agents database (``state/agents.sqlite3``, ``pins`` table, whose
UNIQUE constraints own the one-to-one rule); ``agent get`` serves the binding
as ``pinnedAdapters`` and ``adapter.pins`` is one query.  Legacy desired-state
``channelPins`` entries are drained by an idempotent, fail-loud migration.
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections
from hyprial.shell.impl.cli.commands.common import support as _cli_support

OWNER = "alice"
NODE = "node-1"
_APP_OWNER: ContextVar[Any | None] = ContextVar("adapter_pin_app_owner", default=None)


@pytest.fixture(autouse=True)
def _own_applications(own_daemon_application):  # type: ignore[no-untyped-def]
    # Every helper construction, including the two restart incarnations,
    # retains its own full composition and HomeLease teardown.
    token = _APP_OWNER.set(own_daemon_application)
    try:
        yield
    finally:
        _APP_OWNER.reset(token)


def _supervisor(names: tuple[str, ...]) -> AdapterRuntime:
    """Build the real supervisor; ``__init__`` only populates ``_gateways``.

    Exercising the genuine ``AdapterRuntime`` (rather than a fake) means the
    ``gateway_names`` accessor the daemon validates against is really the one
    derived from ``channels.gateways`` — a stub that answers any accessor would
    prove nothing.
    """

    launcher = SimpleNamespace(startup_timeout=20.0)
    channels = SimpleNamespace(gateways=[SimpleNamespace(name=name) for name in names])
    return AdapterRuntime(launcher, channels)  # type: ignore[arg-type]


class _EmptyInbox:
    """Just enough inbox for agent.destroy's message drain."""

    def pending_messages(self, actor: str) -> tuple[Any, ...]:
        return ()


def _application(tmp_path: Path, adapters: tuple[str, ...]) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        **_app_injections(),
    )
    app._inbox = _EmptyInbox()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app._adapters = _supervisor(adapters)
    owner = _APP_OWNER.get()
    if owner is None:
        raise RuntimeError("adapter-pin application owner fixture is missing")
    return owner(app)


def _canonical(actor: str) -> str:
    return f"agent:{OWNER}:{NODE}:{actor}"


def _agent_record(app: DaemonApplication, actor: str) -> dict[str, Any]:
    """The agent's JSON as agent.get serves it (pinnedAdapters included)."""

    return app.handle("agent.get", {"name": actor})["agent"]


def _seed_legacy_pins(app: DaemonApplication, pins: dict[str, str]) -> None:
    """Fabricate pre-rework desired-state ``channelPins`` entries."""

    for channel, agent in pins.items():
        app.desired_state.set_channel_pin(channel, agent)


def _log_events(tmp_path: Path, *, expected_event: str) -> list[dict[str, Any]]:
    path = tmp_path / "logs" / "daemon.jsonl"
    deadline = time.monotonic() + 3.0
    while True:
        events = (
            [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            if path.exists()
            else []
        )
        if any(item.get("event") == expected_event for item in events):
            return events
        assert time.monotonic() < deadline, f"{expected_event} was not written"
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ("adapter.pin", "adapter.unpin"))
def test_unknown_adapter_is_rejected_with_the_available_names(
    tmp_path: Path, method: str
) -> None:
    app = _application(tmp_path, ("lark-1", "lark-2"))

    params: dict[str, object] = {"name": "lark-9"}
    if method == "adapter.pin":
        params["actor"] = "worker"

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(method, params)

    assert failure.value.code == "ADAPTER_NOT_FOUND"
    message = str(failure.value)
    assert "lark-9" in message
    # The message must list the adapters that *are* configured.
    assert "lark-1" in message
    assert "lark-2" in message


def test_pin_of_a_nonexistent_agent_is_refused_with_the_create_remedy(
    tmp_path: Path,
) -> None:
    """Pin-before-create is gone: the target must already exist (D5)."""

    app = _application(tmp_path, ("lark-1",))

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("adapter.pin", {"name": "lark-1", "actor": "ghost"})

    assert failure.value.code == "AGENT_NOT_FOUND"
    assert "hyprial agent create --name ghost" in str(failure.value)
    # Nothing was written anywhere.
    assert app.handle("adapter.pins", {})["pins"] == {}
    assert dict(app.desired_state.load().channel_pins) == {}


def test_pin_of_a_foreign_machine_uri_names_the_reason(tmp_path: Path) -> None:
    """D6: pins bind local agents only, and the error must say why."""

    app = _application(tmp_path, ("lark-1",))
    foreign = "agent:bob:other-host:worker"

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("adapter.pin", {"name": "lark-1", "actor": foreign})

    assert failure.value.code == "AGENT_NOT_FOUND"
    message = str(failure.value)
    assert "other-host" in message
    assert "this machine" in message


# ---------------------------------------------------------------------------
# Writes: normalization, idempotency, 1:1
# ---------------------------------------------------------------------------


def test_pin_normalizes_a_bare_name_and_stores_it_on_the_agent_record(
    tmp_path: Path,
) -> None:
    app = _application(tmp_path, ("lark-1", "lark-2"))
    app.handle("agent.create", {"name": "worker"})

    result = app.handle("adapter.pin", {"name": "lark-1", "actor": "worker"})

    assert result["ok"] is True
    assert result["adapter"] == "lark-1"
    # The stored value is the canonical URI, never the bare spelling.
    assert result["actor"] == _canonical("worker")
    assert result["previous"] is None
    assert result["changed"] is True
    assert result["pins"] == {"lark-1": _canonical("worker")}
    # Record of truth: the agents database, as agent.get serves it.
    assert _agent_record(app, "worker")["pinnedAdapters"] == ["lark-1"]
    # Desired-state channelPins is legacy staging only; a new pin never
    # lands there.
    assert dict(app.desired_state.load().channel_pins) == {}


def test_pin_accepts_the_canonical_uri_spelling(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})

    result = app.handle(
        "adapter.pin", {"name": "lark-1", "actor": _canonical("worker")}
    )

    assert result["actor"] == _canonical("worker")
    assert _agent_record(app, "worker")["pinnedAdapters"] == ["lark-1"]


def test_pin_is_idempotent_and_reports_previous_on_move(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "one"})
    app.handle("agent.create", {"name": "two"})

    first = app.handle("adapter.pin", {"name": "lark-1", "actor": "one"})
    assert first["changed"] is True
    assert first["previous"] is None

    repeated = app.handle("adapter.pin", {"name": "lark-1", "actor": "one"})
    assert repeated["changed"] is False
    assert repeated["previous"] == _canonical("one")
    assert _agent_record(app, "one")["pinnedAdapters"] == ["lark-1"]

    # Moving the adapter to another agent updates BOTH records.
    moved = app.handle("adapter.pin", {"name": "lark-1", "actor": "two"})
    assert moved["changed"] is True
    assert moved["previous"] == _canonical("one")
    assert _agent_record(app, "one")["pinnedAdapters"] == []
    assert _agent_record(app, "two")["pinnedAdapters"] == ["lark-1"]
    assert app.handle("adapter.pins", {})["pins"] == {"lark-1": _canonical("two")}


def test_second_adapter_pinning_the_same_agent_is_refused(tmp_path: Path) -> None:
    """Adapter->agent AND agent->adapter are one-to-one for new writes."""

    app = _application(tmp_path, ("lark-1", "lark-2"))
    app.handle("agent.create", {"name": "worker"})
    app.handle("adapter.pin", {"name": "lark-1", "actor": "worker"})

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("adapter.pin", {"name": "lark-2", "actor": "worker"})

    assert failure.value.code == "PIN_CONFLICT"
    message = str(failure.value)
    # The error names the holder and the exact unbind command.
    assert "lark-1" in message
    assert "hyprial adapter unpin lark-1" in message
    # State unchanged.
    assert app.handle("adapter.pins", {})["pins"] == {
        "lark-1": _canonical("worker")
    }
    assert _agent_record(app, "worker")["pinnedAdapters"] == ["lark-1"]


@pytest.mark.parametrize("method", ["adapter.pin", "adapter.unpin"])
def test_conditional_write_rejects_a_stale_binding(tmp_path: Path, method: str) -> None:
    app = _application(tmp_path, ("lark-1",))
    for actor in ("old", "new"):
        app.handle("agent.create", {"name": actor})
    app.handle("adapter.pin", {"name": "lark-1", "actor": "new"})
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(method, {"name": "lark-1", "actor": "old", "expectedActor": _canonical("old")})
    assert failure.value.code == "PIN_STATE_CONFLICT"
    assert failure.value.data["currentActor"] == _canonical("new")
    assert app.handle("adapter.pins", {})["pins"] == {"lark-1": _canonical("new")}


@pytest.mark.parametrize("expected", [False, 3, {}, "", "worker"])
def test_conditional_write_rejects_invalid_expectation(tmp_path: Path, expected: object) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("adapter.pin", {"name": "lark-1", "actor": "worker", "expectedActor": expected})
    assert failure.value.code == "INVALID_ARGUMENT"
    assert app.handle("adapter.pins", {})["pins"] == {}


def test_conditional_write_accepts_explicit_unbound_and_matching_uri(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    result = app.handle("adapter.pin", {"name": "lark-1", "actor": "worker", "expectedActor": None})
    assert result["actor"] == _canonical("worker")
    result = app.handle("adapter.unpin", {"name": "lark-1", "expectedActor": _canonical("worker")})
    assert result["changed"] is True
    assert result["pins"] == {}


def test_pins_explicitly_advertise_atomic_expected_actor_support(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    assert app.handle("adapter.pins", {})["supportsExpectedActor"] is True


@pytest.mark.parametrize("method", ["adapter.pin", "adapter.unpin"])
def test_conditional_result_reports_transaction_observation_when_projection_is_stale(tmp_path: Path, method: str) -> None:
    app = _application(tmp_path, ("lark-1",))
    for name in ("old", "new", "target"):
        app.handle("agent.create", {"name": name})
    app.handle("adapter.pin", {"name": "lark-1", "actor": "old"})
    # An independent registry writer can move the binding before this actor's
    # projection catches up. A successful condition proves the actual prior URI.
    app._agent_registry.pin("lark-1", "new")
    result = app.handle(method, {"name": "lark-1", "actor": "target", "expectedActor": _canonical("new")})
    assert result["previous"] == _canonical("new")


def test_unpin_removes_the_record_entry_and_reports_previous(
    tmp_path: Path,
) -> None:
    app = _application(tmp_path, ("lark-1", "lark-2"))
    app.handle("agent.create", {"name": "one"})
    app.handle("agent.create", {"name": "two"})
    app.handle("adapter.pin", {"name": "lark-1", "actor": "one"})
    app.handle("adapter.pin", {"name": "lark-2", "actor": "two"})

    removed = app.handle("adapter.unpin", {"name": "lark-1"})

    assert removed["changed"] is True
    assert removed["previous"] == _canonical("one")
    assert removed["pins"] == {"lark-2": _canonical("two")}
    assert _agent_record(app, "one")["pinnedAdapters"] == []

    unpinned_again = app.handle("adapter.unpin", {"name": "lark-1"})
    assert unpinned_again["changed"] is False
    assert unpinned_again["previous"] is None


# ---------------------------------------------------------------------------
# Lifecycle: restart and destroy
# ---------------------------------------------------------------------------


def test_pins_survive_a_daemon_restart_via_the_registry_scan(
    tmp_path: Path,
) -> None:
    first = _application(tmp_path, ("lark-1",))
    first.handle("agent.create", {"name": "worker"})
    first.handle("adapter.pin", {"name": "lark-1", "actor": "worker"})

    # A new application over the same state dir is a daemon restart: the
    # index is rebuilt from the agent files alone.
    second = _application(tmp_path, ("lark-1",))
    assert second.handle("adapter.pins", {})["pins"] == {
        "lark-1": _canonical("worker")
    }


def test_destroying_the_agent_removes_its_pins_with_the_record(
    tmp_path: Path,
) -> None:
    """The reason pins live on the agent file: destroy leaves nothing behind."""

    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    app.handle("adapter.pin", {"name": "lark-1", "actor": "worker"})

    result = app.handle("agent.destroy", {"name": "worker"})

    assert result["destroyed"] is True
    assert result["unpinnedAdapters"] == ["lark-1"]
    assert app.handle("adapter.pins", {})["pins"] == {}
    assert app.agents.get("worker") is None
    # A rebuilt index (daemon restart) agrees: nothing lingers anywhere.
    assert _application(tmp_path, ("lark-1",)).handle("adapter.pins", {})[
        "pins"
    ] == {}


# ---------------------------------------------------------------------------
# Legacy channelPins migration: loud, idempotent, never a silent drop
# ---------------------------------------------------------------------------


def test_migration_normalizes_a_bare_name_onto_the_agent_record(
    tmp_path: Path,
) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    _seed_legacy_pins(app, {"lark-1": "worker"})

    app._migrate_legacy_channel_pins()

    assert app.handle("adapter.pins", {})["pins"] == {
        "lark-1": _canonical("worker")
    }
    assert _agent_record(app, "worker")["pinnedAdapters"] == ["lark-1"]
    # The staging entry is drained.
    assert dict(app.desired_state.load().channel_pins) == {}
    # Idempotent: a second run changes nothing.
    app._migrate_legacy_channel_pins()
    assert _agent_record(app, "worker")["pinnedAdapters"] == ["lark-1"]


def test_migration_accepts_an_already_canonical_value(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    _seed_legacy_pins(app, {"lark-1": _canonical("worker")})

    app._migrate_legacy_channel_pins()

    assert app.handle("adapter.pins", {})["pins"] == {
        "lark-1": _canonical("worker")
    }


def test_migration_keeps_an_unresolved_pin_and_reports_it_loudly(
    tmp_path: Path,
) -> None:
    """Fail-loud: an unresolvable value is kept, logged, and never routing."""

    app = _application(tmp_path, ("lark-1",))
    _seed_legacy_pins(app, {"lark-1": "ghost"})

    app._migrate_legacy_channel_pins()

    # Not migrated, not dropped.
    assert app.handle("adapter.pins", {})["pins"] == {}
    assert dict(app.desired_state.load().channel_pins) == {"lark-1": "ghost"}
    events = _log_events(tmp_path, expected_event="pin.migration.unresolved")
    unresolved = [
        item for item in events if item["event"] == "pin.migration.unresolved"
    ]
    assert len(unresolved) == 1
    assert unresolved[0]["level"] == "error"
    assert "hyprial agent create" in unresolved[0]["detail"]


def test_creating_the_missing_agent_migrates_the_kept_pin(tmp_path: Path) -> None:
    """The unresolved pin heals the moment its target starts existing."""

    app = _application(tmp_path, ("lark-1",))
    _seed_legacy_pins(app, {"lark-1": "ghost"})
    app._migrate_legacy_channel_pins()
    assert app.handle("adapter.pins", {})["pins"] == {}

    app.handle("agent.create", {"name": "ghost"})

    assert app.handle("adapter.pins", {})["pins"] == {
        "lark-1": _canonical("ghost")
    }
    assert dict(app.desired_state.load().channel_pins) == {}


def test_migration_drops_irc_channel_pins(tmp_path: Path) -> None:
    """The one decided exception to keep-everything: the feature is removed."""

    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    _seed_legacy_pins(app, {"#ops-room": "agent:irc", "lark-1": "worker"})

    app._migrate_legacy_channel_pins()

    assert dict(app.desired_state.load().channel_pins) == {}
    assert app.handle("adapter.pins", {})["pins"] == {
        "lark-1": _canonical("worker")
    }
    events = _log_events(tmp_path, expected_event="pin.migration.irc-dropped")
    assert any(item["event"] == "pin.migration.irc-dropped" for item in events)


def test_migration_keeps_a_one_to_one_conflict_and_reports_it(
    tmp_path: Path,
) -> None:
    """A legacy 1:1 conflict is kept unmigrated and reported, not resolved
    by guessing and not a startup failure."""

    app = _application(tmp_path, ("lark-a", "lark-b"))
    app.handle("agent.create", {"name": "worker"})
    _seed_legacy_pins(app, {"lark-a": "worker", "lark-b": "worker"})

    app._migrate_legacy_channel_pins()

    # Sorted order: lark-a migrates, lark-b is the conflict.
    assert app.handle("adapter.pins", {})["pins"] == {
        "lark-a": _canonical("worker")
    }
    assert dict(app.desired_state.load().channel_pins) == {"lark-b": "worker"}
    events = _log_events(tmp_path, expected_event="pin.migration.conflict")
    conflicts = [
        item for item in events if item["event"] == "pin.migration.conflict"
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["level"] == "error"
    assert "hyprial adapter unpin" in conflicts[0]["detail"]


def test_migration_keeps_an_unknown_adapter_key(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    _seed_legacy_pins(app, {"not-an-adapter": "worker"})

    app._migrate_legacy_channel_pins()

    assert app.handle("adapter.pins", {})["pins"] == {}
    assert dict(app.desired_state.load().channel_pins) == {
        "not-an-adapter": "worker"
    }
    events = _log_events(tmp_path, expected_event="pin.migration.unknown-adapter")
    assert any(
        item["event"] == "pin.migration.unknown-adapter" for item in events
    )


def test_explicit_pin_and_unpin_supersede_a_staged_legacy_entry(
    tmp_path: Path,
) -> None:
    """A deliberate operator write must not be resurrected-over by migration."""

    app = _application(tmp_path, ("lark-1",))
    _seed_legacy_pins(app, {"lark-1": "ghost"})
    app._migrate_legacy_channel_pins()  # ghost is unresolvable, entry kept

    app.handle("agent.create", {"name": "real"})
    result = app.handle("adapter.pin", {"name": "lark-1", "actor": "real"})
    # The superseded staging value is reported as the previous pin.
    assert result["previous"] == "ghost"
    assert dict(app.desired_state.load().channel_pins) == {}

    # And an unpin also clears staging so migration cannot resurrect it.
    _seed_legacy_pins(app, {"lark-1": "ghost"})
    removed = app.handle("adapter.unpin", {"name": "lark-1"})
    assert removed["previous"] == _canonical("real")
    assert dict(app.desired_state.load().channel_pins) == {}
    app._migrate_legacy_channel_pins()
    assert app.handle("adapter.pins", {})["pins"] == {}


# ---------------------------------------------------------------------------
# CLI seam
# ---------------------------------------------------------------------------


def _capture_request(monkeypatch: Any) -> list[tuple[str, object]]:
    """Patch the daemon IPC seam and record (method, params) the CLI sends."""

    calls: list[tuple[str, object]] = []

    def fake(method: str, params: object = None, **_kwargs: object) -> object:
        calls.append((method, params))
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", fake)
    return calls


def test_cli_pin_sends_name_and_actor_under_adapter_pin(monkeypatch: Any) -> None:
    calls = _capture_request(monkeypatch)

    result = CliRunner().invoke(
        app, ["adapter", "pin", "lark-1", "agent:alice", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [("adapter.pin", {"name": "lark-1", "actor": "agent:alice"})]


def test_cli_unpin_sends_name_under_adapter_unpin(monkeypatch: Any) -> None:
    calls = _capture_request(monkeypatch)

    result = CliRunner().invoke(app, ["adapter", "unpin", "lark-1", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [("adapter.unpin", {"name": "lark-1"})]


def test_cli_list_calls_adapter_list(monkeypatch: Any) -> None:
    calls = _capture_request(monkeypatch)

    result = CliRunner().invoke(app, ["adapter", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [("adapter.list", {})]


def test_cli_channel_pin_commands_are_gone() -> None:
    """The IRC channel-pin branch is removed outright."""

    for arguments in (
        ["channel", "pin", "#room", "agent:alice"],
        ["channel", "unpin", "#room"],
        ["channel", "pins"],
    ):
        result = CliRunner().invoke(app, [*arguments, "--json"])
        assert result.exit_code != 0, arguments
