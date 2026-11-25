"""UUID addressing lane A: presence and routes announce the ``a.`` id URI.

With id-form minting on, a registered agent's record stores
``agent:<owner>:<node>:a.<id>``.  Every runtime key the daemon derives for
that agent -- lifecycle route, liveliness token, zenoh inbox endpoint, local
delivery registration, headless pull key, interactive session actor and the
shared actor-status rows -- must be that stored URI, never one composed from
the short name.  Registry minting itself belongs to the identity lane, so
the fixture stands in for it by storing the id URI at record creation.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import hyprial.identity.impl.agents.addressing as addressing
from hyprial.daemon.impl.composition import (
    AgentIdentityProjectionView,
    CorrelatedDomainEvents,
    HarnessPortClient,
)
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.transport.keys import KeySpace
from hyprial.identity import AgentRegistry
from hyprial.kernel import HarnessLaunchSpec, canonical_agent_uri
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _Inbox,
    _Launcher,
    _Presence,
    _Registration,
    _close_application,
    _isolated_application,
)

OWNER = "owner"
NODE = "node-1"
NAME_URI = f"agent:{OWNER}:{NODE}:worker"


class _KeyedTransport:
    def __init__(self) -> None:
        self.keys: list[tuple[str, str]] = []

    def _record(self, kind: str, key: str) -> _Registration:
        self.keys.append((kind, key))
        return _Registration()

    def declare_liveliness(self, key: str) -> _Registration:
        return self._record("liveliness", key)

    def subscribe(self, key: str, _callback: object) -> _Registration:
        return self._record("subscribe", key)

    def declare_queryable(self, key: str, _handler: object) -> _Registration:
        return self._record("queryable", key)


class _KeyedLocalDelivery:
    def __init__(self) -> None:
        self.actors: list[str] = []

    def register_actor(self, actor: str) -> _Registration:
        self.actors.append(actor)
        return _Registration()


@pytest.fixture
def id_form(monkeypatch):
    """Switch on id-form minting and store the id URI on new local records."""

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)

    def minting(original):
        def create(self, agent):
            if agent.hosted_by is None:
                agent = replace(
                    agent,
                    uri=addressing.mint_agent_uri(
                        agent.owner, agent.machine,
                        actor=agent.actor, agent_id=agent.agent_id,
                    ),
                )
            return original(self, agent)

        return create

    for method in ("_create_record", "prepare_create_record"):
        monkeypatch.setattr(
            AgentRegistry, method, minting(getattr(AgentRegistry, method))
        )


@pytest.fixture
def started(tmp_path: Path, request, id_form):
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    events = CorrelatedDomainEvents()
    harness_actor = HarnessRuntimeActor(
        _Launcher(),
        event_sink=events,
        desired_state=app.desired_state,
        agent_identity=AgentIdentityProjectionView(app._agent_session_domains),
    )
    harnesses = HarnessPortClient(harness_actor, events)
    app._harnesses = harnesses
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    transport = _KeyedTransport()
    delivery = _KeyedLocalDelivery()
    app._start_lifecycle_manager(
        transport=transport,  # type: ignore[arg-type]
        inbox=app._inbox,  # type: ignore[arg-type]
        local_delivery=delivery,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
    )
    agent = app.agents.create("worker")
    id_uri = canonical_agent_uri(OWNER, NODE, agent.agent_id)
    assert agent.uri == id_uri
    return app, transport, delivery, id_uri


def test_lifecycle_route_registration_keys_on_the_id_uri(started) -> None:
    app, transport, delivery, id_uri = started
    spec = HarnessLaunchSpec("codex", "worker", True)

    lifecycle = app._lifecycle_spec(spec)
    assert lifecycle.actor == lifecycle.route.route_id == id_uri
    app.handle("lifecycle.start", spec.to_json())

    keys = KeySpace()
    assert delivery.actors == [id_uri]
    assert ("liveliness", keys.actor_liveliness(id_uri)) in transport.keys
    assert ("subscribe", keys.inbox_all(id_uri)) in transport.keys
    assert not any(NAME_URI in KeySpace.decode_identity(part)
                   for _kind, key in transport.keys for part in key.split("/"))
    assert app._routes.owners(id_uri)
    assert app._routes.owners(NAME_URI) == ()


def test_headless_pull_key_is_the_id_uri(started) -> None:
    app, _transport, _delivery, id_uri = started

    assert app._canonical_harness_uri("worker") == id_uri
    assert app._agent_address("worker") == id_uri
    identity = AgentIdentityProjectionView(app._agent_session_domains)
    assert identity.actor_name(id_uri) == "worker"
    assert identity.entity_token(id_uri) == identity.entity_token("worker")
    assert app._agent_address("squire") == f"agent:{OWNER}:{NODE}:squire"


def test_started_runtime_bridge_pulls_headless_work_at_the_id_uri(
    tmp_path: Path, monkeypatch, id_form
) -> None:
    from hyprial.daemon.impl.application.wiring import transport as wiring
    from tests.support.runtime.cell_delivery.composition_probe import _probe
    from tests.daemon.progress.test_progress_end_to_end import _CompositionRuntime

    app, _captured, _links = _probe(monkeypatch, tmp_path)
    wired: dict[str, object] = {}

    class _Bridge(_CompositionRuntime):
        def __init__(self, **kwargs: object) -> None:
            wired.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(wiring, "DaemonEventBridge", _Bridge)
    try:
        agent = app.agents.create("worker")
        app._start_runtime()
        pull_key = wired["harness_actor_uri"]
        assert callable(pull_key)
        assert pull_key("worker") == agent.uri == canonical_agent_uri(
            "op", "node-1", agent.agent_id
        )
        assert pull_key("squire") == "agent:op:node-1:squire"
    finally:
        app._close()
        app.agents.close()


def test_dormant_wake_matches_work_queued_at_the_id_uri(started) -> None:
    app, _transport, _delivery, id_uri = started
    queued: dict[str, tuple[str, ...]] = {id_uri: ("m-1",)}

    class _Queue:
        def has_pending_work(self, recipient: str) -> bool:
            return bool(queued.get(recipient))

    app._inbox = _Queue()  # type: ignore[assignment]
    agent = app.agents.require("worker")

    assert app._pending_restore_work(agent) is True
    queued.clear()
    assert app._pending_restore_work(agent) is False


def test_worker_liveness_matches_the_id_uri_without_a_snapshot(started) -> None:
    app, _transport, _delivery, id_uri = started

    class _Supervisor:
        def status(self):
            return ({"name": "worker", "runtime": "codex", "running": True},)

    app._harnesses = _Supervisor()  # type: ignore[assignment]

    assert app._managed_worker_running(id_uri) is True
    assert app._managed_worker_running(NAME_URI) is None


def test_interactive_session_actor_is_the_id_uri(started) -> None:
    app, _transport, _delivery, id_uri = started

    assert app._canonical_interactive_actor("worker") == id_uri
    assert app._canonical_interactive_actor(NAME_URI) == id_uri
    assert app._canonical_interactive_actor(id_uri) == id_uri
    assert app._canonical_interactive_actor("visitor") == (
        f"agent:{OWNER}:{NODE}:visitor"
    )
    foreign = f"agent:peer:{NODE}:worker"
    assert app._canonical_interactive_actor(foreign) == foreign


def test_name_form_is_unchanged_with_the_switch_off(
    tmp_path: Path, request, monkeypatch
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    app.agents.create("worker")

    assert app._canonical_harness_uri("worker") == NAME_URI
    assert app._canonical_interactive_actor("worker") == NAME_URI


def _register_session(app, actor: str, **extra: object) -> dict[str, object]:
    return app.handle("session.register", {
        "actor": actor, "sessionRef": "visitor-session", "cwd": "/work",
        "command": ["codex", "--remote"], "source": "codex-app-server",
        "runtime": "codex_interactive", **extra,
    })


@pytest.mark.parametrize("spelling", ["visitor", f"agent:{OWNER}:{NODE}:visitor"])
def test_first_session_registration_keys_the_new_agent_at_its_id_uri(
    started, spelling: str
) -> None:
    app, _transport, _delivery, _id_uri = started
    name_uri = f"agent:{OWNER}:{NODE}:visitor"
    assert app.agents.get("visitor") is None

    result = _register_session(app, spelling)

    agent = app.agents.require("visitor")
    assert agent.uri == canonical_agent_uri(OWNER, NODE, agent.agent_id)
    assert result["actor"] == agent.uri
    session = app._agent_session_domains.session
    assert session.read_session(agent.uri).session_ref == "visitor-session"
    assert session.read_session(name_uri) is None
    assert [item.actor for item in app.desired_state.load().interactive_sessions] == [
        agent.uri
    ]
    refreshed = app.handle(
        "session.refresh", {"actor": spelling, "sessionRef": "visitor-session"}
    )
    assert refreshed["actor"] == agent.uri


def test_first_session_registration_starts_turn_reporting_at_the_id_uri(
    started,
) -> None:
    app, _transport, _delivery, _id_uri = started

    _register_session(
        app, "visitor", source="claude-channel", runtime="claude_interactive",
        channelConfirmed=True, turnReporting=True,
    )

    agent = app.agents.require("visitor")
    assert app._interactive_turn_stats[agent.uri].session_ref == "visitor-session"
    assert f"agent:{OWNER}:{NODE}:visitor" not in app._interactive_turn_stats


def test_first_session_registration_keeps_the_name_uri_with_the_switch_off(
    tmp_path: Path, request, monkeypatch
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    app._harnesses = SimpleNamespace(status=lambda: ())  # type: ignore[assignment]
    name_uri = f"agent:{OWNER}:{NODE}:visitor"

    result = _register_session(app, name_uri)

    assert app.agents.require("visitor").uri == name_uri
    assert result["actor"] == name_uri
    assert app._agent_session_domains.session.read_session(name_uri) is not None
    assert [item.actor for item in app.desired_state.load().interactive_sessions] == [
        name_uri
    ]
