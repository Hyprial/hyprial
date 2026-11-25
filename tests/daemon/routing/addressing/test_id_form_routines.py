"""UUID addressing lane A: routine bindings compare through the registry.

A routine-owned coordinator's ``produces`` is minted at ``routine.add``,
before the coordinator agent exists, so it is the name URI; the storage
migration may later rewrite it to the coordinator's ``a.`` id URI.  Either
spelling must resolve to the same local agent wherever a binding is
compared with an agent: the coordinator launch identity check, the
existing-coordinator restore answer, retirement, ``_routines_bound_to``
(agent destroy) and ``agentsWithoutRoutine``.  Answers carry the official
address.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel import HarnessLaunchSpec, canonical_agent_uri
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _Inbox,
    _Presence,
    _close_application,
    _isolated_application,
)
from tests.daemon.routing.addressing.test_id_form_presence import (
    NODE,
    OWNER,
    id_form as id_form,
)
from tests.daemon.cells.observability.mount.test_mount import composed as composed

COORDINATOR = "routine-selfdrive"
NAME_URI = f"agent:{OWNER}:{NODE}:{COORDINATOR}"


@pytest.fixture
def coordinator(tmp_path: Path, request, id_form):
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    agent = app.agents.create(COORDINATOR)
    id_uri = canonical_agent_uri(OWNER, NODE, agent.agent_id)
    assert agent.uri == id_uri
    return app, id_uri


def _produces(spelling: str, id_uri: str) -> str:
    return id_uri if spelling == "id" else NAME_URI


def _routine(produces: str) -> dict[str, object]:
    return {
        "name": "selfdrive",
        "produces": produces,
        "actorOwnership": "routine",
    }


def _owned_spec(app) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        "codex",
        COORDINATOR,
        True,
        nickname=app._routine_coordinator_marker("selfdrive", None),
    )


@pytest.mark.parametrize("spelling", ["name", "id"])
def test_coordinator_launches_by_name_and_answers_the_official_address(
    coordinator, spelling, monkeypatch
) -> None:
    app, id_uri = coordinator
    monkeypatch.setattr(
        "hyprial.daemon.impl.dispatch.matrix.resolve",
        lambda _tier: SimpleNamespace(harness="codex", provider=None, model="m"),
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def handle(method, params):
        calls.append((method, params))
        return {"actor": id_uri}

    app.handle = handle

    result = app._ensure_routine_coordinator_from_specs(
        _routine(_produces(spelling, id_uri)),
        recovering=False, specs=(), adapters=(),
    )

    assert [(method, params["name"]) for method, params in calls] == [
        ("lifecycle.start", COORDINATOR)
    ]
    assert result["actor"] == id_uri


@pytest.mark.parametrize("spelling", ["name", "id"])
def test_coordinator_launch_answering_another_agent_is_refused(
    coordinator, spelling, monkeypatch
) -> None:
    from hyprial.daemon.impl.application import DaemonRequestError

    app, id_uri = coordinator
    other = app.agents.create("other").uri
    monkeypatch.setattr(
        "hyprial.daemon.impl.dispatch.matrix.resolve",
        lambda _tier: SimpleNamespace(harness="codex", provider=None, model="m"),
    )
    calls: list[str] = []
    app.handle = lambda method, params: calls.append(method) or {"actor": other}

    with pytest.raises(DaemonRequestError) as failure:
        app._ensure_routine_coordinator_from_specs(
            _routine(_produces(spelling, id_uri)),
            recovering=False, specs=(), adapters=(),
        )

    assert failure.value.code == "ROUTINE_COORDINATOR_IDENTITY_MISMATCH"
    assert calls == ["lifecycle.start", "down"]


@pytest.mark.parametrize("spelling", ["name", "id"])
def test_existing_coordinator_restore_answers_the_official_address(
    coordinator, spelling
) -> None:
    app, id_uri = coordinator

    result = app._ensure_routine_coordinator_from_specs(
        _routine(_produces(spelling, id_uri)),
        recovering=True, specs=(_owned_spec(app),), adapters=(),
    )

    assert result == {"actor": id_uri, "restored": True, "changed": False}


@pytest.mark.parametrize("spelling", ["name", "id"])
def test_retire_stops_the_coordinator_by_name(
    coordinator, spelling, monkeypatch
) -> None:
    app, id_uri = coordinator
    spec = _owned_spec(app)
    monkeypatch.setattr(
        app.desired_state, "load", lambda: SimpleNamespace(harnesses=(spec,))
    )
    calls: list[tuple[str, dict[str, object]]] = []
    app.handle = lambda method, params: calls.append((method, params)) or {
        "removed": True
    }

    result = app._retire_routine_coordinator(_routine(_produces(spelling, id_uri)))

    assert calls == [("down", {"target": COORDINATOR, "provider": "codex"})]
    assert result == {"actor": id_uri, "retired": True, "changed": True}


@pytest.mark.parametrize("spelling", ["name", "id"])
def test_routines_bound_to_the_coordinator_resolve_either_spelling(
    coordinator, spelling
) -> None:
    app, id_uri = coordinator
    routine = _routine(_produces(spelling, id_uri))
    other = app.agents.create("other").uri
    app._routine_service = SimpleNamespace(  # type: ignore[assignment]
        list=lambda: {"routines": [routine]}, close=lambda: None
    )

    assert app._routines_bound_to(id_uri) == [routine]
    assert app._routines_bound_to(NAME_URI) == [routine]
    assert app._routines_bound_to(other) == []


def _routable(app) -> None:
    app._harnesses = SimpleNamespace(status=lambda: ())  # type: ignore[assignment]
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]


@pytest.mark.parametrize("spelling", ["name", "id"])
def test_agent_list_counts_the_coordinator_as_bound(coordinator, spelling) -> None:
    app, id_uri = coordinator
    other = app.agents.create("other").uri
    _routable(app)
    app._routine_service = SimpleNamespace(  # type: ignore[assignment]
        list=lambda: {"routines": [_routine(_produces(spelling, id_uri))]},
        close=lambda: None,
    )

    listed = app.handle("agent.list", {})

    assert listed["agentsWithoutRoutine"] == [other]


def _routine_ipc(app, stored: dict[str, object], monkeypatch) -> None:
    monkeypatch.setattr(
        app,
        "_routine_text_loader",
        lambda _text: SimpleNamespace(name="selfdrive", actor=None, produces=NAME_URI),
    )
    monkeypatch.setattr(app, "_routine_admit", lambda _spec: None)
    app._routine_service = SimpleNamespace(  # type: ignore[assignment]
        list=lambda: {"routines": [stored]},
        status=lambda name: stored,
        set=lambda **_kwargs: {"ok": True},
        close=lambda: None,
    )


def test_routine_set_keeps_a_binding_migrated_to_the_id_uri(
    coordinator, monkeypatch
) -> None:
    app, id_uri = coordinator
    _routine_ipc(app, _routine(id_uri), monkeypatch)

    assert app._ipc_routine_set({"name": "selfdrive", "yaml": "x"}) == {"ok": True}


def test_legacy_routine_add_sees_a_coordinator_owned_under_its_id_uri(
    coordinator, monkeypatch
) -> None:
    from hyprial.daemon.impl.application import DaemonRequestError

    app, id_uri = coordinator
    _routine_ipc(app, _routine(id_uri), monkeypatch)
    app._routine_coordinator = None
    app._transport = None

    with pytest.raises(DaemonRequestError) as failure:
        app._ipc_routine_add({"yaml": "x", "actor": "user:owner"})

    assert failure.value.code == "ROUTINE_COORDINATOR_CONFLICT"


def _real_routine_service(app, state_dir: Path, produces: str) -> None:
    from hyprial.biz import RoutineService, render_template
    from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter

    service = RoutineService(
        pac=SimpleNamespace(rearm_boundary=lambda **_kwargs: 0), alarm=object(),
        state_dir=state_dir, source_query=lambda _filter: [],
        binding_key=app._routine_binding_key,
    )
    _routable(app)
    app._routine_service = RoutineServiceAdapter(service)
    service.add(
        yaml_text=render_template(
            "selfdrive", owner=produces, escalate_to=f"user:{OWNER}"
        ),
        owner=f"user:{OWNER}", enabled=False,
    )


@pytest.mark.parametrize(("stored", "requested"), [("name", "id"), ("id", "name")])
def test_routine_set_through_the_real_service_resolves_either_spelling(
    coordinator, tmp_path: Path, stored: str, requested: str
) -> None:
    from hyprial.biz import render_template

    app, id_uri = coordinator
    _real_routine_service(app, tmp_path / "routines", _produces(stored, id_uri))
    yaml_text = render_template(
        "selfdrive", owner=_produces(requested, id_uri), escalate_to=f"user:{OWNER}"
    )

    result = app.handle(
        "routine.set",
        {"name": "selfdrive", "yaml": yaml_text, "actor": f"user:{OWNER}"},
    )

    assert result["name"] == "selfdrive"


@pytest.mark.parametrize(("stored", "other"), [("name", "id"), ("id", "name")])
def test_last_binding_counts_either_spelling_as_the_same_agent(
    coordinator, tmp_path: Path, stored: str, other: str
) -> None:
    """D5 on removal: a second routine bound to the other spelling of the
    same agent is an alternative, so the first one may be removed."""

    from hyprial.biz import render_template

    app, id_uri = coordinator
    _real_routine_service(app, tmp_path / "routines", _produces(stored, id_uri))
    service = app._routine_service.service
    service.add(
        yaml_text=render_template(
            "agent-home-setup", owner=_produces(other, id_uri),
            escalate_to=f"user:{OWNER}", name="replacement",
        ),
        owner=f"user:{OWNER}", enabled=True,
    )
    service.resume(name="selfdrive")

    assert service.remove(name="selfdrive", enforce_last=True)["removed"] is True
    assert [item["name"] for item in service.list()["routines"]] == ["replacement"]


def _borrowing(app, actor: str) -> dict[str, object]:
    # A complete routine.list row: the result schema is closed and validated
    # by METHOD_REGISTRY, so the stub carries every required field.
    routine = {
        "name": "selfdrive", "owner": "user:someone-else",
        "enabled": True, "nextDueMs": 0, "sourceErrorStreak": 0,
        "outcomes": [], "registrationId": None, "mode": "scheduled",
        "role": "dispatch", "actor": actor, "actorOwnership": "borrowed",
        "launch": None, "overlap": None, "missedPeriods": None,
        "inFlight": [], "lastDispatchAtMs": None, "lastOutcome": None,
    }
    _routable(app)
    app._routine_service = SimpleNamespace(  # type: ignore[assignment]
        list=lambda: {"routines": [routine]},
        status=lambda name: routine,
        pause=lambda name: {"name": name, "enabled": False},
        close=lambda: None,
    )
    return routine


@pytest.mark.parametrize(("stored", "caller"), [("name", "id"), ("id", "name")])
@pytest.mark.parametrize("method", ["routine.pause", "routine.list"])
def test_routine_caller_matches_its_borrowed_actor_in_either_spelling(
    coordinator, monkeypatch, stored: str, caller: str, method: str
) -> None:
    app, id_uri = coordinator
    _borrowing(app, _produces(stored, id_uri))
    monkeypatch.setattr(app, "_workflow_caller", lambda _params: _produces(caller, id_uri))

    result = app.handle(method, {"name": "selfdrive"})

    if method == "routine.list":
        assert [item["name"] for item in result["routines"]] == ["selfdrive"]
    else:
        assert result == {"name": "selfdrive", "enabled": False}


@pytest.mark.parametrize("method", ["routine.pause", "routine.list"])
def test_routine_caller_naming_another_agent_stays_refused(
    coordinator, monkeypatch, method: str
) -> None:
    from hyprial.daemon.impl.application import DaemonRequestError

    app, id_uri = coordinator
    other = app.agents.create("other").uri
    _borrowing(app, id_uri)
    monkeypatch.setattr(app, "_workflow_caller", lambda _params: other)

    if method == "routine.list":
        assert app.handle(method, {})["routines"] == []
    else:
        with pytest.raises(DaemonRequestError) as refused:
            app.handle(method, {"name": "selfdrive"})
        assert refused.value.code == "CALLER_NOT_AUTHORIZED"


def test_retiring_a_gone_id_uri_coordinator_is_an_idempotent_no_op(
    coordinator, monkeypatch
) -> None:
    app, id_uri = coordinator
    app.agents.destroy(COORDINATOR)
    monkeypatch.setattr(
        app.desired_state, "load", lambda: SimpleNamespace(harnesses=())
    )
    calls: list[str] = []
    app.handle = lambda method, params: calls.append(method) or {}

    result = app._retire_routine_coordinator(_routine(id_uri))

    assert result == {"actor": id_uri, "retired": False, "changed": False}
    assert calls == []


def test_started_runtime_wires_the_routine_binding_key(
    tmp_path: Path, monkeypatch, id_form
) -> None:
    from tests.support.runtime.cell_delivery.composition_probe import _probe

    app, _captured, _links = _probe(monkeypatch, tmp_path)
    wired: list[object] = []

    def factory(deps):
        wired.append(deps)
        raise RuntimeError("captured")

    app._routine_runtime_factory = factory
    try:
        agent = app.agents.create("worker")
        app._start_runtime()
        assert len(wired) == 1
        assert callable(wired[0].resolve_routine_target)
        key = wired[0].binding_key
        assert key(agent.uri) == key(f"agent:{app.owner}:{app.node_id}:worker")
        assert key(agent.uri) != key(f"agent:{app.owner}:{app.node_id}:other")
    finally:
        app._close()
        app.agents.close()


@pytest.mark.parametrize("minting", [False, True], ids=["switch-off", "switch-on"])
def test_wired_binding_key_equates_only_an_id_uri_with_its_frozen_name_uri(
    tmp_path: Path, monkeypatch, minting: bool
) -> None:
    from hyprial.biz.impl.routine.execution.registry.core import same_binding
    from hyprial.identity.impl.agents import addressing
    from tests.support.runtime.cell_delivery.composition_probe import _probe

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", minting)
    app, _captured, _links = _probe(monkeypatch, tmp_path)
    wired: list[object] = []

    def factory(deps):
        wired.append(deps)
        raise RuntimeError("captured")

    app._routine_runtime_factory = factory
    try:
        hosted = app.agents.create_transfer_hosted("tx", pinned_owner="alice")
        worker = app.agents.create("worker")
        app._start_runtime()
        key = wired[0].binding_key
        local_name_uri = f"agent:{app.owner}:{app.node_id}:tx"
        assert hosted.uri != local_name_uri
        assert not same_binding(key, hosted.uri, local_name_uri)
        assert not same_binding(key, local_name_uri, hosted.uri)
        assert not same_binding(key, "tx", hosted.uri)
        worker_name_uri = f"agent:{app.owner}:{app.node_id}:worker"
        assert same_binding(key, worker.uri, worker_name_uri) is True
        assert same_binding(key, "worker", worker_name_uri) is False
    finally:
        app._close()
        app.agents.close()


@pytest.fixture
def publishing_routine(composed):
    """Real registry, lifecycle actor, routine writer and PAC; synthetic process only."""
    from dataclasses import replace
    import yaml
    from hyprial.daemon import RoutineRuntimeDeps
    from hyprial.daemon.impl.application.actors.stored_agent_uris import resolve_routine_target
    from hyprial.daemon.impl.pac.graphs.reactor import NullSender
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation
    from hyprial.shell.impl.daemon_wiring.routines import routine_runtime_factory
    from tests.daemon.cells.observability.mount.test_mount import registered, settle

    app, launcher, harness, _transport, _delivery, launch = composed
    clock = [1_000_000]
    workflow = GraphWorkflowService(state_dir=app.state_dir, owner=app.owner, machine=app.node_id,
                                    sender=NullSender(), admit=app._workflow_admit, start_thread=False)
    app._workflow_service = workflow
    app._restore_done.set()
    runtime = routine_runtime_factory(RoutineRuntimeDeps(
        state_dir=app.state_dir, hyprial_home=app.hyprial_home, owner=app.owner,
        logger=None, alarm=SimpleNamespace(escalate=lambda **_kwargs: None),
        deliver_task=lambda **_kwargs: True, clock_ms=lambda: clock[0],
        resolve_principal=app._resolve_routine_principal,
        resolve_routine_target=lambda name, target: resolve_routine_target(app, name, target),
        migrate_address=app._migrate_stored_routine_address, workflow_service=workflow,
        binding_key=app._routine_binding_key,
    ))
    app._routine_service = runtime.service
    service = runtime.service.service
    logical = f"agent:{app.owner}:{app.node_id}:worker"
    raw = yaml.safe_dump({
        "version": 2, "mode": "scheduled", "name": "coordinate",
        "schedule": {"interval": "1m"}, "task": "one scheduled task",
        "produces": logical, "role": "dispatch",
        "on_task_timeout": {"action": "escalate", "escalate_to": f"user:{app.owner}"},
    })
    service.add(yaml_text=raw, owner=f"user:{app.owner}", enabled=False)
    row = service.status(name="coordinate")
    from hyprial.kernel import DaemonRequestError
    with pytest.raises(DaemonRequestError) as pending_start:
        resolve_routine_target(app, "coordinate", logical)
    assert pending_start.value.code == "ROUTINE_COORDINATOR_IDENTITY_MISMATCH"
    launch = registered(app, launch)
    launch = replace(launch, harness=replace(launch.harness,
        nickname=app._routine_coordinator_marker("coordinate", row["registrationId"])))
    settle(app, LifecycleOperation.create("create", launch))
    agent = app.agents.current(logical)
    binding = app._agent_liveness.binding(agent.uri)
    assert harness.projection.read_observation_source("pi:worker", agent.entity_token, binding.revision)
    yield app, service, workflow, logical, agent, launch, clock, raw, launcher


@pytest.mark.parametrize("spelling", ["name", "id"])
def test_owned_routine_publishes_official_owner_and_keeps_stored_spec(publishing_routine, spelling):
    import yaml
    from hyprial.biz import RoutineStore
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore

    app, service, workflow, logical, agent, _launch, _clock, raw, launcher = publishing_routine
    target = logical
    if spelling == "id":
        document = yaml.safe_load(raw)
        document["produces"] = target = agent.uri
        raw = yaml.safe_dump(document)
        service.set(name="coordinate", yaml_text=raw)
    result = service._pac.start_idempotent(
        routine_name="coordinate", task_uuid="scheduled:1000000", task_text="work",
        target=target, escalate_to=f"user:{app.owner}", timeout_seconds=60,
        sender=f"user:{app.owner}",
    )
    store = PacGraphStore(workflow.database)
    try:
        assert store.node(result["graphId"], "work").owner == agent.uri
    finally:
        store.close()
    persisted = RoutineStore(app.state_dir / "routines.sqlite3")
    try:
        assert persisted.get_routine("coordinate").yaml_text == raw
    finally:
        persisted.close()
    assert launcher.starts == 1


@pytest.mark.parametrize("fault", ["missing-marker", "foreign-marker", "missing-observation", "new-binding", "new-incarnation", "new-incarnation-id", "rotate-during-read", "borrowed", "foreign-target"])
def test_owned_publication_requires_current_registration_and_process(publishing_routine, monkeypatch, fault):
    from dataclasses import replace
    import yaml
    from hyprial.kernel import DaemonRequestError

    app, service, _workflow, logical, agent, launch, _clock, raw, _launcher = publishing_routine
    target = logical
    expected = "ROUTINE_COORDINATOR_IDENTITY_MISMATCH"
    if fault in ("missing-marker", "foreign-marker"):
        spec = app.desired_state.harness_spec("worker")
        app.desired_state.upsert_harness(replace(spec, nickname=None if fault == "missing-marker" else "routine:other"))
    elif fault == "missing-observation":
        app._harnesses.actor.projection.publish({}, 100)
    elif fault == "new-binding":
        app._agent_liveness.release(agent.uri)
        app._agent_liveness.bind(agent.uri, harness=launch.harness.harness, runtime="headless")
    elif fault in ("new-incarnation", "new-incarnation-id"):
        if fault == "new-incarnation-id":
            document = yaml.safe_load(raw)
            document["produces"] = target = agent.uri
            service.set(name="coordinate", yaml_text=yaml.safe_dump(document))
        app._agent_session_domains._registry.rotate_secret_authority("worker")
        assert app.agents.current(logical).entity_token != agent.entity_token
    elif fault == "rotate-during-read":
        current = app.agents.current
        reads = []
        def racing_read(value):
            reads.append(value)
            if len(reads) == 2:
                app._agent_session_domains._registry.rotate_secret_authority("worker")
            return current(value)
        monkeypatch.setattr(app.agents, "current", racing_read)
    elif fault == "borrowed":
        service.remove(name="coordinate")
        document = yaml.safe_load(raw)
        document["actor"] = document.pop("produces")
        service.add(yaml_text=yaml.safe_dump(document), owner=f"user:{app.owner}", enabled=False)
        expected = "AGENT_ADDRESS_RETIRED"
    else:
        target = f"agent:foreign:{app.node_id}:worker"
        expected = "WORKFLOW_REMOTE_UNAVAILABLE"
    with pytest.raises(DaemonRequestError) as refusal:
        service._pac.start_idempotent(routine_name="coordinate", task_uuid="negative", task_text="work",
            target=target, escalate_to=f"user:{app.owner}", timeout_seconds=60, sender=f"user:{app.owner}")
    assert refusal.value.code == expected


def test_routine_dispatch_without_owned_target_port_keeps_strict_resolver(publishing_routine):
    from hyprial.biz import PacRoutineDispatch
    from hyprial.kernel import DaemonRequestError

    app, _service, workflow, logical, _agent, _launch, clock, _raw, _launcher = publishing_routine
    dispatch = PacRoutineDispatch(state_dir=app.state_dir, deliver=lambda **_kwargs: True,
        clock_ms=lambda: clock[0], resolve_principal=app._resolve_routine_principal, workflow=workflow)
    with pytest.raises(DaemonRequestError) as refused:
        dispatch.start_idempotent(routine_name="coordinate", task_uuid="strict-default", task_text="work",
            target=logical, escalate_to=f"user:{app.owner}", timeout_seconds=60, sender=f"user:{app.owner}")
    assert refused.value.code == "AGENT_ADDRESS_RETIRED"


def test_owned_target_does_not_reenable_name_addresses_for_send_or_escalation(publishing_routine):
    from hyprial.kernel import DaemonRequestError

    app, service, _workflow, logical, _agent, _launch, _clock, _raw, _launcher = publishing_routine
    with pytest.raises(DaemonRequestError) as send:
        app._resolve_send_sender(logical)
    assert send.value.code == "AGENT_ADDRESS_RETIRED"
    with pytest.raises(DaemonRequestError) as escalation:
        service._pac.start_idempotent(routine_name="coordinate", task_uuid="strict-escalation", task_text="work",
            target=logical, escalate_to=logical, timeout_seconds=60, sender=f"user:{app.owner}")
    assert escalation.value.code == "AGENT_ADDRESS_RETIRED"


def test_scheduled_writer_dispatch_does_not_wait_on_its_own_mailbox(publishing_routine):
    import time
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore

    app, service, workflow, _logical, agent, _launch, clock, _raw, _launcher = publishing_routine
    service.resume(name="coordinate", align_schedule=True)
    clock[0] = service.status(name="coordinate")["nextDueMs"]
    service.submit_timer(clock[0])
    deadline = time.monotonic() + 5
    while not service.status(name="coordinate")["inFlight"] and time.monotonic() < deadline:
        time.sleep(0.01)
    active = service.status(name="coordinate")["inFlight"]
    assert len(active) == 1
    store = PacGraphStore(workflow.database)
    try:
        assert store.node(active[0]["runId"], "work").owner == agent.uri
    finally:
        store.close()
