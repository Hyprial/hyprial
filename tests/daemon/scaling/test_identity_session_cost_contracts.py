"""Scale-cost contracts for Agent, Session, and PAC actor observation paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import (
    DesiredState,
    DesiredStateStore,
    InteractiveSession,
)
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.operations.session_ports import (
    HeartbeatSessionCommand,
    RefreshSessionCommand,
)
from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime
from hyprial.daemon.impl.session_actor import SessionActor
from hyprial.identity.impl.agents.actor.ports import (
    AgentMutationCompleted,
    BindAgentCommand,
)
from hyprial.kernel import HarnessLaunchSpec, PortAdmission
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _daemon_dependencies,
)
from tests.daemon.sessions.actors.domains.support import (
    ACTOR,
    LEASE,
    _ScriptedAgentCommands,
    _register,
    _registered,
    _wait_until,
)
from tests.support.scaling import assert_flat, at_scales


OWNER = "scale-owner"
NODE = "scale-node"
SCALE = 12


class _Harnesses:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self.rows = rows

    def status(self) -> tuple[dict[str, object], ...]:
        return self.rows


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, _actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _CountingDesiredState:
    """Count loads on the store *instance* the app already holds.

    Patching the instance (not replacing ``app.desired_state``) also counts
    loads through references captured at construction, such as the one
    AgentSessionDomains/AgentActor keep (review 907 on #1178).
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._real = store.load
        self.loads = 0

        def counting_load(*args: Any, **kwargs: Any) -> object:
            self.loads += 1
            return self._real(*args, **kwargs)

        store.load = counting_load

    def restore(self) -> None:
        self._store.load = self._real


class _CountingSessionStore(DesiredStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.loads = 0
        self.rows_read = 0
        self.bytes_parsed = 0
        self.projection_rebuilds = 0
        self.records = 0
        self.completes = 0

    def load(self):  # type: ignore[no-untyped-def]
        self.loads += 1
        state = super().load()
        rows = tuple(session.to_json() for session in state.interactive_sessions)
        self.rows_read += len(rows)
        self.bytes_parsed += len(
            json.dumps(rows, separators=(",", ":")).encode("utf-8")
        )
        return state

    def record_session_agent_effects(self, effects):  # type: ignore[no-untyped-def]
        self.records += 1
        return super().record_session_agent_effects(effects)

    def complete_session_agent_effect(self, effect_id):  # type: ignore[no-untyped-def]
        self.completes += 1
        return super().complete_session_agent_effect(effect_id)

    def reset_counts(self) -> None:
        self.loads = 0
        self.rows_read = 0
        self.bytes_parsed = 0
        self.projection_rebuilds = 0
        self.records = 0
        self.completes = 0

    def counts(self) -> tuple[int, int, int, int, int]:
        return (
            self.rows_read,
            self.bytes_parsed,
            self.projection_rebuilds,
            self.records,
            self.completes,
        )


def _application(root: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=root,
        socket_path=root / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        **_daemon_dependencies(),
    )
    app._inbox = InboxService(  # type: ignore[assignment]
        root / "inbox.sqlite3", MemoryDeliveryTransport()
    )
    app._presence = _Presence()  # type: ignore[assignment]
    return app


def _seed_supervisor(app: DaemonApplication, size: int) -> str:
    rows: list[dict[str, object]] = []
    for index in range(size):
        name = f"worker-{index:03d}"
        app.desired_state.upsert_harness(
            HarnessLaunchSpec(harness="pi", name=name, headless=True)
        )
        rows.append(
            {
                "id": f"pi:{name}",
                "runtime": "pi",
                "name": name,
                "running": True,
                "pid": 30_000 + index,
            }
        )
    app._harnesses = _Harnesses(tuple(rows))  # type: ignore[assignment]
    return str(rows[-1]["name"])


def _bind_loads(root: Path, size: int) -> int:
    app = _application(root)
    try:
        name = _seed_supervisor(app, size)
        actor_uri = app.agents.create(name).uri
        domains = app._agent_session_domains
        first = BindAgentCommand("bind-first", actor_uri, "pi", "headless", "s1")
        domains.call_agent(first, AgentMutationCompleted)

        counting = _CountingDesiredState(app.desired_state)
        try:
            exact = BindAgentCommand("bind-exact", actor_uri, "pi", "headless", "s1")
            result = domains.call_agent(exact, AgentMutationCompleted)
        finally:
            counting.restore()
        assert not result.changed
        return counting.loads
    finally:
        app._close()


def test_heartbeat_bind_desired_state_loads_are_flat(tmp_path: Path) -> None:
    """An exact AgentActor rebind may resolve only its matching supervisor row."""

    at_n, at_4n = at_scales(
        lambda size: _bind_loads(tmp_path / f"bind-{size}", size), SCALE
    )
    assert_flat(at_n, at_4n)
    assert at_n == 0


def _activity_projection_rebuild_rows(
    root: Path, size: int, monkeypatch: Any
) -> int:
    app = _application(root)
    try:
        for index in range(size):
            app.agents.create(f"activity-{index:03d}")
        registry = app._agent_session_domains._registry
        now_ms = registry.now_ms()
        registry._clock = lambda: now_ms + 120_000

        from hyprial.identity.impl.agents.actor import _generation_ops

        rebuilt = 0
        real_refresh = _generation_ops._refresh_projection

        def counting_refresh(projection, source, liveness, version):  # type: ignore[no-untyped-def]
            nonlocal rebuilt
            rebuilt += len(source.list()) + len(liveness.bindings())
            return real_refresh(projection, source, liveness, version)

        with monkeypatch.context() as patch:
            patch.setattr(_generation_ops, "_refresh_projection", counting_refresh)
            assert app.agents.record_activity("activity-000")
        return rebuilt
    finally:
        app._close()


def test_throttled_activity_updates_one_projection_incrementally(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The once-per-minute activity write never rebuilds the N-agent view."""

    at_n = _activity_projection_rebuild_rows(tmp_path / "activity-10", 10, monkeypatch)
    at_10n = _activity_projection_rebuild_rows(
        tmp_path / "activity-100", 100, monkeypatch
    )
    assert at_n == at_10n == 0


def _steady_activity_registry_reads(root: Path, size: int) -> int:
    app = _application(root)
    try:
        for index in range(size):
            app.agents.create(f"steady-{index:03d}")
        app.agents.record_activity("steady-000")
        registry = app._agent_session_domains._registry
        real_get = registry.get
        reads = 0

        def counting_get(actor):  # type: ignore[no-untyped-def]
            nonlocal reads
            reads += 1
            return real_get(actor)

        registry.get = counting_get  # type: ignore[method-assign]
        assert not app.agents.record_activity("steady-000")
        return reads
    finally:
        app._close()


def test_steady_activity_touch_reads_no_registry_rows(tmp_path: Path) -> None:
    """The one-hertz liveness touch stays on keyed in-memory projections."""

    at_n = _steady_activity_registry_reads(tmp_path / "steady-10", 10)
    at_10n = _steady_activity_registry_reads(tmp_path / "steady-100", 100)
    assert at_n == at_10n == 0


def _seed_unrelated_sessions(store: DesiredStateStore, root: Path, size: int) -> None:
    digest = hashlib.sha256(LEASE.encode()).hexdigest()
    for index in range(size):
        store.claim_interactive(
            InteractiveSession(
                actor=f"agent:{OWNER}:{NODE}:other-{index:03d}",
                cwd=str(root),
                command=("claude",),
                source="claude-channel",
                session_ref=f"other-session-{index:03d}",
                runtime="claude_interactive",
                channel_confirmed=True,
                channel_lease_digest=digest,
            )
        )


def _session_cost(
    root: Path, size: int, command: str
) -> tuple[int, int, int, int, int]:
    store = _CountingSessionStore(root / "desired.json")
    _seed_unrelated_sessions(store, root, size)
    events: list[object] = []
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        store,
        daemon_epoch="scale-epoch",
        event_sink=events.append,
        agent_commands=commands,
    )
    commands.attach(actor)
    projection = actor._session_projection  # noqa: SLF001 - cost seam under test
    real_replace = projection.replace

    def counting_replace(*args: object, **kwargs: object) -> None:
        store.projection_rebuilds += 1
        real_replace(*args, **kwargs)  # type: ignore[arg-type]

    projection.replace = counting_replace  # type: ignore[method-assign]
    try:
        assert actor.submit(_register("register", "session-1")) is PortAdmission.ACCEPTED
        _wait_until(lambda: _registered(events, "register"))
        store.reset_counts()
        if command == "heartbeat":
            submitted = HeartbeatSessionCommand(
                "measure-heartbeat", ACTOR, "session-1", LEASE
            )
        else:
            submitted = RefreshSessionCommand(
                "measure-refresh", ACTOR, "session-1", LEASE
            )
        assert actor.submit(submitted) is PortAdmission.ACCEPTED
        _wait_until(lambda: _registered(events, f"measure-{command}"))
        return store.counts()
    finally:
        assert actor.drain(1.0).complete


def test_steady_heartbeat_persistence_cost_is_flat(tmp_path: Path) -> None:
    """A bound heartbeat reads and rebuilds nothing at N=10 and N=100."""

    at_n = _session_cost(tmp_path / "heartbeat-10", 10, "heartbeat")
    at_10n = _session_cost(tmp_path / "heartbeat-100", 100, "heartbeat")
    # Metrics are rows read, bytes parsed, projection loads/rebuilds, durable
    # effect records, and durable effect completions. Invocation count alone
    # hid the old O(N) load because it was always exactly one.
    assert at_n == at_10n
    assert at_n == (0, 0, 0, 0, 0)


def test_repeated_refresh_persistence_cost_is_flat(tmp_path: Path) -> None:
    """A bound refresh retry reads and writes no desired-state rows."""

    at_n, at_4n = at_scales(
        lambda size: _session_cost(tmp_path / f"refresh-{size}", size, "refresh"),
        SCALE,
    )
    assert_flat(at_n, at_4n)
    assert at_n == (0, 0, 0, 0, 0)


def _observe_cost(
    root: Path, size: int, monkeypatch: Any
) -> tuple[int, int]:
    app = _application(root)
    try:
        name = _seed_supervisor(app, size)
        app.agents.create(name)
        counting = _CountingDesiredState(app.desired_state)
        from_json_calls = 0
        real_from_json = DesiredState.from_json

        def counting_from_json(cls: type[DesiredState], value: object) -> DesiredState:
            nonlocal from_json_calls
            from_json_calls += 1
            return real_from_json(value)

        with monkeypatch.context() as patch:
            patch.setattr(
                DesiredState, "from_json", classmethod(counting_from_json)
            )
            try:
                observation = DaemonActorRuntime(app).observe(name)
            finally:
                counting.restore()
        assert observation.present
        return counting.loads, from_json_calls
    finally:
        app._close()


def test_pac_observe_desired_state_loads_are_flat(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """One actor observation performs no whole desired-state copies."""

    at_n, at_4n = at_scales(
        lambda size: _observe_cost(
            tmp_path / f"observe-{size}", size, monkeypatch
        ),
        SCALE,
    )
    assert_flat(at_n, at_4n)
    assert at_n == (0, 0)


def test_canonical_harness_uri_fallback_avoids_whole_state_copy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The single-name fallback preserves URI answers without parsing state."""

    app = _application(tmp_path)
    try:
        app.desired_state.upsert_harness(
            HarnessLaunchSpec(
                harness="pi",
                name="pinned",
                headless=True,
                pinned_owner="source-owner",
            )
        )
        app.desired_state.upsert_harness(
            HarnessLaunchSpec(harness="pi", name="local", headless=True)
        )
        desired = app.desired_state.load()
        names = ("pinned", "local", "unknown")
        expected = tuple(
            app._canonical_harness_uri(name, desired=desired) for name in names
        )
        from_json_calls = 0
        real_from_json = DesiredState.from_json

        def counting_from_json(cls: type[DesiredState], value: object) -> DesiredState:
            nonlocal from_json_calls
            from_json_calls += 1
            return real_from_json(value)

        monkeypatch.setattr(
            DesiredState, "from_json", classmethod(counting_from_json)
        )
        actual = tuple(app._canonical_harness_uri(name) for name in names)

        assert actual == expected
        assert actual == (
            f"agent:source-owner:{NODE}:pinned",
            f"agent:{OWNER}:{NODE}:local",
            f"agent:{OWNER}:{NODE}:unknown",
        )
        assert from_json_calls == 0
    finally:
        app._close()
