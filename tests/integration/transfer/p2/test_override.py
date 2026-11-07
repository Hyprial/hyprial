"""Authenticated custody override: audit, alert, exact release, keep custody.

These are composition tests against the real daemon owners and real SQLite;
process and network edges are controlled fakes. They are not daemon subprocess
crash acceptance, and they never claim the full TC013-015 daemon matrix.
"""

from dataclasses import replace
import threading

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.composition import (
    AgentIdentityProjectionView, CorrelatedDomainEvents, HarnessPortClient,
)
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.transfer.p2.actor import Command, Operation
from hyprial.daemon.impl.transfer.p2.model import Binding, Role
from hyprial.daemon.impl.transfer.p2.source.effect import QuiesceRequest
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _LocalDelivery, _Presence, _Transport, _isolated_application, _close_application,
)

OWNER = "alice"


class _Process:
    pid = None

    def __init__(self):
        self.running = True
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()
        self.running = False


class _Launcher:
    def __init__(self):
        self.processes = []

    def start(self, _spec):
        process = _Process()
        self.processes.append(process)
        return process


class _NoticeInbox:
    """Durable-notice double; ``fail`` models an unwritable audit alert."""

    def __init__(self, *, fail=False):
        self.notices = []
        self.fail = fail
        self.order = []

    def receive(self, _message) -> None:
        return None

    def has_received(self, _message_id: str) -> bool:
        return False

    def has_fetched(self, _message_id: str) -> bool:
        return False

    def receive_system_notice(self, notice) -> bool:
        self.order.append("alert")
        if self.fail:
            return False
        self.notices.append(notice)
        return True


def _compose(tmp_path, *, inbox=None):
    app = _isolated_application(state_dir=tmp_path / "state",
                                socket_path=tmp_path / "daemon.sock",
                                node_id="node", owner=OWNER, network_isolated=True)
    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events,
                               desired_state=app.desired_state,
                               agent_identity=AgentIdentityProjectionView(app._agent_session_domains),
                               restart_backoff_seconds=0)
    harnesses = HarnessPortClient(actor, events)
    inbox = inbox or _NoticeInbox()
    app._harnesses, app._inbox, app._presence = harnesses, inbox, _Presence()
    app._start_lifecycle_manager(transport=_Transport(), inbox=inbox,
                                 local_delivery=_LocalDelivery(), harnesses=harnesses)
    return app, launcher, harnesses, inbox


def _held_source(app, launcher, *, operation_id="move", harness="codex"):
    """Start a worker, quiesce it and leave a durable post-quiesce hold."""

    spec = HarnessLaunchSpec(harness, "worker", True)
    app.handle("lifecycle.start", spec.to_json())
    agent = app.agents.require("worker")
    binding = Binding(Role.SOURCE, OWNER, "node", "worker", agent.entity_token,
                      "target-node", "target", "a" * 64)
    app._transfer_p2.submit(Command(Operation.PREPARE, operation_id, binding)).result(5)
    request = QuiesceRequest(operation_id, "worker", replace(
        app._lifecycle_spec(spec), expected_entity_token=agent.entity_token))
    outcome = app._quiesce_p2_source(request).result(70)
    assert outcome.state == "quiesced", outcome
    # The stop step drops the desired spec; a source that is still managed with
    # retained custody (the boot-hold-replay case) is what the guard must
    # reclassify, so restore the managed spec for that scenario.
    app.desired_state.upsert_harness(spec)
    fact = app._transfer_source_state("worker", agent.entity_token)
    assert fact.operations == (operation_id,)
    assert fact.holds and fact.holds[0].operation_id == operation_id
    return spec, agent, fact


def test_override_audits_alerts_releases_and_keeps_custody(tmp_path):
    app, launcher, harnesses, inbox = _compose(tmp_path)
    try:
        spec, agent, fact = _held_source(app, launcher)
        assert agent.uri == app._lifecycle_spec(spec).actor
        # Held custody suppresses an explicit start before the override.
        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("lifecycle.start", spec.to_json())
        assert rejected.value.code == "TRANSFER_STARTS_HELD"
        assert app._transfer_source_state("worker", agent.entity_token).blocks_start
        published = []
        original_publish = app._publish_restore_eligibility

        def publish(**kwargs):
            published.append(kwargs)
            return original_publish(**kwargs)

        app._publish_restore_eligibility = publish
        before = len(launcher.processes)
        order = []
        original_alert = app._transfer_override_alert
        original_call = harnesses.call

        def alert(**kwargs):
            order.append("alert")
            return original_alert(**kwargs)

        def call(command, result, **kwargs):
            if type(command).__name__ == "ReleaseStartsHold":
                order.append("release")
            return original_call(command, result, **kwargs)

        app._transfer_override_alert = alert
        harnesses.call = call
        receipt = app.handle("transfer.custody.override", {
            "principal": OWNER, "actor": "worker",
            "operationId": "move", "reason": "operator-moved-the-worker",
        })
        assert receipt["ok"] and receipt["holdReleased"]
        assert receipt["custody"] == "held-overridden"
        assert order == ["alert", "release"], order
        # Durable audit row: principal/action/reason are the receipt.
        events = app._transfer_p2._store.journal.events("move")
        assert [(e.principal, e.action, e.reason) for e in events] == [
            (OWNER, "transfer-custody-override", "operator-moved-the-worker"),
        ]
        assert len(inbox.notices) == 1
        # Custody is retained and marked needs-attention: never deleted.
        step = app._transfer_p2._store.journal.get("move")
        assert step.needs_attention and step.phase == "quiesced"
        fact = app._transfer_source_state("worker", agent.entity_token)
        assert fact.operations == ("move",) and fact.overridden
        assert not fact.blocks_start
        # The audited override clears the guard's published suppression and the
        # exact hold no longer blocks a new start.
        assert published and published[-1]["suppressed"] is False
        assert published[-1]["entity_token"] == agent.entity_token
        app.handle("lifecycle.start", spec.to_json())
        assert len(launcher.processes) == before + 1
    finally:
        _close_application(app)


def test_override_rejects_foreign_principal_and_writes_nothing(tmp_path):
    app, launcher, _, inbox = _compose(tmp_path)
    try:
        _held_source(app, launcher)
        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("transfer.custody.override", {
                "principal": "someone-else", "actor": "worker",
                "operationId": "move", "reason": "not-my-machine",
            })
        assert rejected.value.code == "CALLER_NOT_AUTHORIZED"
        assert app._transfer_p2._store.journal.events("move") == ()
        assert inbox.notices == []
        assert app._transfer_source_state("worker", app.agents.require("worker").entity_token).blocks_start
    finally:
        _close_application(app)


@pytest.mark.parametrize("reason", ["", "x" * 501])
def test_override_requires_a_bounded_reason(tmp_path, reason):
    app, launcher, _, _ = _compose(tmp_path)
    try:
        _held_source(app, launcher)
        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("transfer.custody.override", {
                "principal": OWNER, "actor": "worker",
                "operationId": "move", "reason": reason,
            })
        assert rejected.value.code == "INVALID_ARGUMENT"
        assert app._transfer_p2._store.journal.events("move") == ()
    finally:
        _close_application(app)


def test_override_keeps_the_hold_when_the_alert_is_not_durable(tmp_path):
    app, launcher, harnesses, inbox = _compose(tmp_path, inbox=_NoticeInbox(fail=True))
    try:
        spec, agent, _ = _held_source(app, launcher)
        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("transfer.custody.override", {
                "principal": OWNER, "actor": "worker",
                "operationId": "move", "reason": "alert-cannot-land",
            })
        assert rejected.value.code == "TRANSFER_CUSTODY_UNAVAILABLE"
        # The audit row is durable (it precedes the alert) but nothing was
        # released: the harness owner's hold still refuses the start, and the
        # guard's suppression was never cleared by this attempt.
        assert app._transfer_p2._store.journal.get("move").needs_attention
        assert inbox.order == ["alert"]
        with pytest.raises(DaemonRequestError) as blocked:
            app.handle("lifecycle.start", spec.to_json())
        assert blocked.value.code == "TRANSFER_STARTS_HELD"
        assert launcher.processes and not launcher.processes[-1].running
    finally:
        _close_application(app)


def test_release_must_name_the_exact_active_hold(tmp_path):
    app, launcher, harnesses, _ = _compose(tmp_path)
    try:
        spec, agent, fact = _held_source(app, launcher)
        # A stale generation names no active hold: the owner rejects it and the
        # suppression is retained even though the audit row is durable.
        original = harnesses.call

        def stale(command, result, **kwargs):
            if type(command).__name__ == "ReleaseStartsHold":
                command = replace(command, generation=command.generation + 1)
            return original(command, result, **kwargs)

        harnesses.call = stale
        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("transfer.custody.override", {
                "principal": OWNER, "actor": "worker",
                "operationId": "move", "reason": "wrong-generation",
            })
        assert rejected.value.code == "TRANSFER_CONFLICT"
        assert app._transfer_p2._store.journal.get("move").needs_attention
        # The exact hold was never released, so the owner still refuses a start
        # even though the audit row is durable.
        with pytest.raises(DaemonRequestError) as blocked:
            app.handle("lifecycle.start", spec.to_json())
        assert blocked.value.code == "TRANSFER_STARTS_HELD"
    finally:
        _close_application(app)


def test_override_audit_survives_reopen_and_boot_still_holds(tmp_path):
    from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

    app, launcher, _, _ = _compose(tmp_path)
    try:
        _held_source(app, launcher)
        app.handle("transfer.custody.override", {
            "principal": OWNER, "actor": "worker",
            "operationId": "move", "reason": "durable-override",
        })
        database = app._transfer_p2._store._database
        reopened = AdmissionStore(database, node="node", owner=OWNER)
        step = reopened.journal.get("move")
        assert step.needs_attention and step.phase == "quiesced"
        events = reopened.journal.events("move")
        assert [(e.principal, e.reason) for e in events] == [(OWNER, "durable-override")]
        # Custody was never deleted by the override.
        assert reopened.journal.custody(
            "worker", app.agents.require("worker").entity_token).blocked
    finally:
        _close_application(app)


def test_override_refuses_an_unknown_operation_without_side_effects(tmp_path):
    app, launcher, _, inbox = _compose(tmp_path)
    try:
        app.agents.create("worker")
        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("transfer.custody.override", {
                "principal": OWNER, "actor": "worker",
                "operationId": "never-prepared", "reason": "no-such-op",
            })
        assert rejected.value.code == "TRANSFER_CONFLICT"
        assert inbox.notices == []
    finally:
        _close_application(app)


def test_a_hold_with_a_step_names_the_override_that_frees_it(tmp_path):
    """Closeout ①: a stepped retention points at the release that exists.

    The refused second preparation says how the holder is resolved, not just
    that it is held: this attempt wrote steps, so its own audited override is
    the way out, and the refusal says so.
    """

    app, launcher, _, _inbox = _compose(tmp_path)
    try:
        _spec, _agent, _fact = _held_source(app, launcher, harness="pi")
        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("transfer.quiesce", {
                "name": "worker", "provider": "pi",
                "operationId": "transfer-p2:second",
                "targetNode": "target-node", "targetActor": "worker",
                "planDigest": "b" * 64,
            })
        assert rejected.value.code == "TRANSFER_CONFLICT"
        assert rejected.value.data["reservedOperationId"] == "move"
        assert rejected.value.data["reservedHasStep"] is True
        assert "transfer.custody.override" in str(rejected.value)
        assert "no automatic release path" not in str(rejected.value)
    finally:
        _close_application(app)


def test_a_hold_with_no_step_says_there_is_no_release_path(tmp_path):
    """Closeout ①: a bare reservation has no audited override to offer.

    ``apply_override`` answers ``UNKNOWN`` when the operation wrote no step, so
    the refusal must not point at it; the operator gets the truth instead of a
    wasted round trip.
    """

    app, launcher, _, _inbox = _compose(tmp_path)
    try:
        spec = HarnessLaunchSpec("pi", "worker", True)
        app.handle("lifecycle.start", spec.to_json())
        agent = app.agents.require("worker")
        binding = Binding(Role.SOURCE, OWNER, "node", "worker", agent.entity_token,
                          "target-node", "target", "a" * 64)
        # A bare preparation: the admission exists, no step was ever recorded.
        app._transfer_p2.submit(Command(Operation.PREPARE, "move", binding)).result(5)
        assert app._transfer_p2._store.journal.get("move") is None

        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("transfer.quiesce", {
                "name": "worker", "provider": "pi",
                "operationId": "transfer-p2:second",
                "targetNode": "target-node", "targetActor": "worker",
                "planDigest": "b" * 64,
            })
        assert rejected.value.code == "TRANSFER_CONFLICT"
        assert rejected.value.data["reservedOperationId"] == "move"
        assert rejected.value.data["reservedHasStep"] is False
        assert "no automatic release path" in str(rejected.value)
        assert "transfer.custody.override" not in str(rejected.value)
    finally:
        _close_application(app)


def test_the_next_attempt_reaps_the_preparation_the_audit_released(tmp_path):
    """A3: the two-daemon probe's shape -- fail, audit the override, retry.

    The rollback cannot delete the reservation, so the successor reaps the
    operation an authenticated principal already audited away.  Before this,
    the second attempt answered ``reserved`` forever.
    """

    app, launcher, _, _inbox = _compose(tmp_path)
    try:
        _spec, agent, _fact = _held_source(app, launcher)
        receipt = app.handle("transfer.custody.override", {
            "principal": OWNER, "actor": "worker",
            "operationId": "move", "reason": "transfer rollback",
        })
        assert receipt["ok"] and receipt["holdReleased"]
        binding = Binding(Role.SOURCE, OWNER, "node", "worker", agent.entity_token,
                          "target-node", "target", "a" * 64)
        retry = app._transfer_p2.submit(
            Command(Operation.PREPARE, "move-2", binding)
        ).result(5)
        assert retry.operation_id == "move-2"
        # The reservation was cancelled, never deleted, and its audit survives.
        released = app._transfer_p2._store.get("move")
        assert released is not None and released.cancelled_at_ms is not None
        step = app._transfer_p2._store.journal.get("move")
        assert step.needs_attention
        assert [event.reason for event in app._transfer_p2._store.journal.events("move")] == [
            "transfer rollback",
        ]
    finally:
        _close_application(app)


def test_a_live_reservation_is_refused_with_the_operation_that_holds_it(tmp_path):
    """A3: ``reserved`` alone is not actionable, so the refusal names the holder.

    The retry is refused here because nothing has audited the first attempt
    away (the operator never rolled it back); the daemon says WHICH operation
    holds the worker instead of repeating the ledger's word.
    """

    app, launcher, _, _inbox = _compose(tmp_path)
    try:
        spec, agent, _fact = _held_source(app, launcher, harness="pi")
        assert agent.uri == app._lifecycle_spec(spec).actor
        with pytest.raises(DaemonRequestError) as rejected:
            app.handle("transfer.quiesce", {
                "name": "worker", "provider": "pi",
                "operationId": "transfer-p2:second",
                "targetNode": "target-node", "targetActor": "worker",
                "planDigest": "b" * 64,
            })
        assert rejected.value.code == "TRANSFER_CONFLICT"
        assert rejected.value.data["reservedOperationId"] == "move"
        assert "move" in str(rejected.value)
    finally:
        _close_application(app)
