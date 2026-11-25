"""UUID addressing lane A: daemon name-to-address mint sites use the registry.

Each test drives one site that used to compose
``agent:<owner>:<node>:<name>`` from an agent name and checks that, with
id-form minting on, it answers the stored ``a.`` id URI instead.  Service
actors with no registry record keep their name address.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel import canonical_agent_uri
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _close_application,
    _isolated_application,
)
from tests.daemon.routing.addressing.test_id_form_presence import (
    NODE,
    OWNER,
    id_form as id_form,
)


@pytest.fixture
def app_with_worker(tmp_path: Path, request, id_form):
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    agent = app.agents.create("worker")
    id_uri = canonical_agent_uri(OWNER, NODE, agent.agent_id)
    assert agent.uri == id_uri
    return app, id_uri


def test_historical_inbox_recipient_points_at_the_official_address(
    app_with_worker,
) -> None:
    app, id_uri = app_with_worker
    stale = f"agent:{OWNER}:old-node:worker"
    app._inbox = SimpleNamespace(  # type: ignore[assignment]
        pending_recipient_counts=lambda: ((stale, 2),),
    )

    assert app._historical_inbox_recipients() == [
        {"recipient": stale, "currentRecipient": id_uri, "pending": 2}
    ]


def test_service_actor_keeps_its_name_address(app_with_worker) -> None:
    app, _id_uri = app_with_worker

    assert app._agent_address("squire") == f"agent:{OWNER}:{NODE}:squire"


def _routine_app(coordinator_uri: str):
    from hyprial.daemon.impl.application import DaemonApplication

    app = object.__new__(DaemonApplication)
    app.owner = "test"
    app.node_id = "isolated"
    record = SimpleNamespace(owner="test", uri=coordinator_uri)
    app.agents = SimpleNamespace(  # type: ignore[assignment]
        get=lambda key: record if key == "coordinator" else None
    )
    return app


def test_unreadable_routine_retire_reports_the_official_address(id_form) -> None:
    official = "agent:test:isolated:a." + "d" * 26
    app = _routine_app(official)
    app.desired_state = SimpleNamespace(
        load=lambda: SimpleNamespace(
            harnesses=(
                SimpleNamespace(
                    name="coordinator",
                    nickname="routine:selfdrive:coordinator",
                    harness="codex",
                ),
            )
        )
    )
    app.handle = lambda method, params: {"removed": True}

    result = app._retire_routine_coordinator(
        {"name": "selfdrive", "produces": "x", "schemaError": "bad"}
    )

    assert result == {"actor": official, "retired": True, "changed": True}


@pytest.mark.parametrize("minting", [False, True], ids=["switch-off", "switch-on"])
def test_first_registration_and_start_answer_the_record_address(
    tmp_path: Path, monkeypatch, minting: bool
) -> None:
    """A record created by session.register or lifecycle.start is minted
    after the boundary read the name, so both answer the stored URI (the
    name URI with the switch off, the a.<id> URI with it on)."""

    from hyprial.identity.impl.agents import addressing
    from tests.identity.agents.registry.entity.support import (
        NODE as ENTITY_NODE,
        OWNER as ENTITY_OWNER,
        application,
        register_interactive,
        start_headless,
    )

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", minting)
    app = application(tmp_path)
    try:
        registered = register_interactive(
            app, canonical_agent_uri(ENTITY_OWNER, ENTITY_NODE, "tui")
        )
        tui = app.agents.require("tui").uri
        assert registered["actor"] == tui
        sessions = app.desired_state.load().interactive_sessions
        assert [session.actor for session in sessions] == [tui]
        assert app.handle(
            "session.unregister", {"actor": "tui", "sessionRef": "session-1"}
        )["unregistered"] is True

        started = start_headless(app, "pi", "worker")
        assert started["actor"] == app.agents.require("worker").uri
        segment = "a." if minting else "worker"
        assert started["actor"].startswith(
            f"agent:{ENTITY_OWNER}:{ENTITY_NODE}:{segment}"
        )
    finally:
        app._close()
        app._close_home_lease()


@pytest.mark.parametrize("minting", [False, True], ids=["switch-off", "switch-on"])
def test_agent_create_of_a_taken_name_names_destroy_as_the_remedy(
    tmp_path: Path, monkeypatch, minting: bool
) -> None:
    """A destroy frees the name (owner decision 2026-11-19), so AGENT_EXISTS
    advises destroying the holder to reuse it (integrator decision D10)."""

    from hyprial.identity.impl.agents import addressing
    from hyprial.kernel import DaemonRequestError
    from tests.identity.agents.registry.entity.support import application

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", minting)
    app = application(tmp_path)
    try:
        app.handle("agent.create", {"name": "foo"})
        holder = app.agents.require("foo").uri
        with pytest.raises(DaemonRequestError) as failure:
            app.handle("agent.create", {"name": "foo"})
        assert failure.value.code == "AGENT_EXISTS"
        assert failure.value.data == {"actor": holder, "agent": "foo"}
        message = str(failure.value)
        assert holder in message
        reuse = "To reuse the name, destroy that agent first ('hyprial agent destroy foo'"
        assert reuse in message
        assert "freezes" not in message
    finally:
        app._close()
        app._close_home_lease()


@pytest.mark.parametrize("minting", [False, True], ids=["switch-off", "switch-on"])
def test_lifecycle_start_without_a_record_plans_the_record_address(
    tmp_path: Path, monkeypatch, minting: bool
) -> None:
    """Integrator decision D11: a raw lifecycle.start for a name with no
    record (routine coordinators, IPC callers that skip agent.create) plans
    its actor, route, liveliness and inbox keys at the address of the record
    the start creates, never at a name route."""

    from hyprial.daemon.impl.transport.keys import KeySpace
    from hyprial.identity.impl.agents import addressing
    from tests.identity.agents.registry.entity.support import (
        NODE as ENTITY_NODE,
        OWNER as ENTITY_OWNER,
        application,
        start_headless,
    )

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", minting)
    app = application(tmp_path)
    try:
        planned = []
        submit = app._lifecycle_manager.submit

        def record(operation):
            planned.append(operation.target)
            return submit(operation)

        monkeypatch.setattr(app._lifecycle_manager, "submit", record)
        started = start_headless(app, "pi", "fresh")

        agent = app.agents.require("fresh")
        segment = agent.agent_id if minting else "fresh"
        assert agent.uri == canonical_agent_uri(ENTITY_OWNER, ENTITY_NODE, segment)
        (target,) = planned
        keys = KeySpace()
        assert (target.actor, target.route.route_id) == (agent.uri, agent.uri)
        assert target.route.liveliness_key == keys.actor_liveliness(agent.uri)
        assert target.route.inbox_key == keys.inbox_all(agent.uri)
        assert started["actor"] == agent.uri
        binding = app._agent_liveness.binding(agent.uri)
        assert binding is not None and binding.runtime == "headless"
    finally:
        app._close()
        app._close_home_lease()


def test_lifecycle_start_of_a_landed_record_reuses_it_at_its_address(
    tmp_path: Path, monkeypatch
) -> None:
    """D11 against a transfer landing: the landing writes its record
    straight into the identity registry, so the directory projection does
    not hold it yet.  The target's raw lifecycle.start of that name must
    neither refuse it as taken (AGENT_EXISTS) nor mint a second record: it
    starts the landed incarnation at the landed record's own ``a.`` URI."""

    from hyprial.identity.impl.agents import addressing
    from tests.identity.agents.registry.entity.support import (
        NODE as ENTITY_NODE,
        OWNER as ENTITY_OWNER,
        application,
        start_headless,
    )

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    app = application(tmp_path)
    try:
        landed = app._agent_registry.create("landed", preferred_harness="pi")
        assert app.agents.get("landed") is None
        expected_uri = canonical_agent_uri(
            ENTITY_OWNER, ENTITY_NODE, landed.agent_id
        )
        assert landed.uri == expected_uri
        planned = []
        submit = app._lifecycle_manager.submit

        def record(operation):
            planned.append(operation.target)
            return submit(operation)

        monkeypatch.setattr(app._lifecycle_manager, "submit", record)
        started = start_headless(app, "pi", "landed")

        agent = app.agents.require("landed")
        assert (agent.entity_token, agent.uri) == (landed.entity_token, expected_uri)
        assert [item.uri for item in app._agent_registry.list()] == [expected_uri]
        (target,) = planned
        assert (target.actor, target.route.route_id) == (expected_uri, expected_uri)
        assert started["actor"] == expected_uri
    finally:
        app._close()
        app._close_home_lease()


@pytest.mark.parametrize("minting", [False, True], ids=["switch-off", "switch-on"])
def test_lifecycle_start_without_a_record_creates_a_managed_launch_record(
    tmp_path: Path, monkeypatch, minting: bool
) -> None:
    """D11 creates a raw start's record before the lifecycle step; the record
    keeps the step's published facts, so ``ps`` shows the same
    ``creationOrigin`` whether or not the switch creates it early."""

    from hyprial.identity.impl.agents import addressing
    from tests.identity.agents.registry.entity.support import (
        application,
        start_headless,
    )

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", minting)
    app = application(tmp_path)
    try:
        start_headless(app, "codex", "fresh")

        agent = app.agents.require("fresh")
        assert agent.capabilities.get("creationOrigin") == "managed-launch"
        assert agent.preferred_harness == "codex"
        rows = [row for row in app.handle("ps", {})["agents"] if row["actor"] == "fresh"]
        assert [row["capabilities"]["creationOrigin"] for row in rows] == ["managed-launch"]
    finally:
        app._close()
        app._close_home_lease()
