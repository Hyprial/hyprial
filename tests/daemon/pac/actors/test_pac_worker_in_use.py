"""A workflow worker that a person is using is never reclaimed automatically.

R&D default (2026-10-18, pending Allen's ruling): an agent pinned to an
adapter, or online with a registered interactive session, is left to a
human.  Terminal cleanup parks it as ``worker_in_use`` attention without a
stop, and PAC GC skips it as ``worker-in-use``.  The cleanup path drives the
real graph authority and SQLite store; GC uses the real selector and agent
registry.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation
from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime
from hyprial.daemon.impl.pac.actors.gc import PacGc
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from tests.daemon.pac.actors.test_pac_worker_reclaim import (
    USER,
    _managed,
    _read,
    _started,
    _state_is,
    _wait_for,
)
from tests.daemon.pac.lifecycle.test_pac_gc import (
    Application,
    _close,
    _register_owned_worker,
    _service,
    _start_owned,
)


@pytest.mark.parametrize("use", ["pinned", "interactive_online"])
def test_cleanup_parks_an_in_use_worker_until_it_is_released(
    tmp_path: Path, use: str
) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _started(authority, runtime, graph_id, actor)
        running = runtime.running[actor.actor_name]
        runtime.running[actor.actor_name] = RuntimeObservation(
            True, running.identity_marker, running.harness, None,
            running.agent_entity_token, **{use: True},
        )
        service.cancel(run_id=graph_id, actor=USER)
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(
            authority.database, graph_id, "attention", "worker_in_use",
        ))
        assert runtime.stops == []
        intent = _read(
            authority.database,
            lambda s: s.workflow_worker_cleanup_intents(graph_id),
        )[0]
        assert use in intent["last_observation_json"]

        runtime.running[actor.actor_name] = running
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "complete"))
        assert actor.actor_name not in runtime.running
    finally:
        assert service.close()
        assert authority.close(3.0)


class _Runtime:
    def __init__(self, observation: RuntimeObservation) -> None:
        self.observation = observation

    def observe(self, _actor_name: str) -> RuntimeObservation:
        return self.observation


def test_gc_skips_an_offline_worker_with_an_online_interactive_session(
    tmp_path: Path,
) -> None:
    application = Application(tmp_path)
    service = _service(tmp_path)
    try:
        graph_id = _start_owned(service, "in-use-gc")
        store = PacGraphStore(service.database)
        try:
            _close(store, graph_id)
            actor_name, _home = _register_owned_worker(application, store, graph_id)
        finally:
            store.close()
        gc = PacGc(
            state_dir=tmp_path / "state",
            application=application,
            runtime=_Runtime(RuntimeObservation(False, interactive_online=True)),
            logger=lambda *args, **kwargs: None,
            start_thread=False,
        )
        try:
            preview = gc.preview()
            report = gc.run_pass()
        finally:
            gc.close()
        assert preview["selectedCount"] == 0
        assert preview["skipped"].get("worker-in-use") == 1
        assert report["removed"] == 0
        assert application.agents.get(actor_name) is not None
    finally:
        service.close()
        application.agents.close()


class _Spec:
    nickname = "pac:wf-a:_actor.work:1:effect"
    harness = "pi"


def _observe_application(*, pinned: tuple[str, ...], sessions, online: bool):
    agent = SimpleNamespace(entity_token="entity", pinned_adapters=pinned)
    return SimpleNamespace(
        desired_state=SimpleNamespace(
            harness_spec=lambda name, excluding=None: _Spec()
        ),
        agents=SimpleNamespace(get=lambda name: agent),
        _canonical_harness_uri=lambda name, spec: f"agent:o:n:{name}",
        _managed_worker_running=lambda actor: True,
        _registered_interactive_actors=lambda: frozenset(sessions),
        _presence=SimpleNamespace(actor_online=lambda actor: online),
    )


def test_runtime_observe_reports_pins_and_online_interactive_sessions() -> None:
    uri = "agent:o:n:wf-a"
    idle = DaemonActorRuntime(
        _observe_application(pinned=(), sessions=(), online=True)
    ).observe("wf-a")
    assert (idle.pinned, idle.interactive_online) == (False, False)

    pinned = DaemonActorRuntime(
        _observe_application(pinned=("lark-main",), sessions=(), online=True)
    ).observe("wf-a")
    assert (pinned.pinned, pinned.interactive_online) == (True, False)

    attached = DaemonActorRuntime(
        _observe_application(pinned=(), sessions=(uri,), online=True)
    ).observe("wf-a")
    assert attached.interactive_online is True

    stale = DaemonActorRuntime(
        _observe_application(pinned=(), sessions=(uri,), online=False)
    ).observe("wf-a")
    assert stale.interactive_online is False


def test_a_pinned_worker_that_is_not_running_keeps_its_record(tmp_path: Path) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _started(authority, runtime, graph_id, actor)
        running = runtime.running[actor.actor_name]
        runtime.running[actor.actor_name] = RuntimeObservation(
            False, running.identity_marker, running.harness, None,
            running.agent_entity_token, retirement_pending=True, pinned=True,
        )
        service.cancel(run_id=graph_id, actor=USER)
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(
            authority.database, graph_id, "attention", "worker_in_use",
        ))
        assert runtime.stops == []
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_an_in_use_hold_never_counts_an_attempt(tmp_path: Path) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _started(authority, runtime, graph_id, actor)
        running = runtime.running[actor.actor_name]
        runtime.running[actor.actor_name] = RuntimeObservation(
            True, running.identity_marker, running.harness, None,
            running.agent_entity_token, pinned=True,
        )
        service.cancel(run_id=graph_id, actor=USER)
        for _ in range(3):
            assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
            _wait_for(_state_is(
                authority.database, graph_id, "attention", "worker_in_use",
            ))
        intent = _read(
            authority.database,
            lambda s: s.workflow_worker_cleanup_intents(graph_id),
        )[0]
        assert intent["attempts"] == 0
    finally:
        assert service.close()
        assert authority.close(3.0)
