# Decision record (UUID addressing flip, 2026-11-03):
# - A first ``session.register`` for a name with no agent record creates the
#   record inside the registration.  The actor it arrived under was composed
#   before the record existed, so the session is stored under the record's
#   official address (its ``a.`` id URI once id-form minting is on), never
#   the name URI.  Every later session call canonicalizes the name to that
#   same address, so it must find the session there.
# - With id-form minting off the official address is the name URI and the
#   registration is byte-for-byte what it was before the flip.
# - The record is created (or found) before the per-actor route coordination
#   key is chosen, under a per-name boundary, so concurrent first
#   registrations for one new name all serialize and fence under the id
#   address: one session at the id URI, none at the name URI.
"""A first interactive registration lands at the agent's official address."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import DesiredStateStore, InteractiveSession
from hyprial.identity.impl.agents import addressing
from hyprial.daemon.impl.network.routing.session_route_coordinator import (
    SessionRouteKind,
)
from hyprial.kernel import CHANNEL_PROTOCOL_VERSION

from ..support import (
    _close_application_domains as _close_application_domains,
    FakeInbox,
    application,
    pending_message,
)

_LEASE = "lease-session".ljust(32, "0")


def _register(app, actor: str = "alice", session_ref: str = "session-1"):  # type: ignore[no-untyped-def]
    return app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": session_ref,
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelConfirmed": True,
            "channelProtocolVersion": CHANNEL_PROTOCOL_VERSION,
            "channelLeaseToken": _LEASE,
            "turnReporting": True,
        },
    )


def test_first_register_stores_the_session_at_the_minted_id_address(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    result = _register(app)

    agent = app.agents.get("alice")
    assert agent is not None
    assert agent.uri == f"agent:op:node-1:{agent.agent_id}"
    assert result["actor"] == agent.uri
    sessions = app._agent_session_domains.session.read_sessions()
    assert [(item.actor, item.session_ref) for item in sessions] == [
        (agent.uri, "session-1")
    ]
    assert app._interactive_turn_stats[agent.uri].session_ref == "session-1"
    for actor in ("alice", "agent:op:node-1:alice", agent.uri):
        assert app.handle(
            "session.refresh",
            {"actor": actor, "sessionRef": "session-1", "channelLeaseToken": _LEASE},
        )["refreshed"] is True
        assert app.handle(
            "session.heartbeat",
            {"actor": actor, "sessionRef": "session-1", "channelLeaseToken": _LEASE},
        )["alive"] is True


def test_second_register_supersedes_the_first_at_one_address(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    _register(app, session_ref="session-old")
    _register(app, session_ref="session-new")

    agent = app.agents.get("alice")
    assert agent is not None
    sessions = app._agent_session_domains.session.read_sessions()
    assert [(item.actor, item.session_ref) for item in sessions] == [
        (agent.uri, "session-new")
    ]


def test_first_register_keeps_the_name_address_with_minting_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    app = application(tmp_path, FakeInbox(pending_message()))

    result = _register(app)

    agent = app.agents.get("alice")
    assert agent is not None
    assert agent.uri == "agent:op:node-1:alice"
    assert result["actor"] == "agent:op:node-1:alice"
    sessions = app._agent_session_domains.session.read_sessions()
    assert [(item.actor, item.session_ref) for item in sessions] == [
        ("agent:op:node-1:alice", "session-1")
    ]
    assert app._interactive_turn_stats["agent:op:node-1:alice"].session_ref == (
        "session-1"
    )


@pytest.mark.parametrize("id_form", [True, False], ids=["id-form", "name-form"])
def test_startup_normalization_is_what_the_session_domain_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, id_form: bool
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", id_form)
    DesiredStateStore(tmp_path / "desired-state.json").register_interactive(
        InteractiveSession(
            actor="agent:op:node-1:alice",
            cwd="/work/alice",
            command=("codex",),
            source="codex-app-server",
            session_ref="dead-session",
            runtime="codex_interactive",
            process_pid=999999,
            process_identity="0",
        )
    )
    app = application(tmp_path, FakeInbox(pending_message()))
    address = app.agents.require("alice").uri
    assert (address != "agent:op:node-1:alice") is id_form

    app._normalize_persisted_interactive_sessions()

    assert [s.actor for s in app.desired_state.load().interactive_sessions] == [
        address
    ]
    assert [
        s.actor for s in app._agent_session_domains.session.read_sessions()
    ] == [address]
    assert app._clean_dead_interactive_sessions() == ()
    assert set(app._agent_recovery_cleanups) == {address}
    assert app.desired_state.load().interactive_sessions == ()


@pytest.mark.parametrize("id_form", [True, False], ids=["id-form", "name-form"])
def test_a_hosted_carrier_name_uri_registers_at_the_hosted_record_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, id_form: bool
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", id_form)
    app = application(tmp_path, FakeInbox(pending_message()))
    app.agents.create_transfer_hosted("visitor", pinned_owner="guest")
    hosted = app.agents.get("visitor")
    assert hosted is not None
    name_uri = "agent:guest:node-1:visitor"
    assert (hosted.uri != name_uri) is id_form

    result = _register(app, actor=name_uri, session_ref="visitor-session")

    assert result["actor"] == hosted.uri
    sessions = app._agent_session_domains.session.read_sessions()
    assert [(item.actor, item.session_ref) for item in sessions] == [
        (hosted.uri, "visitor-session")
    ]
    assert app.handle(
        "session.heartbeat",
        {
            "actor": name_uri,
            "sessionRef": "visitor-session",
            "channelLeaseToken": _LEASE,
        },
    )["alive"] is True


@pytest.mark.parametrize("id_form", [True, False], ids=["id-form", "name-form"])
def test_concurrent_first_registrations_serialize_under_one_official_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, id_form: bool
) -> None:
    """Both registrations map their actor before either record exists.

    The barrier holds each thread right after the boundary mapping, so both
    carry the name URI; the registration must still create the record first
    and route both under its official address.
    """

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", id_form)
    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    name_uri = "agent:op:node-1:alice"
    mapped = threading.Barrier(2, timeout=30)
    held = threading.local()
    real_mcp_actor = app._mcp_actor
    real_call_route = app._call_session_route
    route_keys: list[str] = []

    def map_then_wait(params):  # type: ignore[no-untyped-def]
        actor = real_mcp_actor(params)
        if not getattr(held, "done", False):
            held.done = True
            mapped.wait()
        return actor

    def record_route(kind, *, actor, **kwargs):  # type: ignore[no-untyped-def]
        if kind is SessionRouteKind.REGISTER:
            route_keys.append(actor)
        return real_call_route(kind, actor=actor, **kwargs)

    monkeypatch.setattr(app, "_mcp_actor", map_then_wait)
    monkeypatch.setattr(app, "_call_session_route", record_route)
    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def run(session_ref: str) -> None:
        try:
            results[session_ref] = _register(app, session_ref=session_ref)
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [
        threading.Thread(target=run, args=(ref,)) for ref in ("session-a", "session-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

    agent = app.agents.get("alice")
    assert agent is not None
    expected = f"agent:op:node-1:{agent.agent_id}" if id_form else name_uri
    assert agent.uri == expected
    sessions = app._agent_session_domains.session.read_sessions()
    assert [item.actor for item in sessions] == [expected]
    assert sessions[0].session_ref in {"session-a", "session-b"}
    assert {ref: result["actor"] for ref, result in results.items()} == {
        "session-a": expected,
        "session-b": expected,
    }
    assert route_keys == [expected, expected]
