"""Isolated mailbox mechanism controls; no actor process is attached or started."""

from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

import hyprial.daemon.impl.pac.graphs.authority as module
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority, PacGraphOverloaded, ReconcileActor
from hyprial.daemon.impl.pac.graphs.edits import add_node, create_graph
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import PacGraphStore

OWNER = "user:probe-owner"
CAPACITY = 4


def wait_for(predicate):
    deadline = time.monotonic() + 3.0
    while not predicate():
        assert time.monotonic() < deadline
        threading.Event().wait(0.005)


def make_authority(tmp_path):
    database = tmp_path / "graph.sqlite3"
    store = PacGraphStore(database)
    try:
        graph = create_graph(store, name="admission-probe", created_by=OWNER)["graphId"]
        version = 1
        for n in range(CAPACITY + 1):
            version = add_node(
                store,
                tmp_path,
                graph_id=graph,
                node_id=f"_actor.{n}",
                owner=OWNER,
                brief_ref=f"brief:{n}",
                kind="actor",
                actor_name=f"probe-{n}",
                launch_ref=f"fixture:{n}",
                expect_version=version,
            )["version"]
    finally:
        store.close()
    return PacGraphAuthority(
        database,
        NullSender(),
        mailbox_capacity=CAPACITY,
        completion_capacity=16,
        call_timeout=3.0,
    ), graph


def invoke(target, results, errors):
    try:
        results.append(target())
    except BaseException as error:
        errors.append(error)


def writer_gate(authority, graph, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = module._Generation._write
    results, errors = [], []

    def blocked(generation, command):
        if isinstance(command, module.ClockTick) and not entered.is_set():
            entered.set()
            assert release.wait(5.0)
        return original(generation, command)

    monkeypatch.setattr(module._Generation, "_write", blocked)
    caller = threading.Thread(
        target=invoke, args=(lambda: authority.clock_tick(graph), results, errors)
    )
    caller.start()
    assert entered.wait(2.0)
    assert authority._runtime.snapshot(authority._handle).in_flight == 1
    return release, caller, results, errors


def record_wakes(monkeypatch):
    handled = []
    original = module._Generation._reconcile_actor

    def observed(generation, command):
        if command.node_id != "_controlled_crash":
            handled.append((generation.generation, command.graph_id, command.node_id))
        return original(generation, command)

    monkeypatch.setattr(module._Generation, "_reconcile_actor", observed)
    return handled


def test_queued_duplicates_share_one_slot_and_unrelated_command_commits(
    tmp_path, monkeypatch
):
    authority, graph = make_authority(tmp_path)
    handled = record_wakes(monkeypatch)
    release, caller, results, errors = writer_gate(authority, graph, monkeypatch)
    mutation_results, mutation_errors = [], []
    mutation = threading.Thread(
        target=invoke,
        args=(
            lambda: authority.close_graph(graph, actor=OWNER),
            mutation_results,
            mutation_errors,
        ),
    )
    try:
        for _ in range(CAPACITY + 1):
            assert (
                authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
            )
        assert authority._runtime.snapshot(authority._handle).queued == 1
        assert len(authority._reconcile_claims) == 1
        mutation.start()
        wait_for(lambda: authority._runtime.snapshot(authority._handle).queued == 2)
        assert not handled and not mutation_results
        release.set()
        caller.join(3.0)
        mutation.join(3.0)
        assert not caller.is_alive() and not mutation.is_alive()
        assert len(results) == 1 and not errors
        assert len(mutation_results) == 1 and not mutation_errors
        assert [row[2] for row in handled] == ["_actor.0"]
        assert not authority._reconcile_claims
        store = PacGraphStore(authority.database, read_only=True)
        try:
            assert store.graph(graph)["closed_at"] is not None
        finally:
            store.close()
    finally:
        release.set()
        caller.join(3.0)
        if mutation.ident is not None:
            mutation.join(3.0)
        assert authority.close(3.0)


def test_distinct_wakes_keep_real_capacity_backpressure(tmp_path, monkeypatch):
    authority, graph = make_authority(tmp_path)
    handled = record_wakes(monkeypatch)
    release, caller, results, errors = writer_gate(authority, graph, monkeypatch)
    try:
        for n in range(CAPACITY):
            assert (
                authority.reconcile_actor(graph, f"_actor.{n}")
                is AdmissionResult.ACCEPTED
            )
        assert authority._runtime.snapshot(authority._handle).queued == CAPACITY
        assert len(authority._reconcile_claims) == CAPACITY
        assert (
            authority.reconcile_actor(graph, f"_actor.{CAPACITY}")
            is AdmissionResult.OVERLOADED
        )
        with pytest.raises(PacGraphOverloaded, match="admission: overloaded"):
            authority.close_graph(graph, actor=OWNER)
        # The already-admitted duplicate adds no work even at true capacity.
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
        release.set()
        caller.join(3.0)
        assert not caller.is_alive() and len(results) == 1 and not errors
        wait_for(lambda: len(handled) == CAPACITY)
        assert Counter(row[2] for row in handled) == Counter(
            f"_actor.{n}" for n in range(CAPACITY)
        )
        assert not authority._reconcile_claims
        assert (
            authority.reconcile_actor(graph, f"_actor.{CAPACITY}")
            is AdmissionResult.ACCEPTED
        )
        wait_for(lambda: len(handled) == CAPACITY + 1)
        assert authority.close_graph(graph, actor=OWNER)["ok"] is True
    finally:
        release.set()
        caller.join(3.0)
        assert authority.close(3.0)


def test_typed_commands_are_not_coalesced_and_refused_wake_retires_claim(
    tmp_path, monkeypatch
):
    authority, graph = make_authority(tmp_path)
    release, caller, results, errors = writer_gate(authority, graph, monkeypatch)
    typed_results, typed_errors = [], []
    typed = [
        threading.Thread(
            target=invoke,
            args=(lambda: authority.clock_tick(graph), typed_results, typed_errors),
        )
        for _ in range(CAPACITY)
    ]
    try:
        for thread in typed:
            thread.start()
        wait_for(
            lambda: authority._runtime.snapshot(authority._handle).queued == CAPACITY
        )
        assert not authority._reconcile_claims
        assert (
            authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.OVERLOADED
        )
        assert not authority._reconcile_claims
        release.set()
        caller.join(3.0)
        for thread in typed:
            thread.join(3.0)
            assert not thread.is_alive()
        assert len(typed_results) == CAPACITY and not typed_errors
        assert len(results) == 1 and not errors
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
    finally:
        release.set()
        caller.join(3.0)
        for thread in typed:
            if thread.ident is not None:
                thread.join(3.0)
        assert authority.close(3.0)


@pytest.mark.parametrize("outcome", ["overloaded", "closed", "raise"])
def test_provisional_duplicate_never_gets_false_acceptance(
    tmp_path, monkeypatch, outcome
):
    authority, graph = make_authority(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_tell = authority._runtime.tell
    results, errors = [], []
    handled = record_wakes(monkeypatch)

    def refused_tell(handle, command):
        if isinstance(command, ReconcileActor):
            entered.set()
            assert release.wait(5.0)
            if outcome == "raise":
                raise RuntimeError("controlled tell failure")
            return AdmissionResult(outcome)
        return original_tell(handle, command)

    monkeypatch.setattr(authority._runtime, "tell", refused_tell)
    caller = threading.Thread(
        target=invoke,
        args=(lambda: authority.reconcile_actor(graph, "_actor.0"), results, errors),
    )
    caller.start()
    try:
        assert entered.wait(2.0)
        claim = authority._reconcile_claims[(graph, "_actor.0")]
        assert not claim.admitted
        assert (
            authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.OVERLOADED
        )
        assert not handled
        release.set()
        caller.join(3.0)
        assert not caller.is_alive()
        if outcome == "raise":
            assert not results and len(errors) == 1
            assert isinstance(errors[0], RuntimeError)
        else:
            assert results == [AdmissionResult(outcome)] and not errors
        assert not authority._reconcile_claims
        monkeypatch.setattr(authority._runtime, "tell", original_tell)
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
        wait_for(lambda: len(handled) == 1)
    finally:
        release.set()
        caller.join(3.0)
        assert authority.close(3.0)


def test_synchronous_undelivered_callback_runs_outside_owner_lock(
    tmp_path, monkeypatch
):
    authority, graph = make_authority(tmp_path)
    original_tell = authority._runtime.tell

    def rejected_tell(handle, command):
        if isinstance(command, ReconcileActor):
            # Fail without hanging if a future implementation holds this lock.
            unlocked = authority._lock.acquire(blocking=False)
            assert unlocked, (
                "tell was called while the non-reentrant owner lock was held"
            )
            authority._lock.release()
            authority._undelivered(command, "CONTROLLED_UNDELIVERED")
            return AdmissionResult.CLOSED
        return original_tell(handle, command)

    monkeypatch.setattr(authority._runtime, "tell", rejected_tell)
    try:
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.CLOSED
        assert not authority._reconcile_claims
    finally:
        assert authority.close(3.0)


def test_dequeue_before_tell_return_cannot_revive_old_claim_or_swallow_followup(
    tmp_path, monkeypatch
):
    authority, graph = make_authority(tmp_path)
    entered, release_handler, release_tell = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_tell = authority._runtime.tell
    original_reconcile = module._Generation._reconcile_actor
    first_commands, handled, results, errors = [], [], [], []

    def hold_first_handler(generation, command):
        handled.append(command)
        if len(handled) == 1:
            entered.set()
            assert release_handler.wait(5.0)
        return original_reconcile(generation, command)

    def delayed_tell_return(handle, command):
        first = isinstance(command, ReconcileActor) and not first_commands
        if first:
            first_commands.append(command)
        result = original_tell(handle, command)
        if first:
            assert release_tell.wait(5.0)
        return result

    monkeypatch.setattr(module._Generation, "_reconcile_actor", hold_first_handler)
    monkeypatch.setattr(authority._runtime, "tell", delayed_tell_return)
    caller = threading.Thread(
        target=invoke,
        args=(lambda: authority.reconcile_actor(graph, "_actor.0"), results, errors),
    )
    caller.start()
    try:
        assert entered.wait(2.0)
        assert not authority._reconcile_claims  # Released before state observation.
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
        successor = authority._reconcile_claims[(graph, "_actor.0")]
        for _ in range(CAPACITY):
            assert (
                authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
            )
        assert authority._runtime.snapshot(authority._handle).queued == 1
        release_tell.set()
        caller.join(3.0)
        assert results == [AdmissionResult.ACCEPTED] and not errors
        assert authority._reconcile_claims[(graph, "_actor.0")] is successor
        authority._undelivered(first_commands[0], "LATE_OLD_RECEIPT")
        assert authority._reconcile_claims[(graph, "_actor.0")] is successor
        release_handler.set()
        wait_for(lambda: len(handled) == 2)
        assert not authority._reconcile_claims
    finally:
        release_tell.set()
        release_handler.set()
        caller.join(3.0)
        assert authority.close(3.0)


def test_close_rejects_new_wakes_and_does_not_claim_drain_while_writer_is_held(
    tmp_path, monkeypatch
):
    authority, graph = make_authority(tmp_path)
    handled = record_wakes(monkeypatch)
    release, caller, results, errors = writer_gate(authority, graph, monkeypatch)
    try:
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
        assert authority.close(0.01) is False
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.CLOSED
        assert handled == []
        release.set()
        caller.join(3.0)
        assert not caller.is_alive() and len(results) == 1 and not errors
        assert authority.close(3.0) is True
        assert [row[2] for row in handled] == ["_actor.0"]
        assert not authority._reconcile_claims
    finally:
        release.set()
        caller.join(3.0)
        assert authority.close(3.0)


def test_close_remains_incomplete_until_provisional_admission_is_resolved(
    tmp_path, monkeypatch
):
    authority, graph = make_authority(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_tell = authority._runtime.tell
    results, errors = [], []

    def held_tell(handle, command):
        if isinstance(command, ReconcileActor):
            entered.set()
            assert release.wait(5.0)
        return original_tell(handle, command)

    monkeypatch.setattr(authority._runtime, "tell", held_tell)
    caller = threading.Thread(
        target=invoke,
        args=(lambda: authority.reconcile_actor(graph, "_actor.0"), results, errors),
    )
    caller.start()
    try:
        assert entered.wait(2.0)
        assert authority.close(1.0) is False
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.CLOSED
        release.set()
        caller.join(3.0)
        assert results == [AdmissionResult.CLOSED] and not errors
        assert not authority._reconcile_claims
        assert authority.close(3.0) is True
    finally:
        release.set()
        caller.join(3.0)
        assert authority.close(3.0)


def test_restart_retires_old_wake_claim_and_old_receipt_cannot_remove_successor(
    tmp_path, monkeypatch
):
    authority, graph = make_authority(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = module._Generation.__call__
    handled = record_wakes(monkeypatch)
    calls, failures = [], []

    def crash_after_dequeue(generation, command):
        result = original(generation, command)
        if (
            isinstance(command, ReconcileActor)
            and command.node_id == "_controlled_crash"
        ):
            # The real handler entry retired its claim before this fault.
            entered.set()
            assert release.wait(5.0)
            raise RuntimeError("controlled generation retirement")
        return result

    monkeypatch.setattr(module._Generation, "__call__", crash_after_dequeue)
    writer = threading.Thread(
        target=invoke,
        args=(lambda: authority.close_graph(graph, actor=OWNER), calls, failures),
    )
    next_gate = None
    try:
        before = authority._runtime.snapshot(authority._handle).generation
        assert (
            authority.reconcile_actor(graph, "_controlled_crash")
            is AdmissionResult.ACCEPTED
        )
        assert entered.wait(2.0)
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
        old_command = authority._reconcile_claims[(graph, "_actor.0")].command
        writer.start()
        wait_for(lambda: authority._runtime.snapshot(authority._handle).queued == 2)
        release.set()
        writer.join(3.0)
        assert not writer.is_alive() and not calls and len(failures) == 1
        assert isinstance(failures[0], PacGraphOverloaded)
        assert "ACTOR_RESTARTED_UNDELIVERED" in str(failures[0])
        wait_for(
            lambda: (
                authority._runtime.snapshot(authority._handle).generation > before
                and authority._runtime.snapshot(authority._handle).state.value
                == "running"
            )
        )
        assert not handled and not authority._reconcile_claims
        next_gate = writer_gate(authority, graph, monkeypatch)
        assert authority.reconcile_actor(graph, "_actor.0") is AdmissionResult.ACCEPTED
        successor = authority._reconcile_claims[(graph, "_actor.0")]
        assert successor.command is not old_command
        authority._undelivered(old_command, "ACTOR_RESTARTED_UNDELIVERED")
        assert authority._reconcile_claims[(graph, "_actor.0")] is successor
        next_gate[0].set()
        next_gate[1].join(3.0)
        wait_for(lambda: len(handled) == 1)
        assert handled[0][0] > before
        assert not authority._reconcile_claims
        assert authority.close_graph(graph, actor=OWNER)["ok"] is True
    finally:
        release.set()
        if writer.ident is not None:
            writer.join(3.0)
        if next_gate is not None:
            next_gate[0].set()
            next_gate[1].join(3.0)
        assert authority.close(3.0)
