"""#1779: an accepted request must be served after a daemon restart.

The activation row is durable before the restart -- that is what "accepted"
means -- so a restarted daemon must reconcile it and start the worker once,
instead of waiting for a request that will never come again. Moved out of
test_pac_actor_lifecycle.py, which the structure gate caps at 1000 lines.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation
from hyprial.daemon.impl.pac.actors.daemon import PacActorService
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore

# The graph fixture is defined in the lifecycle module; importing it here keeps
# one definition (pytest reads fixtures from the module namespace).
from tests.daemon.pac.lifecycle.test_pac_actor_lifecycle import (  # noqa: F401
    activation,
    coordinator,
    event_types,
    graph,
)

USER_ALLEN = "user:allen"
AGENT_W = "agent:allen:node:w"


class FakeRuntime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.starts: list[tuple[str, str, str]] = []
        self.stops: list[tuple[str, str, str]] = []

    def observe(self, actor_name: str) -> RuntimeObservation:
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(self, actor_name: str, launch: Any, *, operation_id: str, identity_marker: str) -> RuntimeObservation:
        self.starts.append((actor_name, operation_id, identity_marker))
        observed = RuntimeObservation(True, identity_marker, launch.harness, operation_id)
        self.running[actor_name] = observed
        return observed

    def stop(self, actor_name: str, launch: Any, *, operation_id: str, identity_marker: str) -> RuntimeObservation:
        self.stops.append((actor_name, operation_id, identity_marker))
        self.running.pop(actor_name, None)
        return RuntimeObservation(False)


def _pend_worker(store: PacGraphStore, graph_id: str, *, node_id: str = "worker") -> None:
    """Commit an in-transition activation so the fallback sweep supplies it."""

    db = store.write()
    try:
        db.execute(
            "INSERT OR IGNORE INTO actor_activations "
            "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,"
            "identity_marker,updated_at) "
            "VALUES (?,?,0,'up','pending',?,?,?,0)",
            (graph_id, node_id, f"effect:{node_id}", f"operation:{node_id}",
             f"pac:{graph_id}:{node_id}:1:{node_id}"),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise





def _request_workflow_node(
    store: PacGraphStore,
    graph_id: str,
    *,
    state: str = "requested",
    request_id: str | None = "workflow-request:t1",
    node_id: str = "worker",
) -> None:
    """Record the request account entry the relaunch judgement reads.

    The judgement is the PAC request account (``workflow_nodes``), not the
    inbox: only ``state='requested'`` with a recorded requestId means work is
    still owed.  ``done``/``failed``/``blocked``/``cancelled`` are the
    completion or failure marks and must never trigger a relaunch.
    """

    with store.write() as db:
        db.execute(
            "INSERT INTO workflow_nodes"
            "(graph_id,node_id,actor_node,state,request_id,input_token,generation,"
            "deadline_ms,reason_ref) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                graph_id,
                node_id,
                node_id,
                state,
                request_id,
                "input-token",
                1,
                10**13,
                None if state == "requested" else f"reason:{state}",
            ),
        )


def test_restart_relaunches_a_worker_with_an_accepted_request(
    graph,  # noqa: F811 - the imported fixture, not a redefinition
    tmp_path: Path,
) -> None:
    """#1779: an accepted request must be served after a daemon restart.

    The activation row is already durable before the restart (that is what
    "accepted" means), so the restarted daemon must reconcile it and start the
    worker exactly once -- not wait for a new request that never comes.
    """

    store, graph_id = graph
    launch = tmp_path / "launch" / "w.yaml"
    launch.parent.mkdir()
    launch.write_text("harness: codex\nargs: [--headless]\n")
    PacReactor(store).set_flag(graph_id, "a", actor=USER_ALLEN)
    _pend_worker(store, graph_id)
    _request_workflow_node(store, graph_id)

    def new_service(
        runtime: FakeRuntime, epoch: str
    ) -> tuple[PacActorService, PacGraphAuthority]:
        authority = PacGraphAuthority(store.path, NullSender())
        service = PacActorService(
            state_dir=store.path.parent,
            reference_root=tmp_path,
            runtime=runtime,
            sender=NullSender(),
            daemon_epoch=epoch,
            logger=lambda *_args, **_kwargs: None,
            workers=1,
            graph_authority=authority,
        )
        return service, authority

    first_runtime = FakeRuntime()
    first, first_authority = new_service(first_runtime, "epoch-1")
    first.submit_tick()
    deadline = time.monotonic() + 3.0
    while not first_runtime.starts:
        assert time.monotonic() < deadline, "first generation never started the worker"
        time.sleep(0.01)

    restarted_runtime = FakeRuntime()
    restarted, restarted_authority = new_service(restarted_runtime, "epoch-2")
    try:
        restarted.submit_tick()
        deadline = time.monotonic() + 3.0
        while not restarted_runtime.starts:
            assert time.monotonic() < deadline, (
                "accepted request was not re-served after the restart: "
                f"starts={restarted_runtime.starts}"
            )
            time.sleep(0.01)
        restarted.submit_tick()
        time.sleep(0.2)
        assert len(restarted_runtime.starts) == 1, restarted_runtime.starts
    finally:
        # The first generation is discarded the way a crash discards it: no
        # graceful close, so no cleanup intent is recorded for its worker.
        assert restarted.close(3.0)
        assert restarted_authority.close(3.0)
        assert first.close(3.0)
        assert first_authority.close(3.0)


def test_a_finished_request_is_not_relaunched_after_a_restart(graph) -> None:  # noqa: F811
    """Control for #1779: the completion mark must keep the lost contract.

    The `[restart]` contract (`test_lost_current_runtime_is_terminal_unflagged_
    and_not_relaunched`) must not move: when the request account has already
    marked the node ``done``, a restarted daemon reports the loss once, resets
    the flag and drives the actor down -- it never starts a second worker.
    """

    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    coordinator(store, runtime, "epoch-1").reconcile_all()
    assert store.node(gid, "worker").flag is True
    _request_workflow_node(store, gid, state="done")

    runtime.running.clear()
    sender = NullSender()
    observer = coordinator(store, runtime, "epoch-2", sender)
    observer.reconcile_all()
    observer.reconcile_all()

    assert len(runtime.starts) == 1, (
        "a finished request must never be relaunched: " f"{runtime.starts}"
    )
    assert event_types(store, gid).count("actor_lost") == 1
    assert event_types(store, gid).count("flag_reset") == 1
    assert store.node(gid, "worker").flag is False
    assert activation(store, gid)["desired"] == "down"
