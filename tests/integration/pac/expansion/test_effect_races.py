"""Prepared child worktrees are replayable and clean removal preserves refs."""

from pathlib import Path
import subprocess
import threading
import time

import pytest

from hyprial.daemon.impl.pac.actors.worktrees import (
    cleanup_worktree,
    prepare_worktree,
)
from hyprial.daemon.impl.pac.graphs.authority.expansion import prepare_expansion
import hyprial.daemon.impl.pac.graphs.authority as authority_module
from hyprial.daemon.impl.pac.graphs.authority import (
    PacGraphAuthority,
    _ActorEffect,
    _ActorEffectCompleted,
)
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime.types import close_workflow

from .support import PLANNER, child_document, parent_graph


def test_prepared_worktree_replay_and_cleanup_preserve_branch(tmp_path):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    database = store.path
    store.close()
    context = prepare_expansion(
        database,
        home=home,
        raw=child_document(1),
        graph_id=graph_id,
        node_id="planner",
        request_id="request-1",
        actor=PLANNER,
        at=200,
        machine="machine",
        local_owner="test",
    )
    plan = dict(context.worktrees[0])
    assert prepare_worktree(plan, timeout_s=10)["state"] == "prepared"
    assert prepare_worktree(plan, timeout_s=10)["state"] == "prepared"
    path = Path(plan["path"])
    assert path.is_dir()
    result = cleanup_worktree(plan, timeout_s=10)
    assert result["state"] == "removed"
    assert not path.exists()
    branch = subprocess.run(
        ["git", "-C", plan["repo"], "show-ref", "--verify", f"refs/heads/{plan['branch']}"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert branch.returncode == 0


def test_authority_prepares_worktree_before_runtime_start(tmp_path):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    database = store.path
    store.close()
    raw = child_document(1)

    class Runtime:
        def __init__(self):
            self.running = None

        def observe(self, _actor_name):
            return self.running or RuntimeObservation(False)

        def start(self, actor_name, launch, *, operation_id, identity_marker):
            assert Path(launch.cwd).is_dir()
            self.running = RuntimeObservation(
                True, identity_marker, launch.harness, operation_id, "entity"
            )
            return self.running

        def stop(self, _actor_name, _launch, *, operation_id, identity_marker):
            self.running = None
            return RuntimeObservation(False, identity_marker, operation_id=operation_id)

    runtime = Runtime()
    authority = PacGraphAuthority(database, NullSender())
    try:
        context = authority.prepare_expansion(
            database=database,
            home=home,
            raw=raw,
            graph_id=graph_id,
            node_id="planner",
            request_id="request-1",
            actor=PLANNER,
            at=200,
            machine="machine",
            local_owner="test",
        )
        authority.attach_reconciler(runtime, home, "epoch")
        authority.set_flag(
            graph_id,
            "planner",
            actor=PLANNER,
            reason_ref="evidence:prepare-before-start",
            expected_request="request-1",
            expansion=raw,
            expansion_context=context,
        )
        actor_node = context.worktrees[0]["actorNode"]
        authority.reconcile_actor(context.child_graph_id, actor_node)
        deadline = time.monotonic() + 5
        while runtime.running is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        state = None
        while state != "prepared":
            assert time.monotonic() < deadline
            read = PacGraphStore(database, read_only=True)
            try:
                state = read._db.execute(
                    "SELECT state FROM workflow_worktrees WHERE graph_id=?",
                    (context.child_graph_id,),
                ).fetchone()[0]
            finally:
                read.close()
            if state != "prepared":
                time.sleep(0.01)
    finally:
        assert authority.close()


def test_observe_failure_preserves_prepared_worktree_for_next_start(tmp_path):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    database = store.path
    store.close()
    raw = child_document(1)
    context = prepare_expansion(
        database,
        home=home,
        raw=raw,
        graph_id=graph_id,
        node_id="planner",
        request_id="request-1",
        actor=PLANNER,
        at=200,
        machine="machine",
        local_owner="test",
    )
    plan = dict(context.worktrees[0])
    assert prepare_worktree(plan, timeout_s=10)["state"] == "prepared"
    observe_called = threading.Event()

    class Runtime:
        def __init__(self):
            self.observe_error: Exception | None = RuntimeError("observe unavailable")
            self.running = None

        def observe(self, _actor_name):
            observe_called.set()
            if self.observe_error is not None:
                raise self.observe_error
            return self.running or RuntimeObservation(False)

        def start(self, _actor_name, launch, *, operation_id, identity_marker):
            assert Path(launch.cwd) == Path(plan["path"])
            self.running = RuntimeObservation(
                True, identity_marker, launch.harness, operation_id, "entity"
            )
            return self.running

        def stop(self, _actor_name, _launch, *, operation_id, identity_marker):
            self.running = None
            return RuntimeObservation(False, identity_marker, operation_id=operation_id)

    runtime = Runtime()
    authority = PacGraphAuthority(database, NullSender())
    try:
        authority.set_flag(
            graph_id,
            "planner",
            actor=PLANNER,
            reason_ref="evidence:prepared-observe-failure",
            expected_request="request-1",
            expansion=raw,
            expansion_context=context,
        )
        write = PacGraphStore(database)
        try:
            with write.write():
                write._db.execute(
                    "UPDATE workflow_worktrees SET state='prepared' "
                    "WHERE graph_id=? AND actor_node=?",
                    (context.child_graph_id, plan["actorNode"]),
                )
        finally:
            write.close()
        authority.attach_reconciler(runtime, home, "epoch")

        authority.reconcile_actor(context.child_graph_id, plan["actorNode"])
        deadline = time.monotonic() + 3
        assert observe_called.wait(1.0)
        while authority.actor_effect_stats()["inFlight"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        read = PacGraphStore(database, read_only=True)
        try:
            state = read._db.execute(
                "SELECT state FROM workflow_worktrees WHERE graph_id=?",
                (context.child_graph_id,),
            ).fetchone()[0]
        finally:
            read.close()
        assert state == "prepared"

        runtime.observe_error = None
        authority.reconcile_actor(context.child_graph_id, plan["actorNode"])
        while runtime.running is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        runtime.observe_error = None
        assert authority.close()


def test_close_during_start_keeps_worktree_cleanup_pending(
    tmp_path, monkeypatch
):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    database = store.path
    store.close()
    raw = child_document(1)
    context = prepare_expansion(
        database,
        home=home,
        raw=raw,
        graph_id=graph_id,
        node_id="planner",
        request_id="request-1",
        actor=PLANNER,
        at=200,
        machine="machine",
        local_owner="test",
    )
    authority = PacGraphAuthority(database, NullSender())
    processed = threading.Event()
    original_completed = authority_module._Generation._actor_effect_completed

    def observed_completed(writer, completion):
        original_completed(writer, completion)
        processed.set()

    monkeypatch.setattr(
        authority_module._Generation,
        "_actor_effect_completed",
        observed_completed,
    )
    try:
        authority.set_flag(
            graph_id,
            "planner",
            actor=PLANNER,
            reason_ref="evidence:close-during-start",
            expected_request="request-1",
            expansion=raw,
            expansion_context=context,
        )
        write = PacGraphStore(database)
        try:
            with write.write():
                child = write.graph(context.child_graph_id)
                assert child is not None
                close_workflow(
                    write,
                    child,
                    state="cancelled",
                    reason="pac:parent-closed",
                    at=300,
                )
        finally:
            write.close()

        generation = authority._runtime.snapshot(authority._handle).generation
        plan = context.worktrees[0]
        effect = _ActorEffect(
            generation,
            context.child_graph_id,
            plan["actorNode"],
            "start",
            "late-worker",
        )
        completion = _ActorEffectCompleted(
            generation,
            effect,
            worktree_result={"state": "prepared"},
        )
        assert authority._runtime.tell(authority._handle, completion).value == "accepted"
        assert processed.wait(1.0)
        read = PacGraphStore(database, read_only=True)
        try:
            row = read._db.execute(
                "SELECT state FROM workflow_worktrees WHERE graph_id=?",
                (context.child_graph_id,),
            ).fetchone()
            assert row["state"] == "cleanup_pending"
        finally:
            read.close()
    finally:
        assert authority.close()


def test_preflight_git_budget_finishes_before_caller_timeout(tmp_path, monkeypatch):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    database = store.path
    store.close()
    observed: list[float] = []

    def bounded_failure(_repo, _base, *, timeout_s):
        observed.append(timeout_s)
        time.sleep(timeout_s)
        raise RuntimeError("bounded git failure")

    monkeypatch.setattr(
        "hyprial.daemon.impl.pac.graphs.authority.expansion.resolve_base",
        bounded_failure,
    )
    authority = PacGraphAuthority(database, NullSender(), call_timeout=0.25)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="bounded git failure"):
            authority.prepare_expansion(
                database=database,
                home=home,
                raw=child_document(1),
                graph_id=graph_id,
                node_id="planner",
                request_id="request-1",
                actor=PLANNER,
                at=200,
                machine="machine",
                local_owner="test",
            )
        elapsed = time.monotonic() - started
        assert observed and 0 < observed[0] < 0.25
        assert elapsed < 0.25
    finally:
        assert authority.close()
