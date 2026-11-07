"""Real daemon owners and SQLite; process/network edges are controlled fakes.

These are composition tests, not daemon subprocess crash acceptance.
"""

from dataclasses import replace
import threading

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.composition import (
    AgentIdentityProjectionView, CorrelatedDomainEvents, HarnessPortClient, DomainCommandError,
)
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.transfer.p2.actor import Command, Operation
from hyprial.daemon.impl.transfer.p2.model import Binding, Role
from hyprial.daemon.impl.transfer.p2.source.effect import QuiesceRequest
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessMutationCompleted
from hyprial.daemon.impl.harnesses.runtime.transfer_ports import StartsHeld
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _Inbox, _LocalDelivery, _Presence, _Transport, _isolated_application,
    _close_application,
)


class Process:
    pid = None

    def __init__(self):
        self.running = True
        self.stopping = threading.Event()
        self.release = threading.Event()
        self.stops = 0

    def stop(self):
        self.stops += 1
        self.stopping.set()
        assert self.release.wait(5), "test did not release process stop"
        self.running = False


class Launcher:
    def __init__(self):
        self.processes = []

    def start(self, _spec):
        process = Process()
        self.processes.append(process)
        return process


def test_quiesce_uses_real_owners_and_keeps_identity_while_stop_is_pending(tmp_path):
    app = _isolated_application(state_dir=tmp_path / "state",
                               socket_path=tmp_path / "daemon.sock",
                               node_id="node", owner="alice", network_isolated=True)
    launcher = Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state,
                               agent_identity=AgentIdentityProjectionView(app._agent_session_domains),
                               restart_backoff_seconds=0)
    harnesses = HarnessPortClient(actor, events)
    app._harnesses, app._inbox, app._presence = harnesses, _Inbox(), _Presence()
    app._start_lifecycle_manager(transport=_Transport(), inbox=app._inbox,
                                local_delivery=_LocalDelivery(), harnesses=harnesses)
    try:
        spec = HarnessLaunchSpec("codex", "worker", True)
        app.handle("lifecycle.start", spec.to_json())
        original = app.agents.require("worker")
        process = launcher.processes[0]
        binding = Binding(Role.SOURCE, "alice", "node", "worker", original.entity_token,
                          "target-node", "target", "a" * 64)
        app._transfer_p2.submit(Command(Operation.PREPARE, "move", binding)).result(5)
        request = QuiesceRequest("move", "worker", replace(
            app._lifecycle_spec(spec), expected_entity_token=original.entity_token))
        pending = app._quiesce_p2_source(request)
        try:
            assert process.stopping.wait(5), "quiesce never reached the real harness owner"
            assert not pending.done()
            fact = app._transfer_source_state("worker", original.entity_token)
            assert fact.state != "clear"
            with pytest.raises(DaemonRequestError) as rejected:
                app.handle("lifecycle.start", spec.to_json())
            # Until the stop receipt is durable the journal reports unknown,
            # while the harness owner's acknowledged start hold is active.
            assert rejected.value.code == "TRANSFER_CUSTODY_UNAVAILABLE"
            assert len(launcher.processes) == 1
        finally:
            process.release.set()
        result = pending.result(5)
        assert result.state == "quiesced", result
        assert not process.running
        assert app.agents.require("worker").entity_token == original.entity_token
        assert app._transfer_source_state("worker", original.entity_token).state == "held"
        receipt = app._lifecycle_manager.wait(result.receipt, 5)
        assert receipt.state.value == "completed"
        assert app._quiesce_p2_source(request).result(5).state == "quiesced"
        assert process.stops == 1
    finally:
        for process in launcher.processes:
            process.release.set()
        _close_application(app)


@pytest.mark.parametrize("completed", [False, True])
def test_restore_reconstructs_owner_hold_from_journal_before_start(tmp_path, completed):
    app = _isolated_application(state_dir=tmp_path / "state",
                               socket_path=tmp_path / "daemon.sock",
                               node_id="node", owner="alice", network_isolated=True)
    launcher = Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state,
                               agent_identity=AgentIdentityProjectionView(app._agent_session_domains),
                               restart_backoff_seconds=0)
    harnesses = HarnessPortClient(actor, events)
    app._harnesses = harnesses
    try:
        original = app.agents.create("worker")
        spec = HarnessLaunchSpec("codex", "worker", True)
        app.desired_state.upsert_harness(spec)
        binding = Binding(Role.SOURCE, "alice", "node", "worker", original.entity_token,
                          "target-node", "target", "a" * 64)
        store = app._transfer_p2._store
        store.prepare("move", binding)
        intent = store.journal.begin("move", step="quiesce", fingerprint="fp", actor="worker")
        if completed:
            store.journal.finish("move", expected=intent.state, receipt="arranged-stop",
                                 entity_token=original.entity_token)
        # Fresh owner has no volatile hold. Boot must rebuild it from durable
        # custody, not only omit this spec from its own candidate batch.
        app._restore_harnesses()
        with pytest.raises(DomainCommandError) as rejected:
            harnesses.call(EnsureHarnessCommand("direct-start", spec), HarnessMutationCompleted)
        assert getattr(rejected.value, "code", None) == "TRANSFER_STARTS_HELD"
        assert not launcher.processes
        assert app.agents.require("worker").entity_token == original.entity_token
    finally:
        for process in launcher.processes:
            process.release.set()
        _close_application(app)


# ---------------------------------------------------------------------------
# Pending quiesce recovery: a durable intent is closed only from owner receipts.
#
# Real daemon owners and SQLite; process/network edges are controlled fakes.
# These are composition tests, not daemon subprocess crash acceptance.
# ---------------------------------------------------------------------------
def _arrange_pending(tmp_path):
    """Crash window: durable quiesce intent with no journal result, no hold."""

    app = _isolated_application(state_dir=tmp_path / "state",
                                socket_path=tmp_path / "daemon.sock",
                                node_id="node", owner="alice", network_isolated=True)
    launcher = Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state,
                                agent_identity=AgentIdentityProjectionView(
                                    app._agent_session_domains),
                                restart_backoff_seconds=0)
    harnesses = HarnessPortClient(actor, events)
    app._harnesses, app._inbox, app._presence = harnesses, _Inbox(), _Presence()
    app._start_lifecycle_manager(transport=_Transport(), inbox=app._inbox,
                                 local_delivery=_LocalDelivery(), harnesses=harnesses)
    spec = HarnessLaunchSpec("codex", "worker", True)
    app.handle("lifecycle.start", spec.to_json())
    original = app.agents.require("worker")
    process = launcher.processes[0]
    token = original.entity_token
    binding = Binding(Role.SOURCE, "alice", "node", "worker", token,
                      "target-node", "target", "a" * 64)
    app._transfer_p2.submit(Command(Operation.PREPARE, "move", binding)).result(5)
    app._transfer_p2._store.journal.begin("move", step="quiesce",
                                          fingerprint="fp", actor="worker")
    stored = next(s for s in app.desired_state.load().harnesses if s.name == "worker")
    fenced = replace(app._lifecycle_spec(stored), expected_entity_token=token)
    return app, launcher, harnesses, process, fenced, token


def test_pending_quiesce_is_closed_from_the_completed_owner_receipt(tmp_path):
    app, launcher, harnesses, process, fenced, token = _arrange_pending(tmp_path)
    try:
        operation_id = "transfer-quiesce:move:1"
        admitted = app._lifecycle_manager.submit(
            LifecycleOperation.deactivate(operation_id, fenced))
        assert admitted.value == "accepted"
        process.release.set()
        assert app._lifecycle_manager.wait(operation_id, 5).state.value == "completed"
        assert not process.running
        row = app._transfer_p2._store.journal.get("move")
        assert row.phase == "quiesce-pending" and row.receipt == ""

        app._reconcile_transfer_quiesce()

        closed = app._transfer_p2._store.journal.get("move")
        assert (closed.phase, closed.receipt) == ("quiesced", operation_id)
        assert app._transfer_source_state("worker", token).state == "held"
        # The re-derived hold is the real owner fence, not a caller read.
        with pytest.raises(Exception) as rejected:
            harnesses.call(EnsureHarnessCommand(
                "direct-start", HarnessLaunchSpec("codex", "worker", True)),
                HarnessMutationCompleted)
        assert getattr(rejected.value, "code", None) == "TRANSFER_STARTS_HELD"
        assert len(launcher.processes) == 1
        # Re-running is idempotent: the row is no longer pending.
        app._reconcile_transfer_quiesce()
        assert app._transfer_p2._store.journal.get("move").phase == "quiesced"
    finally:
        process.release.set()
        _close_application(app)


def test_pending_quiesce_stays_unknown_without_a_completed_stop_receipt(tmp_path):
    app, launcher, harnesses, process, fenced, token = _arrange_pending(tmp_path)
    try:
        # No lifecycle receipt at all: the accepted effect is not certified.
        app._reconcile_transfer_quiesce()
        row = app._transfer_p2._store.journal.get("move")
        assert row.phase == "quiesce-pending" and row.receipt == ""
        assert app._transfer_source_state("worker", token).blocks_start
        assert process.running
    finally:
        process.release.set()
        _close_application(app)


class _MismatchedReceipt:
    """Owner edge double: a COMPLETED result that names another operation."""

    def __init__(self, inner, reported):
        self._inner = inner
        self._reported = reported

    def wait(self, operation_id, timeout):
        result = self._inner.wait(operation_id, timeout)
        return replace(result, operation_id=self._reported)


def test_pending_quiesce_rejects_a_receipt_that_names_another_operation(tmp_path):
    app, launcher, harnesses, process, fenced, token = _arrange_pending(tmp_path)
    manager = None
    try:
        operation_id = "transfer-quiesce:move:1"
        app._lifecycle_manager.submit(LifecycleOperation.deactivate(operation_id, fenced))
        process.release.set()
        assert app._lifecycle_manager.wait(operation_id, 5).state.value == "completed"

        # A matching-in-everything-else COMPLETED stop that names a different
        # operation is not this intent's receipt.
        manager, app._lifecycle_manager = app._lifecycle_manager, _MismatchedReceipt(
            app._lifecycle_manager, "transfer-quiesce:move:999")
        app._reconcile_transfer_quiesce()

        row = app._transfer_p2._store.journal.get("move")
        assert row.phase == "quiesce-pending" and row.receipt == ""
        assert app._transfer_source_state("worker", token).blocks_start
    finally:
        if manager is not None:
            app._lifecycle_manager = manager
        process.release.set()
        _close_application(app)


class _RefusingOwner:
    """Owner edge double: the hold is answered without a positive ack."""

    def __init__(self, inner):
        self._inner = inner

    def call(self, command, expected, **kwargs):
        result = self._inner.call(command, expected, **kwargs)
        if isinstance(result, StartsHeld) and result.disposition == "held":
            return replace(result, disposition="unavailable")
        return result


def test_pending_quiesce_stays_unknown_when_the_owner_does_not_confirm_the_hold(tmp_path):
    app, launcher, harnesses, process, fenced, token = _arrange_pending(tmp_path)
    try:
        operation_id = "transfer-quiesce:move:1"
        app._lifecycle_manager.submit(LifecycleOperation.deactivate(operation_id, fenced))
        process.release.set()
        assert app._lifecycle_manager.wait(operation_id, 5).state.value == "completed"

        app._harnesses = _RefusingOwner(harnesses)
        app._reconcile_transfer_quiesce()

        row = app._transfer_p2._store.journal.get("move")
        assert row.phase == "quiesce-pending" and row.receipt == ""
        assert app._transfer_source_state("worker", token).blocks_start
    finally:
        process.release.set()
        _close_application(app)
