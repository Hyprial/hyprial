"""composition.cells.cell_runtime(app): lazy, single, closable composition.

The I5-1 additions pin the cell actor attachment: a composed runtime with an
actor host registry and an agent directory gets a ``CellActorRegistry`` with
one keyed actor per local agent cell; an app without host registry (a test
double) skips it, logged, and still composes; closing detaches the registry
before the runtime stops, so a late ensure is refused instead of telling a
dying runtime's hooks.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.cells.actor.contracts import (
    CellActorWakeup,
)
from hyprial.daemon.impl.cells.model import CellBinding
from hyprial.daemon.impl.cells.ports import CellIndex, CellRuntimeHealth, CellRuntimePort, CellSpec
from hyprial.daemon.impl.composition.cells import (
    CELL_RUNTIME_CLOSE_TIMEOUT_S,
    cell_runtime,
    close_cell_runtime,
)
from hyprial.daemon.impl.composition.cells.wiring import attach_cell_actors
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.identity import org_space_name
from hyprial.daemon.impl.composition.actors import ActorHostRegistry
from hyprial.kernel import ActorState, AdmissionResult
from hyprial.identity.impl.agents import Agent
from tests.daemon.cells.actor.actor_support import agent_cell
from hyprial.daemon.impl.cells.runtime import create_cell_cache
from hyprial.daemon.impl.composition.cells.wiring import local_cell_authority
from tests.daemon.cells.runtime.cell_runtime_support import ALICE, uid_for

CELL_GROUP = "cell"


def _app(tmp_path: Path) -> Any:
    fs = LocalOrgFs(author="user:alice")
    fs.create_space(org_space_name("group-acme"))
    logged: list[str] = []
    runtime = SimpleNamespace(facade=fs)
    orgfs_calls: list[int] = []

    def require() -> Any:
        orgfs_calls.append(1)
        return runtime

    app = SimpleNamespace(
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        owner="alice",
        user_id=uid_for("user:alice"),
        user_issuer="",
        _require_orgfs_runtime=require,
        _log=lambda _level, _component, event, **_fields: logged.append(event),
        logged=logged,
        orgfs_calls=orgfs_calls,
        fs=fs,
    )
    return app


class _Agents:
    """The agent directory port the wiring reads: ``list`` only."""

    def __init__(self, *agents: Agent) -> None:
        self.items = list(agents)

    def list(self) -> tuple[Agent, ...]:
        return tuple(self.items)


def _agent(actor: str) -> Agent:
    return Agent(
        uri=f"agent:alice:mac:{actor}",
        actor=actor,
        owner="alice",
        machine="mac",
        entity_token=uuid.uuid4().hex,
    )


def _app_with_actors(tmp_path: Path, agents: _Agents) -> Any:
    app = _app(tmp_path)
    app.agents = agents
    app._actor_hosts = ActorHostRegistry()
    return app


def test_cell_runtime_composes_once_and_reuses_the_instance(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        first = cell_runtime(app)
        port: CellRuntimePort = first
        assert port.index().generation >= 1
        assert cell_runtime(app) is first
        assert app._cell_runtime is first
        assert first.identity.writer == "user:alice"
        assert first.identity.authority == ALICE
        assert (tmp_path / "state" / "cells.sqlite3").is_file()
        assert "cell.runtime_composed" in app.logged
        assert any(thread.name == "cell-runtime" for thread in threading.enumerate())
    finally:
        close_cell_runtime(app)
    assert app._cell_runtime is None
    assert not any(thread.name == "cell-runtime" for thread in threading.enumerate())
    close_cell_runtime(app)


def test_concurrent_first_calls_compose_one_runtime(tmp_path: Path) -> None:
    app = _app(tmp_path)
    results: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def call() -> None:
        try:
            barrier.wait(5.0)
            results.append(cell_runtime(app))
        except BaseException as error:
            errors.append(error)
            barrier.abort()

    threads = [threading.Thread(target=call) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 4 and len({id(item) for item in results}) == 1
    finally:
        barrier.abort()
        for thread in threads:
            thread.join(5.0)
        assert all(not thread.is_alive() for thread in threads)
        close_cell_runtime(app)


def test_a_composed_runtime_persists_to_the_app_orgfs(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        cells = cell_runtime(app)
        cell = cells.ensure(CellSpec(kind="agent", binding=CellBinding.keyed("eng")))
        cells.run_once()
        space = next(info.space_id for info in app.fs.spaces())
        content, _ = app.fs.read_text(space, f"cells/agent/{cell.cell_id}.json")
        assert cell.cell_id in content
    finally:
        close_cell_runtime(app)


def test_close_reports_a_runtime_that_does_not_stop(tmp_path: Path) -> None:
    class Stuck:
        def close(self, timeout: float) -> bool:
            assert timeout == CELL_RUNTIME_CLOSE_TIMEOUT_S
            return False

    app = SimpleNamespace(_cell_runtime=Stuck())
    try:
        close_cell_runtime(app)
    except RuntimeError as error:
        assert "cell runtime" in str(error)
    else:
        raise AssertionError("a stuck runtime was reported closed")
    assert app._cell_runtime is None


def _cell_threads() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == "cell-runtime")


def test_a_closed_app_never_composes_a_runtime_again(tmp_path: Path) -> None:
    app = _app(tmp_path)
    first = cell_runtime(app)
    assert first is not None
    close_cell_runtime(app)
    calls = len(app.orgfs_calls)
    assert cell_runtime(app) is None
    assert len(app.orgfs_calls) == calls
    assert _cell_threads() == 0


def test_closing_before_first_use_blocks_composition(tmp_path: Path) -> None:
    app = _app(tmp_path)
    close_cell_runtime(app)
    assert cell_runtime(app) is None
    assert app.orgfs_calls == []
    assert not (tmp_path / "state" / "cells.sqlite3").exists()


def test_a_composition_failure_returns_none_closes_the_cache_and_backs_off(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from hyprial.daemon.impl.cells.runtime import CellRuntime

    def broken_start(_self: Any) -> None:
        raise RuntimeError("no thread")

    monkeypatch.setattr(CellRuntime, "start", broken_start)
    app = _app(tmp_path)
    assert cell_runtime(app) is None
    assert "cell.runtime_unavailable" in app.logged
    assert cell_runtime(app) is None
    assert len(app.orgfs_calls) == 1
    assert getattr(app, "_cell_runtime", None) is None
    close_cell_runtime(app)


def test_a_composed_runtime_attaches_one_keyed_actor_per_agent_cell(
    tmp_path: Path,
) -> None:
    worker = _agent("worker")
    app = _app_with_actors(tmp_path, _Agents(worker))
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        registry = app._cell_actor_registry
        assert registry is not None
        host = app._actor_hosts.host_for("cell-actors")
        actor = host.find(CELL_GROUP, worker.cell_id)
        assert actor is not None
        actor.ask(object(), timeout=30.0)
        snapshot = registry.snapshot(worker.cell_id)
        assert snapshot is not None, "waiting for initial actor snapshot publication"
        assert snapshot.cell_id == worker.cell_id
        assert snapshot.state.value == "active"
        assert "cell.actor_registry_attached" in app.logged
    finally:
        close_cell_runtime(app)
    assert app._cell_actor_registry is None


def test_an_app_without_an_actor_host_registry_skips_the_actors(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        assert getattr(app, "_cell_actor_registry", None) is None
        assert "cell.actor_registry_skipped" in app.logged
    finally:
        close_cell_runtime(app)


def test_close_detaches_the_actor_registry_before_the_runtime_stops(
    tmp_path: Path,
) -> None:
    worker = _agent("worker")
    app = _app_with_actors(tmp_path, _Agents(worker))
    runtime = cell_runtime(app)
    assert runtime is not None
    registry = app._cell_actor_registry
    assert registry is not None
    close_cell_runtime(app)
    assert registry.ensure_actor(worker.cell_id) is None
    assert registry.notify_health(True) == 0
    runtime.run_once()


def test_a_cell_created_after_composition_gets_its_actor_through_on_index(
    tmp_path: Path,
) -> None:
    late = _agent("late")
    app = _app_with_actors(tmp_path, _Agents())
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        registry = app._cell_actor_registry
        assert registry is not None
        app.agents.items.append(late)
        from hyprial.daemon.impl.cells.runtime.agents import agent_cell_spec

        runtime.ensure(agent_cell_spec(late))
        runtime.run_once()
        host = app._actor_hosts.host_for("cell-actors")
        actor = host.find(CELL_GROUP, late.cell_id)
        assert actor is not None
        actor.ask(object(), timeout=30.0)
        snapshot = registry.snapshot(late.cell_id)
        assert snapshot is not None, "waiting for late actor snapshot publication"
        assert snapshot.state.value == "active"
    finally:
        close_cell_runtime(app)


class _FakeActor:
    def __init__(self) -> None:
        self.commands: list[object] = []

    def tell(self, command: object) -> Any:
        self.commands.append(command)
        return AdmissionResult.ACCEPTED

    def snapshot(self) -> Any:
        return SimpleNamespace(state=ActorState.RUNNING, generation=1)


class _FakeHost:
    def __init__(self) -> None:
        self.actors: dict[str, _FakeActor] = {}

    def find(self, group: str, key: str) -> _FakeActor | None:
        return self.actors.get(key)

    def spawn(self, spec: Any) -> _FakeActor:
        actor = _FakeActor()
        self.actors[spec.key] = actor
        return actor


class _FakeHosts:
    def __init__(self, host: _FakeHost) -> None:
        self._host = host

    def host_for(self, owner: str) -> _FakeHost:
        return self._host


class _HookRuntime:
    author = "user:alice"

    def __init__(self, cell: Any, *, stale: bool = False, fail_health: bool = False) -> None:
        self._index = CellIndex(1, {cell.cell_id: cell})
        self._health = CellRuntimeHealth(stale, "stale-before-attach" if stale else None, 9)
        self._fail_health = fail_health
        self._index_hooks: list[Any] = []
        self._health_hooks: list[Any] = []

    def index(self) -> CellIndex:
        return self._index

    def health(self) -> CellRuntimeHealth:
        return self._health

    def on_index(self, hook: Any) -> Any:
        self._index_hooks.append(hook)

        def unregister() -> None:
            if hook in self._index_hooks:
                self._index_hooks.remove(hook)

        return unregister

    def on_health(self, hook: Any) -> Any:
        if self._fail_health:
            raise RuntimeError("health registration failed")
        self._health_hooks.append(hook)

        def unregister() -> None:
            if hook in self._health_hooks:
                self._health_hooks.remove(hook)

        return unregister


def test_actor_hook_installation_rolls_back_on_health_registration_failure() -> None:
    worker = _agent("rollback")
    runtime = _HookRuntime(worker, fail_health=True)
    host = _FakeHost()
    app = SimpleNamespace(
        _actor_hosts=_FakeHosts(host),
        agents=_Agents(worker),
        _log=lambda *_args, **_kwargs: None,
    )
    assert attach_cell_actors(app, runtime) is None
    assert runtime._index_hooks == []


def test_actor_hook_attachment_replays_stale_health_before_return() -> None:
    worker = _agent("stale")
    runtime = _HookRuntime(agent_cell(worker.agent_id), stale=True)
    host = _FakeHost()
    app = SimpleNamespace(
        _actor_hosts=_FakeHosts(host),
        agents=_Agents(worker),
        _log=lambda *_args, **_kwargs: None,
    )
    registry = attach_cell_actors(app, runtime)
    assert registry is not None
    actor = host.actors[worker.cell_id]
    assert len(actor.commands) >= 1, "waiting for stale-health replay wakeup"
    assert all(isinstance(command, CellActorWakeup) for command in actor.commands)
    registry.detach()


def test_close_waits_for_runtime_publication_and_actor_attachment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import hyprial.daemon.impl.composition.cells.wiring as wiring

    app = _app(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    close_done = threading.Event()
    composed: list[Any] = []

    def blocking_attach(_app: Any, _runtime: Any) -> None:
        entered.set()
        assert release.wait(5.0)
        return None

    monkeypatch.setattr(wiring, "attach_agent_cells", blocking_attach)

    def compose() -> None:
        composed.append(cell_runtime(app))

    def close() -> None:
        close_started.set()
        close_cell_runtime(app)
        close_done.set()

    composer = threading.Thread(target=compose)
    closer = threading.Thread(target=close)
    composer.start()
    assert entered.wait(5.0)
    closer.start()
    assert close_started.wait(5.0)
    assert not close_done.is_set()
    release.set()
    composer.join(5.0)
    closer.join(5.0)
    assert not composer.is_alive()
    assert not closer.is_alive()
    assert composed and composed[0] is not None
    assert app._cell_runtime is None


def test_a_failed_composition_heals_on_its_own_retry_timer(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """I5-2 r2 item 2: the failure path arms the registrar-style retry.

    Nothing periodic reads the cell world and the registrar's hooks exist
    only after a successful composition, so the first failure arms one
    daemon timer that calls ``cell_runtime`` again; a later attempt that
    succeeds both composes and cancels any further retry.
    """
    import time as _time

    from hyprial.daemon.impl.composition.cells import wiring

    attempts = {"count": 0}
    composed = SimpleNamespace(
        index=lambda: SimpleNamespace(cells={}, dirty=(), generation=1),
        dirty=lambda: (),
        kick=lambda: None,
        close=lambda timeout: True,
    )

    def flaky_compose(app: Any) -> Any:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("orgfs down at boot")
        return composed

    monkeypatch.setattr(wiring, "_compose", flaky_compose)
    monkeypatch.setattr(wiring, "attach_agent_cells", lambda app, runtime: None)
    monkeypatch.setattr(wiring, "attach_cell_actors", lambda app, runtime: None)
    monkeypatch.setattr(wiring, "COMPOSE_RETRY_S", 0.2)
    app = _app(tmp_path)
    try:
        assert cell_runtime(app) is None
        assert getattr(app, "_cell_runtime_retry_timer", None) is not None
        deadline = _time.monotonic() + 10
        while getattr(app, "_cell_runtime", None) is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert app._cell_runtime is composed, "the retry timer never healed"
        assert attempts["count"] == 2
        assert getattr(app, "_cell_runtime_retry_timer", None) is None
    finally:
        close_cell_runtime(app)


def test_a_close_during_the_retry_window_never_recomposes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Round 3 item 2: the callback checks closed under the retry lock.

    A close that races the armed retry timer leaves the app closed for the
    callback: no second composition attempt happens after close, and no
    timer is left pending.
    """
    import time as _time

    from hyprial.daemon.impl.composition.cells import wiring

    attempts = {"count": 0}

    def failing_compose(app: Any) -> Any:
        attempts["count"] += 1
        raise RuntimeError("orgfs still down")

    monkeypatch.setattr(wiring, "_compose", failing_compose)
    monkeypatch.setattr(wiring, "attach_agent_cells", lambda app, runtime: None)
    monkeypatch.setattr(wiring, "attach_cell_actors", lambda app, runtime: None)
    monkeypatch.setattr(wiring, "COMPOSE_RETRY_S", 0.1)
    app = _app(tmp_path)
    try:
        assert cell_runtime(app) is None
        assert attempts["count"] == 1
        close_cell_runtime(app)
        _time.sleep(0.4)
        assert attempts["count"] == 1, "a closed app composed again"
        assert getattr(app, "_cell_runtime_retry_timer", None) is None
    finally:
        close_cell_runtime(app)


def test_repeated_failures_never_arm_two_timers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Round 3 item 2: one pending retry timer at most, registrar-style.

    Arming while a timer is pending keeps the incumbent, and a timer-driven
    second failure re-arms exactly one successor.
    """
    import time as _time

    from hyprial.daemon.impl.composition.cells import wiring

    attempts = {"count": 0}

    def failing_compose(app: Any) -> Any:
        attempts["count"] += 1
        raise RuntimeError("orgfs still down")

    monkeypatch.setattr(wiring, "_compose", failing_compose)
    monkeypatch.setattr(wiring, "attach_agent_cells", lambda app, runtime: None)
    monkeypatch.setattr(wiring, "attach_cell_actors", lambda app, runtime: None)
    monkeypatch.setattr(wiring, "COMPOSE_RETRY_S", 0.05)
    app = _app(tmp_path)
    try:
        assert cell_runtime(app) is None
        first = app._cell_runtime_retry_timer
        assert first is not None
        wiring._arm_compose_retry(app, 0.05)
        assert app._cell_runtime_retry_timer is first
        deadline = _time.monotonic() + 5
        while attempts["count"] < 3 and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert attempts["count"] >= 3, "the retry chain stopped early"
        pending = getattr(app, "_cell_runtime_retry_timer", None)
        assert pending is None or pending is not first
        assert not isinstance(pending, list)
    finally:
        close_cell_runtime(app)


def _events(app: Any, name: str) -> list[dict[str, Any]]:
    return [fields for event, fields in app.fields if event == name]


def _logging_app(tmp_path: Path, **overrides: Any) -> Any:
    app = _app(tmp_path)
    app.fields = []
    app._log = lambda _level, _component, event, **fields: app.fields.append((event, fields))
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes() for path in sorted(directory.iterdir()) if path.is_file()
    }


def test_a_missing_uid_refuses_composition_without_touching_orgfs_or_files(
    tmp_path: Path,
) -> None:
    app = _logging_app(tmp_path, user_id=None)
    try:
        assert cell_runtime(app) is None
        (refused,) = _events(app, "cell.runtime_unavailable")
        assert refused["reason"] == "authority-uid-missing"
        assert app.orgfs_calls == []
        assert not (tmp_path / "state").exists()
    finally:
        close_cell_runtime(app)


def test_the_runtime_identity_pairs_the_uid_authority_with_the_orgfs_writer(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        assert runtime.identity.authority == local_cell_authority(app) == ALICE
        assert runtime.identity.writer == "user:alice"
        assert runtime.identity.issuer == ""
    finally:
        close_cell_runtime(app)


def test_a_cache_of_another_identity_refuses_composition_and_keeps_its_bytes(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    create_cell_cache(state / "cells.sqlite3", authority=ALICE, issuer="https://other",
                      writer="user:alice")
    before = _snapshot(state)
    app = _logging_app(tmp_path, user_issuer="https://id.example")
    try:
        assert cell_runtime(app) is None
        (refused,) = _events(app, "cell.runtime_unavailable")
        assert refused["reason"] == "cache-identity-mismatch"
        assert refused["keys"] == "issuer"
        assert _snapshot(state) == before
    finally:
        close_cell_runtime(app)


def test_a_writer_mismatch_refuses_composition_like_an_authority_mismatch(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    create_cell_cache(state / "cells.sqlite3", authority=ALICE, issuer="",
                      writer="user:alice-old")
    before = _snapshot(state)
    app = _logging_app(tmp_path)
    try:
        assert cell_runtime(app) is None
        (refused,) = _events(app, "cell.runtime_unavailable")
        assert (refused["reason"], refused["keys"]) == ("cache-identity-mismatch", "writer")
        assert _snapshot(state) == before
    finally:
        close_cell_runtime(app)


def test_an_unreadable_cache_refuses_composition_and_is_left_alone(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "cells.sqlite3-wal").write_bytes(b"orphan wal")
    before = _snapshot(state)
    app = _logging_app(tmp_path)
    try:
        assert cell_runtime(app) is None
        (refused,) = _events(app, "cell.runtime_unavailable")
        assert refused["reason"] == "cache-unreadable"
        assert _snapshot(state) == before
    finally:
        close_cell_runtime(app)


def test_a_leftover_staging_file_refuses_composition_and_is_kept(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "cells.sqlite3.new").write_bytes(b"leftover staging")
    before = _snapshot(state)
    app = _logging_app(tmp_path)
    try:
        assert cell_runtime(app) is None
        (refused,) = _events(app, "cell.runtime_unavailable")
        assert refused["reason"] == "cache-unreadable"
        assert _snapshot(state) == before
    finally:
        close_cell_runtime(app)


def test_a_refused_composition_retries_read_only_and_composes_once_fixed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import hyprial.daemon.impl.composition.cells.wiring as wiring

    monkeypatch.setattr(wiring, "COMPOSE_RETRY_S", 0.0)
    app = _logging_app(tmp_path, user_id=None)
    try:
        assert cell_runtime(app) is None
        assert cell_runtime(app) is None
        assert [f["reason"] for f in _events(app, "cell.runtime_unavailable")][:2] == [
            "authority-uid-missing",
            "authority-uid-missing",
        ]
        app.user_id = uid_for("user:alice")
        assert cell_runtime(app) is not None
    finally:
        close_cell_runtime(app)


def _daemon(tmp_path: Path, **kwargs: Any) -> Any:
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    state = home / "state"
    return DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="mac",
        hyprial_home=home,
        **kwargs,
        **daemon_dependencies(),
    )


def test_an_explicit_owner_without_user_id_gives_no_uid_and_no_cell_runtime(
    tmp_path: Path,
) -> None:
    app = _daemon(tmp_path, owner="alice")
    try:
        assert app.user_id is None and app.user_issuer == ""
        with pytest.raises(Exception) as raised:
            local_cell_authority(app)
        assert getattr(raised.value, "reason", None) == "authority-uid-missing"
    finally:
        app._close()
    paired = _daemon(tmp_path, owner="alice", user_id=uid_for("user:alice"))
    try:
        assert local_cell_authority(paired) == ALICE
    finally:
        paired._close()


def test_the_uid_is_resolved_once_per_serving_generation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import json

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.delenv("HYPRIAL_USER_ID", raising=False)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    first = uid_for("user:alice")
    settings = {"version": 1, "owner": "alice", "userId": first, "identityMode": "casdoor",
                "identityIssuer": "https://id.example"}
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    app = _daemon(tmp_path)
    try:
        assert (app.user_id, app.user_issuer) == (first, "https://id.example")
        settings["userId"] = uid_for("user:someone-else")
        (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        assert app.user_id == first
        assert local_cell_authority(app) == ALICE
    finally:
        app._close()
