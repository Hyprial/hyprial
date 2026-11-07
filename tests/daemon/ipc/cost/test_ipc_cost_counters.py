"""Per-IPC-method / per-session-command cost counters (``ps`` → ``daemon.ipcStats``).

The counters exist to attribute the daemon's unexplained idle CPU, so what
they must never do is lie in a way two snapshots cannot detect: miss a call,
lose an error, grow without bound under hostile method names, or count the
same CPU on both sides of the IPC/actor boundary.  Each test below pins one
of those properties through the real dispatch (``_response`` with a JSON
line), because in-process ``handle`` calls deliberately bypass the counters.

Decision 2026-11-01 (0.5.5.3 shards-green): the cells runState payload is a
per-request observation cut — observedAtMs and the RunProjection publish
revision advance on every ps call by contract — so the skipped-rebuild
equality drops exactly those two fields and still compares every other
runState field between the skipped and the fresh cut.
"""

from __future__ import annotations

import inspect
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.ipc.ipc_stats import OTHER_KEY, CallCostCounters
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

LEASE = "ipc-cost-lease-token-" + "0" * 24


class _NoHarness:
    def status(self) -> tuple[()]:
        return ()


class _Presence:
    def actor_online(self, actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Inbox:
    """Just enough inbox for ``ps``: nothing pending, nothing held."""

    def pending_all(self) -> tuple[()]:
        return ()

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


def _application(tmp_path: Path) -> DaemonApplication:
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
        **_app_injections(),
    )
    application._harnesses = _NoHarness()  # type: ignore[assignment]
    application._presence = _Presence()  # type: ignore[assignment]
    application._inbox = _Inbox()  # type: ignore[assignment]
    return application


def _close_application(application: DaemonApplication) -> None:
    application._close()
    application._close_home_lease()


@pytest.fixture
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    application = _application(tmp_path)
    yield application
    _close_application(application)


def _ipc(app: DaemonApplication, method: str, params: dict[str, Any]) -> dict:
    return app._response(
        json.dumps(
            {"version": 1, "id": f"req-{method}", "method": method, "params": params}
        ).encode()
    )


def _stats(app: DaemonApplication) -> dict[str, Any]:
    return app.handle("ps", {})["daemon"]["ipcStats"]


def _stats_once_agent_recorded(
    app: DaemonApplication, key: str, *, calls: int = 1, timeout: float = 2.0
) -> dict[str, Any]:
    """Stats after the agent actor has recorded ``key`` at least ``calls`` times.

    The agent actor records a command's cost in a ``finally`` after
    ``_dispatch`` -- and ``_dispatch`` is what publishes the completion the
    IPC call waits on.  So an IPC call can return before its agentActor row
    exists; reading stats straight away races the actor thread (CI unit
    33401: ``agentActor == {}``).  Wait, bounded, for the row itself.
    """

    deadline = time.monotonic() + timeout
    block = _stats(app)
    while block["agentActor"].get(key, {}).get("calls", 0) < calls:
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
        block = _stats(app)
    return block


def _register(app: DaemonApplication) -> None:
    reply = _ipc(
        app,
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
            "channelProtocolVersion": 2,
            "channelLeaseToken": LEASE,
        },
    )
    assert "error" not in reply, reply


def _heartbeat(app: DaemonApplication) -> dict:
    return _ipc(
        app,
        "session.heartbeat",
        {"actor": "alice", "sessionRef": "session-1", "channelLeaseToken": LEASE},
    )


# -- the aggregate itself ----------------------------------------------------


def test_buckets_partition_calls_and_errors_are_counted() -> None:
    counters = CallCostCounters({"m"}, wall=True)
    for wall in (0.0005, 0.005, 0.05, 0.5, 0.0009):
        counters.record("m", cpu_seconds=0.001, wall_seconds=wall)
    counters.record("m", cpu_seconds=0.0, wall_seconds=0.0001, error=True)

    row = counters.snapshot()["m"]
    assert row["calls"] == 6
    assert row["errors"] == 1
    assert (row["lt1ms"], row["lt10ms"], row["lt100ms"], row["ge100ms"]) == (
        3,
        1,
        1,
        1,
    )
    assert row["lt1ms"] + row["lt10ms"] + row["lt100ms"] + row["ge100ms"] == 6
    assert row["threadCpuMs"] == pytest.approx(5.0)
    assert row["maxWallMs"] == pytest.approx(500.0)


def test_unknown_names_fold_into_one_bounded_key() -> None:
    counters = CallCostCounters({"known"}, wall=False)
    for index in range(500):
        counters.record(f"invented-{index}", cpu_seconds=0.0)
    counters.record("known", cpu_seconds=0.0)

    snapshot = counters.snapshot()
    assert set(snapshot) == {OTHER_KEY, "known"}
    assert snapshot[OTHER_KEY]["calls"] == 500
    # The actor shape carries no wall fields: queue wait is not its cost.
    assert set(snapshot["known"]) == {"calls", "errors", "threadCpuMs"}


def test_method_key_set_covers_every_dispatched_method() -> None:
    """Drift guard: a new ``method == "x"`` without a counter key fails here.

    Otherwise the new method's cost would land silently in ``(other)`` and
    the per-method split would stop meaning what it says.
    """

    source = inspect.getsource(DaemonApplication.handle)
    dispatched = set(re.findall(r'method == "([^"]+)"(?!\s*\+)', source))
    for group in re.findall(r"method in \(([^)]*)\)", source):
        dispatched.update(re.findall(r'"([^"]+)"', group))
    assert dispatched, "the source scan found no dispatch comparisons"
    assert dispatched - _IPC_STATS_METHODS == set()


# -- through the real IPC dispatch ------------------------------------------


def test_one_call_counts_once_with_non_negative_cpu(app: DaemonApplication) -> None:
    before = _stats(app)["methods"].get("ping", {"calls": 0})["calls"]
    reply = _ipc(app, "ping", {})
    assert "error" not in reply

    row = _stats(app)["methods"]["ping"]
    assert row["calls"] == before + 1
    assert row["errors"] == 0
    assert row["threadCpuMs"] >= 0
    assert row["wallMs"] >= 0
    assert row["lt1ms"] + row["lt10ms"] + row["lt100ms"] + row["ge100ms"] == row["calls"]


def test_error_reply_increments_errors(app: DaemonApplication) -> None:
    reply = _ipc(app, "session.heartbeat", {"actor": "nobody"})
    assert "error" in reply

    row = _stats(app)["methods"]["session.heartbeat"]
    assert row["calls"] == 1
    assert row["errors"] == 1


def test_unknown_method_folds_into_other(app: DaemonApplication) -> None:
    for name in ("no.such.method", "another.invented.name"):
        reply = _ipc(app, name, {})
        assert reply["error"]["code"] == "METHOD_NOT_FOUND"

    methods = _stats(app)["methods"]
    assert "no.such.method" not in methods
    assert "another.invented.name" not in methods
    assert methods[OTHER_KEY]["calls"] == 2
    assert methods[OTHER_KEY]["errors"] == 2


def test_in_process_handle_is_not_charged(app: DaemonApplication) -> None:
    """Only socket requests are "IPC"; restore/lifecycle self-calls are not."""

    app.handle("ping", {})
    assert "ping" not in _stats(app)["methods"]


def test_ps_exposes_the_block_with_process_cpu(app: DaemonApplication) -> None:
    daemon = app.handle("ps", {})["daemon"]
    block = daemon["ipcStats"]
    assert daemon["ipcClients"] == {
        "active": 0,
        "highWater": 0,
        "limit": 64,
        "saturated": False,
    }
    assert set(block) == {
        "sinceMs",
        "processCpuSeconds",
        "coverageCpuMs",
        "methods",
        "sessionActor",
        "agentActor",
        "effectAdmission",
        "sessionPersistenceIo",
        "statePersistence",
        "runtime",
        "runtimeOverflowFolds",
        "instrumentedCpuMs",
        "ipcPathCpuMs",
    }
    assert isinstance(block["processCpuSeconds"], float)
    assert block["processCpuSeconds"] > 0
    assert isinstance(block["sinceMs"], int)
    # Additive: the pre-existing daemon keys are untouched.
    for key in ("running", "pid", "epoch", "nodeId", "owner", "socket"):
        assert key in daemon


def test_ipc_client_high_water_survives_after_clients_leave(
    app: DaemonApplication,
) -> None:
    clients = {object(), object(), object()}
    with app._ipc_clients_lock:
        app._ipc_clients.update(clients)  # type: ignore[arg-type]
    assert app._ipc_client_status()["highWater"] == 3
    with app._ipc_clients_lock:
        app._ipc_clients.difference_update(clients)  # type: ignore[arg-type]
    assert app._ipc_client_status() == {
        "active": 0,
        "highWater": 3,
        "limit": 64,
        "saturated": False,
    }


def test_session_actor_records_the_heartbeat_command(app: DaemonApplication) -> None:
    _register(app)
    assert "error" not in _heartbeat(app)

    actor = _stats(app)["sessionActor"]
    assert actor["HeartbeatSessionCommand"]["calls"] == 1
    assert actor["HeartbeatSessionCommand"]["threadCpuMs"] >= 0
    assert actor["RegisterSessionCommand"]["calls"] == 1


def test_instrumented_cpu_never_exceeds_process_cpu(app: DaemonApplication) -> None:
    """The calibration contract's upper bound, in-process.

    CPU summed over all six sides over a window must not exceed the process
    CPU delta of the same window.  If any side read a clock that includes
    another thread (process CPU instead of thread CPU), work would be
    counted twice and this breaks.

    A concurrent, uninstrumented CPU burner makes the bound sharp: it is one
    more disjoint thread, so instrumented + burner must still fit in Δ.  A
    side that read process CPU would also absorb the burner's CPU during its
    own spans, which is exactly the double counting the bound exists to
    catch -- without the burner that excess can hide in the uncovered slack.
    """

    import threading
    import time

    _register(app)
    _heartbeat(app)
    stop = threading.Event()
    burned: list[float] = []

    def burn() -> None:
        started = time.thread_time()
        while not stop.is_set():
            sum(range(500))
            # Yield the GIL each slice so the heartbeats still make progress;
            # the burner only needs to be running DURING the timed spans.
            time.sleep(0)
        burned.append(time.thread_time() - started)

    before = _stats(app)
    burner = threading.Thread(target=burn)
    burner.start()
    try:
        for index in range(20):
            assert "error" not in _heartbeat(app)
            # Agent-actor-heavy work too (registry insert + projection
            # rebuild on that thread), so every side has spans long enough
            # to absorb the burner if it read the wrong clock.
            created = _ipc(app, "agent.create", {"name": f"bound-{index}"})
            assert "error" not in created, created
    finally:
        stop.set()
        burner.join()
    after = _stats(app)

    instrumented_ms = after["instrumentedCpuMs"] - before["instrumentedCpuMs"]
    ipc_path_ms = after["ipcPathCpuMs"] - before["ipcPathCpuMs"]
    burner_ms = 1000 * burned[0]
    delta_ms = 1000 * (after["processCpuSeconds"] - before["processCpuSeconds"])
    # Totals are rounded to 0.001 ms per row per snapshot; allow that much.
    assert 0 < ipc_path_ms <= instrumented_ms + 0.01
    assert instrumented_ms + burner_ms <= delta_ms + 0.5, (
        instrumented_ms,
        burner_ms,
        delta_ms,
    )


def test_four_sides_cover_a_pure_heartbeat_load(app: DaemonApplication) -> None:
    """Coverage of a heartbeat-only window clears the contract's 70 %."""

    _register(app)
    _heartbeat(app)
    before = _stats(app)
    for _ in range(40):
        assert "error" not in _heartbeat(app)
    after = _stats(app)
    instrumented_ms = after["instrumentedCpuMs"] - before["instrumentedCpuMs"]
    delta_ms = 1000 * (after["processCpuSeconds"] - before["processCpuSeconds"])
    assert instrumented_ms / delta_ms >= 0.70, (instrumented_ms, delta_ms)


def test_session_effect_persistence_sides_preserve_ipc_origin(tmp_path: Path) -> None:
    # The first refresh of a new daemon generation re-decides the binding
    # durably (record + complete); a repeated refresh or a steady heartbeat of
    # an already-bound session no longer persists at all (see
    # test_steady_heartbeat_skips_the_projection_rebuild_and_the_durable_rewrite).
    first = _application(tmp_path)
    try:
        _register(first)
    finally:
        _close_application(first)
    app = _application(tmp_path)
    try:
        refreshed = _ipc(
            app,
            "session.refresh",
            {"actor": "alice", "sessionRef": "session-1", "channelLeaseToken": LEASE},
        )
        assert "error" not in refreshed
        block = _stats(app)
    finally:
        _close_application(app)

    session_io = block["sessionPersistenceIo"]
    state = block["statePersistence"]
    assert session_io["record_session_agent_effects/ipc"]["calls"] >= 1
    assert session_io["complete_session_agent_effect/ipc"]["calls"] >= 1
    assert state["RecordSessionAgentEffects/ipc"]["calls"] >= 1
    assert state["CompleteSessionAgentEffect/ipc"]["calls"] >= 1


def test_background_state_work_is_not_folded_into_ipc_path(
    app: DaemonApplication,
) -> None:
    before = _stats(app)
    app._state_persistence.settled_desired.record_session_agent_effects(())
    after = _stats(app)

    row = after["statePersistence"]["RecordSessionAgentEffects/background"]
    assert row["calls"] == 1
    state_delta = row["threadCpuMs"]
    assert after["instrumentedCpuMs"] - before["instrumentedCpuMs"] == pytest.approx(
        state_delta, abs=0.01
    )
    assert after["ipcPathCpuMs"] - before["ipcPathCpuMs"] == pytest.approx(
        0.0, abs=0.01
    )


def test_totals_are_the_sums_of_their_sides(app: DaemonApplication) -> None:
    _register(app)
    _heartbeat(app)
    _ipc(app, "agent.create", {"name": "bob"})
    block = _stats_once_agent_recorded(app, "CreateAgentCommand/other")

    def side(name: str, suffix: str = "") -> float:
        return sum(
            row["threadCpuMs"]
            for key, row in block[name].items()
            if key.endswith(suffix)
        )

    common = side("methods") + side("sessionActor") + side("effectAdmission")
    assert block["instrumentedCpuMs"] == pytest.approx(
        common
        + side("agentActor")
        + side("sessionPersistenceIo")
        + side("statePersistence"),
        abs=0.01,
    )
    assert block["ipcPathCpuMs"] == pytest.approx(
        common
        + side("agentActor", "/session")
        + side("sessionPersistenceIo", "/ipc")
        + side("statePersistence", "/ipc"),
        abs=0.01,
    )


def test_agent_actor_and_effect_admission_sides_split_by_origin(
    app: DaemonApplication,
) -> None:
    _register(app)
    # #1889: a steady heartbeat on a live binding sends no bind, so release the
    # binding first; the heartbeat must then rebind through a session effect.
    app._agent_liveness.release(app.agents.get("alice").uri)  # type: ignore[union-attr]
    assert "error" not in _heartbeat(app)
    assert "error" not in _ipc(app, "agent.create", {"name": "bob"})
    _stats_once_agent_recorded(app, "BindAgentCommand/session", calls=2)
    block = _stats_once_agent_recorded(app, "CreateAgentCommand/other")

    agent = block["agentActor"]
    # register + the rebinding heartbeat each sent one bind through a session effect.
    assert agent["BindAgentCommand/session"]["calls"] == 2
    assert "BindAgentCommand/other" not in agent
    # agent.create came straight from the IPC handler, not from a session.
    assert agent["CreateAgentCommand/other"]["calls"] >= 1
    assert "CreateAgentCommand/session" not in agent
    assert block["effectAdmission"]["bind"]["calls"] == 2


def test_counters_off_switch_skips_recording(app: DaemonApplication) -> None:
    app._ipc_stats.enabled = False
    _ipc(app, "ping", {})
    app._ipc_stats.enabled = True
    assert "ping" not in _stats(app)["methods"]


def test_steady_heartbeats_keep_agent_liveness_past_its_ttl(
    app: DaemonApplication,
) -> None:
    """Why the heartbeat still emits its bind effect (see ipc-cost report).

    The exact-binding branch of the Agent actor's ``_bind`` is the only
    ``AgentLiveness.touch`` on this path, and an interactive agent's verdict
    is ``heartbeat_fresh`` (AGENT_HEARTBEAT_TTL_SECONDS).  Suppressing the
    "redundant" bind when the binding is already exact would turn a live,
    heartbeating session offline one TTL later.
    """

    from hyprial.identity.impl.agents.state.liveness import AGENT_HEARTBEAT_TTL_SECONDS

    now = [1000.0]
    app._clock = lambda: now[0]  # type: ignore[method-assign]
    _register(app)
    uri = app.agents.get("alice").uri  # type: ignore[union-attr]
    assert app._interactive_actor_liveness(uri) is True
    steps = int(2 * AGENT_HEARTBEAT_TTL_SECONDS)
    for _ in range(steps):
        now[0] += 1.0
        # Checked just BEFORE each heartbeat: liveness must never lapse
        # between beats.  A lapse self-heals on the next beat (a stale
        # binding rebinds), so checking only after beats would miss it.
        assert app._interactive_actor_liveness(uri) is True
        assert "error" not in _heartbeat(app)
    assert app._interactive_actor_liveness(uri) is True
    now[0] += AGENT_HEARTBEAT_TTL_SECONDS + 1.0
    assert app._interactive_actor_liveness(uri) is False



# -- option (a): the exact-binding bind skips the projection rebuild --------


def _agent_actor_module():  # type: ignore[no-untyped-def]
    from hyprial.identity.impl.agents.actor import _AgentGeneration

    # The inherited bind method uses the generation-operations module's
    # imported global, not the package face or the function-definition module.
    return inspect.getmodule(_AgentGeneration._bind)


def _count_rebuilds(monkeypatch: pytest.MonkeyPatch, app: DaemonApplication):  # type: ignore[no-untyped-def]
    module = _agent_actor_module()
    original_refresh = module._refresh_projection
    registry = app._agent_registry
    original_list = registry.list
    counts = {"refresh": 0, "list": 0}

    def refresh(*args, **kwargs):  # type: ignore[no-untyped-def]
        counts["refresh"] += 1
        return original_refresh(*args, **kwargs)

    def listing(*args, **kwargs):  # type: ignore[no-untyped-def]
        counts["list"] += 1
        return original_list(*args, **kwargs)

    monkeypatch.setattr(module, "_refresh_projection", refresh)
    monkeypatch.setattr(registry, "list", listing)
    return counts


def _count_saves(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from hyprial.daemon.impl.desired_state import DesiredStateStore

    original = DesiredStateStore.save
    counts = {"save": 0}

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        counts["save"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DesiredStateStore, "save", save)
    return counts


def _projected_binding(app: DaemonApplication):  # type: ignore[no-untyped-def]
    uri = app.agents.get("alice").uri  # type: ignore[union-attr]
    return app._agent_session_domains.agent.read_binding(uri)


def test_steady_heartbeat_skips_the_projection_rebuild_and_the_durable_rewrite(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(app)
    assert "error" not in _heartbeat(app)
    rebuilds = _count_rebuilds(monkeypatch, app)
    saves = _count_saves(monkeypatch)

    for _ in range(5):
        assert "error" not in _heartbeat(app)

    assert rebuilds == {"refresh": 0, "list": 0}
    # Changed 2026-10-05: this used to pin record + complete (2 full
    # desired-state rewrites) per heartbeat.  Once the session is durably
    # bound, a heartbeat's bind is only a liveness touch and carries no
    # custody -- at 1 Hz per session those rewrites saturated the single
    # writer in production (session.register failed 35 of 39).  Lost or moved
    # bindings still re-bind: see the next test.
    assert saves["save"] == 0
    assert _projected_binding(app).session_id == "session-1"


def test_lost_changed_or_stale_binding_still_rebuilds_and_rebinds(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.identity.impl.agents.state.liveness import AGENT_HEARTBEAT_TTL_SECONDS

    now = [1000.0]
    app._clock = lambda: now[0]  # type: ignore[method-assign]
    _register(app)
    uri = app.agents.get("alice").uri  # type: ignore[union-attr]
    rebuilds = _count_rebuilds(monkeypatch, app)

    # Lost: released behind the session's back.
    app._agent_liveness.release(uri)
    assert _projected_binding(app) is None
    before = rebuilds["refresh"]
    assert "error" not in _heartbeat(app)
    assert rebuilds["refresh"] > before
    assert _projected_binding(app).session_id == "session-1"

    # Changed: another interactive session took the binding.
    app._agent_liveness.bind(
        uri, harness="claude", runtime="interactive", session_id="elsewhere"
    )
    assert _projected_binding(app).session_id == "elsewhere"
    before = rebuilds["refresh"]
    assert "error" not in _heartbeat(app)
    assert rebuilds["refresh"] > before
    assert _projected_binding(app).session_id == "session-1"

    # Stale: same identity, but the liveness heartbeat lapsed.
    bound_at = _projected_binding(app).bound_at_ms
    now[0] += AGENT_HEARTBEAT_TTL_SECONDS + 1.0
    assert app._interactive_actor_liveness(uri) is False
    assert "error" not in _heartbeat(app)
    # #1889: a lapsed but unchanged binding is renewed by the liveness touch;
    # no rebuild is required, but liveness and the bind time must recover.
    assert app._interactive_actor_liveness(uri) is True
    assert _projected_binding(app).bound_at_ms >= bound_at


def test_skipped_rebuild_leaves_ps_and_targets_identical_to_a_fresh_one(
    app: DaemonApplication,
) -> None:
    now = [1000.0]
    app._clock = lambda: now[0]  # type: ignore[method-assign]
    _register(app)
    uri = app.agents.get("alice").uri  # type: ignore[union-attr]
    for _ in range(3):
        now[0] += 1.0
        assert "error" not in _heartbeat(app)
    # A transition in the middle: the binding moves away and the next
    # heartbeat takes it back, then steady heartbeats resume.
    app._agent_liveness.bind(
        uri, harness="claude", runtime="interactive", session_id="elsewhere"
    )
    for _ in range(3):
        now[0] += 1.0
        assert "error" not in _heartbeat(app)

    observations: list[tuple[int, int]] = []

    def observed() -> tuple[object, object]:
        agents = app.handle("ps", {})["agents"]
        for row in agents:
            # GUI run-state adds per-read observation coordinates. Compare all
            # state/coverage fields, but not a fresh sample's time/revision.
            state = row["runState"]
            observations.append((state.pop("observedAtMs"), state.pop("revision")))
        return agents, app.handle("targets", {})

    skipped = observed()
    actor = app._agent_session_domains.agent
    _agent_actor_module()._refresh_projection(
        actor._projection, actor._registry, actor._liveness, actor.version
    )
    assert observed() == skipped
    assert len(observations) == 2
    assert observations[1][0] >= observations[0][0]
    assert observations[1][1] > observations[0][1]


# -- the /session origin discriminator, both directions ----------------------


def _row_sum(block: dict[str, Any], side: str, suffix: str = "") -> float:
    return sum(
        row["threadCpuMs"]
        for key, row in block[side].items()
        if key.endswith(suffix)
    )


def _non_agent_ms(block: dict[str, Any]) -> float:
    return (
        _row_sum(block, "methods")
        + _row_sum(block, "sessionActor")
        + _row_sum(block, "effectAdmission")
        + _row_sum(block, "sessionPersistenceIo")
        + _row_sum(block, "statePersistence")
    )


def _non_agent_ipc_ms(block: dict[str, Any]) -> float:
    return (
        _row_sum(block, "methods")
        + _row_sum(block, "sessionActor")
        + _row_sum(block, "effectAdmission")
        + _row_sum(block, "sessionPersistenceIo", "/ipc")
        + _row_sum(block, "statePersistence", "/ipc")
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Known gap from #1889, tracked for 0.5.5.1: every heartbeat also sends "
        "RecordAgentActivityIfBindingCommand from routes.py directly, not through a "
        "session effect, so its agent-actor CPU is attributed to /other and missed "
        "by the IPC-path total. Remove this mark when the record is session-owned."
    ),
)
def test_session_effect_agent_work_counts_in_the_ipc_path(
    app: DaemonApplication,
) -> None:
    """(a) A bind sent by a SessionActor effect is ``*/session`` and IPC path.

    Measured as a delta around one heartbeat: registering creates the agent
    straight from the IPC handler (``CreateAgentCommand/other``), which is
    correct but not what this test is about.
    """

    _register(app)
    # #1889: only a heartbeat that has to rebind sends BindAgentCommand.
    app._agent_liveness.release(app.agents.get("alice").uri)  # type: ignore[union-attr]
    before = _stats_once_agent_recorded(app, "BindAgentCommand/session")
    assert "error" not in _heartbeat(app)
    after = _stats_once_agent_recorded(
        app,
        "BindAgentCommand/session",
        calls=before["agentActor"]["BindAgentCommand/session"]["calls"] + 1,
    )

    def delta(side: str, suffix: str = "") -> float:
        return _row_sum(after, side, suffix) - _row_sum(before, side, suffix)

    changed = {
        key
        for key, row in after["agentActor"].items()
        if row["calls"] != before["agentActor"].get(key, {}).get("calls", 0)
    }
    assert changed == {"BindAgentCommand/session"}, changed
    session_ms = delta("agentActor", "/session")
    assert session_ms > 0
    assert delta("agentActor", "/other") == pytest.approx(0.0, abs=0.001)
    non_agent = (
        delta("methods")
        + delta("sessionActor")
        + delta("effectAdmission")
        + delta("sessionPersistenceIo", "/ipc")
        + delta("statePersistence", "/ipc")
    )
    ipc_path = after["ipcPathCpuMs"] - before["ipcPathCpuMs"]
    instrumented = after["instrumentedCpuMs"] - before["instrumentedCpuMs"]
    assert ipc_path == pytest.approx(non_agent + session_ms, abs=0.01)
    assert ipc_path == pytest.approx(instrumented, abs=0.01)


def test_non_session_agent_work_is_instrumented_but_not_ipc_path(
    app: DaemonApplication,
) -> None:
    """(b) An Agent command sent straight from IPC is ``*/other``.

    It counts in ``instrumentedCpuMs`` (coverage) and NOT in
    ``ipcPathCpuMs``: the gap between the two totals is exactly its CPU.
    """

    assert "error" not in _ipc(app, "agent.create", {"name": "bob"})
    block = _stats_once_agent_recorded(app, "CreateAgentCommand/other")

    agent = block["agentActor"]
    assert set(agent) == {"CreateAgentCommand/other"}, agent
    other_ms = _row_sum(block, "agentActor", "/other")
    assert other_ms > 0
    assert block["instrumentedCpuMs"] == pytest.approx(
        _non_agent_ms(block) + other_ms, abs=0.01
    )
    assert block["ipcPathCpuMs"] == pytest.approx(
        _non_agent_ipc_ms(block), abs=0.01
    )
    assert block["instrumentedCpuMs"] - block["ipcPathCpuMs"] == pytest.approx(
        other_ms, abs=0.01
    )


def test_errors_are_broken_down_by_code_and_capped() -> None:
    # 2026-10-05: session.refresh showed 31 errors, all under 1 ms, and nothing
    # said whether they were fence rejections or overload.
    counters = CallCostCounters({"m"}, wall=True)
    counters.record("m", cpu_seconds=0.0, error=True, error_code="STALE_SESSION")
    counters.record("m", cpu_seconds=0.0, error=True, error_code="STALE_SESSION")
    counters.record("m", cpu_seconds=0.0, error=True, error_code="SESSION_SUPERSEDED")
    counters.record("m", cpu_seconds=0.0)
    row = counters.snapshot()["m"]
    assert row["errors"] == 3
    assert row["errorCodes"] == {"SESSION_SUPERSEDED": 1, "STALE_SESSION": 2}

    for index in range(40):
        counters.record("m", cpu_seconds=0.0, error=True, error_code=f"CODE_{index}")
    codes = counters.snapshot()["m"]["errorCodes"]
    assert len(codes) <= 17 and codes["other"] > 0


def test_ipc_errors_are_recorded_with_their_wire_code(app: DaemonApplication) -> None:
    reply = _heartbeat(app)  # never registered: refused by the session fence
    assert "error" in reply
    row = _stats(app)["methods"]["session.heartbeat"]
    assert row["errorCodes"] == {reply["error"]["code"]: 1}


def test_a_key_without_errors_has_no_error_codes() -> None:
    counters = CallCostCounters({"m"}, wall=False)
    counters.record("m", cpu_seconds=0.001)
    assert "errorCodes" not in counters.snapshot()["m"]


def test_an_admission_refusal_is_counted_with_its_code(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Closed:
        def start(self, method: str) -> None:
            return None

    monkeypatch.setattr(app, "_ipc_request_owner", _Closed())
    reply = _ipc(app, "ping", {})
    monkeypatch.undo()
    assert "error" in reply
    row = _stats(app)["methods"]["ping"]
    assert row["errorCodes"] == {reply["error"]["code"]: 1}


def test_an_uncoded_exception_is_counted_as_daemon_error(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = app.handle

    def handle(method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        if method == "ping":
            raise RuntimeError("no code attribute")
        return original(method, params, **kwargs)

    monkeypatch.setattr(app, "handle", handle)
    reply = _ipc(app, "ping", {})
    assert reply["error"]["code"] == "DAEMON_ERROR"
    assert _stats(app)["methods"]["ping"]["errorCodes"] == {"DAEMON_ERROR": 1}
