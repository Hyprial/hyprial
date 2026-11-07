"""Card 259 (d1579b24): one actor-status snapshot parses desired state once.

The 2026-09-04 production dump (pid 86380) showed ``ps``/``targets`` spending
their whole IPC budget re-parsing the 93 KB desired-state document: every
actor's liveness probe walks every supervised harness row, and each row
re-loaded the store through ``_canonical_harness_uri``.  77 actors x N
harnesses pushed one snapshot past the 15 s client deadline.

The fix has two halves, each pinned here:

* T1 — the snapshot loads the document once and hands it down the probe
  chain (``_actor_status_snapshot`` -> ``registered_status`` ->
  ``actor_worker_running`` -> ``_managed_worker_running`` ->
  ``_canonical_harness_uri``), so one snapshot is one read.
* T2 — the daemon's StatePersistence authority serves snapshots from an
  immutable memory projection after committed mutations.

T3 pins that threading the state down changes no output byte: the rows are
identical to the per-lookup-loading shape on the same fixture.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.status import build_actor_status_snapshot
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


OWNER = "op"
NODE = "node-1"

#: The production shape this card was filed against: 77 actors in one
#: snapshot, each liveness probe iterating every supervised harness row.
ACTORS = 77
SUPERVISOR_ROWS = 3


class _Harnesses:
    """The supervisor seam, in the shape ``_managed_worker_running`` reads."""

    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self.rows = rows

    def status(self) -> tuple[dict[str, object], ...]:
        return self.rows


class _Presence:
    def __init__(self, actors: tuple[str, ...] = ()) -> None:
        self.actors = actors

    def online_actors(self) -> tuple[str, ...]:
        return self.actors

    def actor_online(self, actor: str) -> bool:
        return actor in self.actors

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _CountingDesiredState:
    """A delegating counter around the real store; no behavior of its own."""

    def __init__(self, inner: DesiredStateStore) -> None:
        self._inner = inner
        self.load_calls = 0

    def load(self) -> object:
        self.load_calls += 1
        return self._inner.load()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _application(tmp_path: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = _Harnesses(())  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    _TEST_APPLICATIONS.append(app)
    return app


_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_test_applications():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        app = _TEST_APPLICATIONS.pop()
        if not app._agent_session_domains._domains_closed:
            if app._runtime is not None and not hasattr(app._runtime, "stop"):
                app._runtime = None
            app._close()


def _seed_workers(
    app: DaemonApplication, count: int = SUPERVISOR_ROWS
) -> tuple[dict[str, object], ...]:
    """Desired harness specs plus matching supervisor rows; no real process."""

    rows: list[dict[str, object]] = []
    for index in range(count):
        name = f"worker-{index}"
        app.desired_state.upsert_harness(
            HarnessLaunchSpec(
                harness="pi",
                name=name,
                headless=True,
                command=("/nonexistent/hyprial-card-259-probe",),
            )
        )
        rows.append(
            {
                "id": f"pi:{name}",
                "runtime": "pi",
                "name": name,
                "running": index == 0,
                "pid": 24_000 + index,
            }
        )
    return tuple(rows)


def _patch_counting_read_document(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    """Count SQLite document reads (the intg authority; U0a-2)."""
    from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow

    reads = [0]
    real = DesiredStateSqliteShadow.read_document

    def counting(self: DesiredStateSqliteShadow) -> object:
        reads[0] += 1
        return real(self)

    monkeypatch.setattr(DesiredStateSqliteShadow, "read_document", counting)
    return reads


def test_t1_snapshot_loads_desired_state_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T1 (card 259): 77 actors x 3 supervised rows, exactly one store load.

    Mutation guard: dropping the threaded desired state — letting every
    liveness probe load its own copy — drives the count past one and this
    goes red.
    """

    app = _application(tmp_path)
    app._harnesses = _Harnesses(_seed_workers(app))  # type: ignore[assignment]
    for index in range(ACTORS):
        app.agents.create(f"actor-{index:02d}")

    disk_reads = _patch_counting_read_document(monkeypatch)
    counting = _CountingDesiredState(app.desired_state)
    app.desired_state = counting  # type: ignore[assignment]

    rows = app._actor_status_snapshot()

    assert len(rows) == ACTORS + SUPERVISOR_ROWS
    assert counting.load_calls == 1
    assert disk_reads[0] == 0
    by_actor = {row["actor"]: row for row in rows}
    assert by_actor[f"agent:{OWNER}:{NODE}:worker-0"]["status"] == "online"
    assert by_actor[f"agent:{OWNER}:{NODE}:worker-1"]["status"] == "offline"
    assert by_actor[f"agent:{OWNER}:{NODE}:actor-00"]["status"] == "offline"


def test_t1b_request_snapshot_and_actor_snapshot_share_one_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Production path: the ps/agent handlers install the request snapshot
    (intg U2) and then build the actor table (dev card 259 P1).  Both used
    to load; the snapshot now hands its state down, so one request is
    ONE projection load and no SQLite document read.
    """

    app = _application(tmp_path)
    app._harnesses = _Harnesses(_seed_workers(app))  # type: ignore[assignment]
    for index in range(ACTORS):
        app.agents.create(f"actor-{index:02d}")

    disk_reads = _patch_counting_read_document(monkeypatch)
    counting = _CountingDesiredState(app.desired_state)
    app.desired_state = counting  # type: ignore[assignment]

    with app._worker_status_snapshot():
        rows = app._actor_status_snapshot()

    assert len(rows) == ACTORS + SUPERVISOR_ROWS
    assert counting.load_calls == 1
    assert disk_reads[0] == 0
    by_actor = {row["actor"]: row for row in rows}
    assert by_actor[f"agent:{OWNER}:{NODE}:worker-0"]["status"] == "online"
    assert by_actor[f"agent:{OWNER}:{NODE}:worker-1"]["status"] == "offline"
    assert by_actor[f"agent:{OWNER}:{NODE}:actor-00"]["status"] == "offline"


def test_status_projection_never_opens_sqlite_and_tracks_committed_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow

    app = _application(tmp_path)
    app._harnesses = _Harnesses(())  # type: ignore[assignment]
    observed_reads = 0
    real = DesiredStateSqliteShadow.read_document

    def counting(self: DesiredStateSqliteShadow) -> object:
        nonlocal observed_reads
        observed_reads += 1
        return real(self)

    monkeypatch.setattr(DesiredStateSqliteShadow, "read_document", counting)
    before = app.desired_state.load()
    app._actor_status_snapshot()
    assert observed_reads == 0

    app.desired_state.upsert_harness(
        HarnessLaunchSpec(harness="pi", name="new-worker", headless=True)
    )
    assert observed_reads > 0  # the writer did the required SQLite work
    observed_reads = 0
    after = app.desired_state.load()
    app._actor_status_snapshot()
    assert {item.name for item in after.harnesses} == {
        *(item.name for item in before.harnesses), "new-worker"
    }
    assert observed_reads == 0


# T2 now lives at the StatePersistence authority: committed mutations publish
# a versioned memory projection, and request snapshots never open SQLite.
def test_t3_threaded_state_changes_no_snapshot_byte(
    tmp_path: Path,
) -> None:
    """T3 (card 259): the threaded desired state alters no output byte.

    The reference side is the pre-fix shape on the same fixture: every
    status lookup loads the store itself (``desired=None``), exactly what
    each probe did before the parameter existed.
    """

    app = _application(tmp_path)
    app._harnesses = _Harnesses(_seed_workers(app))  # type: ignore[assignment]
    for index in range(ACTORS):
        app.agents.create(f"actor-{index:02d}")

    threaded = app._actor_status_snapshot()

    desired = app.desired_state.load()
    sessions = {session.actor: session for session in desired.interactive_sessions}

    def status_for_actor(actor: str, online: frozenset[str]) -> str:
        session = sessions.get(actor)
        if (
            session is not None
            and session.source == "claude-channel"
            and session.channel_lease_digest is not None
            and not app._channel_alive(session, app._clock())
        ):
            return "offline"
        agent = app.agents.get(actor)
        if agent is not None:
            return app._registered_agent_status(agent)
        return app._agent_target_status(actor, online)

    reference = build_actor_status_snapshot(
        owner=app.owner,
        node_id=app.node_id,
        connectors=app._harnesses.status(),
        interactive_sessions=(
            {
                "actor": session.actor,
                "runtime": session.runtime,
                "source": session.source,
            }
            for session in desired.interactive_sessions
        ),
        registered_actors=(agent.uri for agent in app.agents.list()),
        presence_actors=app._presence.online_actors(),
        status_for_actor=status_for_actor,
    )

    # C adds runState; the desired-state refactor must still change no old field.
    for row in threaded:
        if row["actor"] in {agent.uri for agent in app.agents.list()}:
            assert row.pop("runState")["state"] == "unknown"
    assert json.dumps(threaded, sort_keys=True) == json.dumps(reference, sort_keys=True)


class _CountingHarnesses(_Harnesses):
    """``_Harnesses`` plus the supervisor ``status()`` seam, counted.

    ``_managed_worker_running`` falls back to a live ``status()`` round trip
    for every verdict when no request snapshot is installed; the count is
    the only thing that tells a batch path from N live queries.
    """

    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        super().__init__(())
        self._rows = rows
        self.status_calls = 0

    def status(self) -> tuple[dict[str, object], ...]:
        self.status_calls += 1
        return self._rows


def test_t4_targets_is_a_batch_path_like_ps(tmp_path: Path) -> None:
    """Production 2026-09-05 (c3daa97e): ``ps`` answered in 0.2s and
    ``targets`` timed out at 15s on the same registry.  ``ps`` installs the
    request snapshot (intg U2); ``targets`` built its rows bare, so every
    registered agent's liveness verdict paid one supervisor ``status()``
    round trip -- N round trips for one request.  Red on base: ACTORS+
    calls; green: the snapshot's single call is shared by every row.
    """
    app = _application(tmp_path)
    harnesses = _CountingHarnesses(_seed_workers(app))
    app._harnesses = harnesses  # type: ignore[assignment]
    for index in range(ACTORS):
        app.agents.create(f"actor-{index:02d}")
    counting = _CountingDesiredState(app.desired_state)
    app.desired_state = counting  # type: ignore[assignment]
    try:
        targets = app.handle("targets", {"kind": "agent"})["targets"]
    finally:
        app._close()
    assert len(targets) >= ACTORS
    assert harnesses.status_calls == 1, harnesses.status_calls
    assert counting.load_calls == 1


def _top_snapshot_document_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, actors: int
) -> int:
    """SQLite desired-state reads for one ``top.snapshot`` with ``actors`` online.

    Uses the production presence wrapper -- ``_LocalPresence`` with the real
    interactive-liveness verdict -- because that is where the per-actor
    liveness probe runs.  The tests above install a bare ``_Presence`` fake,
    which skips that probe entirely; that is how ``top.snapshot`` kept an
    O(actors x connectors) path after card 259 fixed ``ps``.
    """

    from hyprial.daemon.impl.application import _LocalPresence

    app = _application(tmp_path)
    app._harnesses = _Harnesses(_seed_workers(app))  # type: ignore[assignment]
    online = [f"agent:{OWNER}:{NODE}:worker-{index}" for index in range(SUPERVISOR_ROWS)]
    for index in range(actors):
        online.append(app.agents.create(f"actor-{index:02d}").uri)
    app._presence = _LocalPresence(  # type: ignore[assignment]
        _Presence(tuple(online)),
        NODE,
        interactive_liveness=app._interactive_actor_liveness,
    )
    reads = _patch_counting_read_document(monkeypatch)
    app.handle("top.snapshot", {})
    monkeypatch.undo()
    return reads[0]


def test_t4_top_snapshot_reads_desired_state_a_constant_number_of_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``top.snapshot`` is a batch path and must install the request snapshot.

    Production 2026-09-17 (pid 20514, SIGUSR1 stack dump): the IPC thread for
    ``top.snapshot`` sat in ``_actor_status_snapshot -> online_actors ->
    liveness_keeps -> ... -> _managed_worker_running -> _canonical_harness_uri
    -> desired_state.load``; ``hyprial agent top`` timed out at 15 s every time while
    ``ps`` answered.  ``_managed_worker_running`` says it outright: batch paths
    must install ``_worker_status_snapshot`` or pay one supervisor round trip
    plus one desired-state load per connector, per actor.

    The count must not grow with the number of online actors.
    """

    many = _top_snapshot_document_reads(monkeypatch, tmp_path / "many", ACTORS)
    few = _top_snapshot_document_reads(monkeypatch, tmp_path / "few", 7)
    assert many == few, (many, few)
    assert many <= 3, many


def _maintenance_liveness_document_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, actors: int
) -> int:
    """SQLite desired-state reads for one maintenance-tick liveness pass."""

    from hyprial.daemon.impl.application import _LocalPresence

    app = _application(tmp_path)
    app._harnesses = _Harnesses(_seed_workers(app))  # type: ignore[assignment]
    online = [f"agent:{OWNER}:{NODE}:worker-{index}" for index in range(SUPERVISOR_ROWS)]
    for index in range(actors):
        online.append(app.agents.create(f"actor-{index:02d}").uri)
    app._presence = _LocalPresence(  # type: ignore[assignment]
        _Presence(tuple(online)),
        NODE,
        interactive_liveness=app._interactive_actor_liveness,
    )
    reads = _patch_counting_read_document(monkeypatch)
    app._running_actor_uris()
    monkeypatch.undo()
    return reads[0]


def test_t5_maintenance_tick_reads_desired_state_a_constant_number_of_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The maintenance tick is a batch path too (every ~1 s, not per request).

    Production 2026-09-25 (pid 25448, 273 agents, three SIGUSR1 dumps): the
    tick thread sat in ``_watch_inbox_collection -> _running_actor_uris ->
    _actor_status_snapshot -> online_actors -> liveness_keeps -> ... ->
    _managed_worker_running -> _canonical_harness_uri -> desired_state.load``
    and the idle daemon held ~0.8 core.  Same shape T4 fixed for top.snapshot.
    """

    many = _maintenance_liveness_document_reads(monkeypatch, tmp_path / "many", ACTORS)
    few = _maintenance_liveness_document_reads(monkeypatch, tmp_path / "few", 7)
    assert many == few, (many, few)
    assert many <= 3, many


class _StreamingHarnesses(_Harnesses):
    """Supervisor seam plus the runtime's streaming-actor view."""

    def __init__(self, rows: tuple[dict[str, object], ...], names: tuple[str, ...]) -> None:
        super().__init__(rows)
        self.names = names

    def streaming_actors(self) -> tuple[str, ...]:
        return self.names


def _runtime_dispatch_document_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, actors: int
) -> int:
    """SQLite desired-state reads for one runtime delivery pass with a snapshot."""

    from hyprial.daemon.impl.runtime import DaemonEventBridge

    app = _application(tmp_path)
    rows = _seed_workers(app, actors)
    harnesses = _StreamingHarnesses(rows, tuple(str(row["name"]) for row in rows))
    app._harnesses = harnesses  # type: ignore[assignment]
    bridge = DaemonEventBridge(
        state_dir=tmp_path,
        node_id=NODE,
        desired_state=app.desired_state,
        transport=None,  # type: ignore[arg-type]
        inbox=app._inbox,
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=app._canonical_harness_uri,
    )
    reads = _patch_counting_read_document(monkeypatch)
    with app._worker_status_snapshot():
        bridge._dispatch_harness_deliveries()
    monkeypatch.undo()
    return reads[0]


def test_t6_runtime_delivery_pass_reads_desired_state_a_constant_number_of_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runtime timer is a batch path: one URI resolution per streaming actor.

    Production 2026-09-25 (pid 54206, after #787): the residual ~0.6 core sat
    in ``_run_scheduled_domains -> runtime.on_timer ->
    _dispatch_harness_deliveries -> _canonical_harness_uri ->
    desired_state.load``.  Inside a snapshot the fallback must reuse the
    snapshot's loaded copy, so the count must not grow with the actor count.
    """

    many = _runtime_dispatch_document_reads(monkeypatch, tmp_path / "many", 24)
    few = _runtime_dispatch_document_reads(monkeypatch, tmp_path / "few", 3)
    assert many == few, (many, few)
    assert many <= 3, many


def test_t6_runtime_timer_runs_inside_a_worker_status_snapshot(tmp_path: Path) -> None:
    """``_run_scheduled_domains`` must hand the runtime tick a snapshot."""

    app = _application(tmp_path)
    seen: list[object] = []

    class _Runtime:
        def on_timer(self, _observed_at_ms: int | None = None) -> object:
            seen.append(app._current_worker_snapshot())
            return None

        def stop(self) -> None:
            pass

    app._runtime = _Runtime()  # type: ignore[assignment]
    # Through the production scheduler, not the helper: a call site that
    # bypasses ``_runtime_timer`` must turn this red.
    app._run_scheduled_domains(0)
    assert len(seen) == 1 and seen[0] is not None
    assert app._current_worker_snapshot() is None


def test_t6_snapshot_reuse_returns_the_same_uris(tmp_path: Path) -> None:
    """Fewer loads must not change the answer ("the same either way").

    Covers the branch where the URI depends on the loaded spec: a transferred
    worker whose ``pinned_owner`` differs from the daemon owner.
    """

    app = _application(tmp_path)
    rows = _seed_workers(app, 4)
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(
            harness="pi",
            name="transferred",
            headless=True,
            command=("/nonexistent/hyprial-card-259-probe",),
            pinned_owner="source-owner",
        )
    )
    names = [str(row["name"]) for row in rows] + ["transferred", "unknown-actor"]
    app._harnesses = _Harnesses(rows)  # type: ignore[assignment]
    without = [app._canonical_harness_uri(name) for name in names]
    with app._worker_status_snapshot():
        within = [app._canonical_harness_uri(name) for name in names]
    assert within == without
    assert without[-2] == f"agent:source-owner:{NODE}:transferred"
    assert without[0] == f"agent:{OWNER}:{NODE}:worker-0"


def test_t7_unsnapshotted_worker_probe_loads_only_the_matching_row(tmp_path: Path) -> None:
    """A bind's liveness probe must not reload desired state per connector.

    Production 2026-10-06: every heartbeat's BindAgentCommand asked
    ``_managed_worker_running`` (no request snapshot on the Agent actor
    thread), which resolved every supervisor row's canonical URI with one
    full desired-state load each -- ~82 ms per bind, 83% of attributed CPU.
    A canonical URI always ends ``:<name>``, so only a same-named row can
    match; its owner comes from the bounded single-harness projection read.
    """

    from hyprial.kernel import canonical_agent_uri

    app = _application(tmp_path)
    rows = _seed_workers(app, count=20)
    app._harnesses = _Harnesses(rows)  # type: ignore[assignment]
    counting = _CountingDesiredState(app.desired_state)
    app.desired_state = counting  # type: ignore[assignment]

    interactive = canonical_agent_uri(OWNER, NODE, "someone-interactive")
    assert app._managed_worker_running(interactive) is None
    assert counting.load_calls == 0

    running = canonical_agent_uri(OWNER, NODE, "worker-0")
    stopped = canonical_agent_uri(OWNER, NODE, "worker-7")
    assert app._managed_worker_running(running) is True
    assert app._managed_worker_running(stopped) is False
    assert app._managed_worker_running("worker-7") is False  # bare name still matches
    assert counting.load_calls == 0
