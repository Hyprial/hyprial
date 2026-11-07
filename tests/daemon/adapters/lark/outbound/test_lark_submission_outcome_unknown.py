"""Accepted Inbox commands retain unknown custody across late/lost IPC replies."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from tests.daemon.adapters.lark.outbound.test_lark_daemon_idempotency import _DroppingDaemonSocket, _LoopbackTransport, _Presence, _Routes, _Lark
from hyprial.daemon.impl.adapters.lark import (
    ActorTarget,
    LarkAdapter,
    LarkInboundMessage,
    LarkStateStore,
)
from hyprial.daemon.impl.adapters.lark.state import store as state_module
from hyprial.daemon.impl.adapters.lark.worker.daemon_link import _Harness
from hyprial.daemon.impl.alias_resolver import (
    AliasKind,
    AliasRecord,
    AliasResolver,
    StaticAliasSource,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox.actor.coordinator import DeliveryCustodyCoordinator
from hyprial.daemon.impl.inbox.authority.facade import DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.actor.errors import InboxAuthorityUnavailable
from hyprial.daemon.impl.inbox.contracts.ports import SubmissionCompleted, SubmitMessageCommand
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


class _Logs:
    def __init__(self): self.events = []
    def bind(self, **_kwargs): return self
    def log(self, level, event, **fields): self.events.append((event, fields))
    def info(self, event, **fields): self.log("info", event, **fields)
    def warn(self, event, **fields): self.log("warn", event, **fields)


class _Platform(_Lark):
    def __init__(self, message):
        super().__init__(message)
        self.replies = []
    def reply(self, message_id, text, *, idempotency_key):
        self.replies.append((message_id, text, idempotency_key))
        return f"om_notice_{len(self.replies)}"


class _Rig:
    def __init__(self, root: Path, phase="after-commit", budget=0):
        self.root = root
        self.entered = threading.Event()
        self.release = threading.Event()
        self.settled = threading.Event()
        rig = self
        class Gate:
            first = True
            def publish(self, event):
                if isinstance(event, SubmissionCompleted) and self.first:
                    self.first = False
                    rig.correlation = event.correlation_id
                    rig.message_id = event.result.message_id
                    if phase == "after-commit":
                        rig.entered.set()
                        assert rig.release.wait(10)
                    rig.settled.set()
        self.transport = _LoopbackTransport()
        self.recipient = InboxService(root / "recipient.sqlite3", self.transport)
        self.transport.service = self.recipient
        self.coordinator = DeliveryCustodyCoordinator(root / "sender.sqlite3", self.transport, Gate(), node_id="lark-idempotency-node")
        self.facade = DeliveryCustodyFacade(self.coordinator, root / "sender.sqlite3")
        assert self.facade._timeout == 2.0
        if phase == "before-commit":
            handler = self.coordinator._active_handler
            original = handler._handle
            first = True
            def blocked(command):
                nonlocal first
                if isinstance(command, SubmitMessageCommand) and first:
                    first = False
                    self.entered.set()
                    assert self.release.wait(10)
                original(command)
            handler._handle = blocked
        home = root / "home"
        home.mkdir()
        self.app = DaemonApplication(state_dir=home / "state", socket_path=root / "d.sock", node_id="lark-idempotency-node", owner="test-owner", hyprial_home=home, zenoh_listen=(), zenoh_connect=(), **_deep_module_daemon_dependencies())
        self.app._inbox = self.facade
        self.app._presence = _Presence()
        self.app._harnesses = object()
        self.app._adapters = None
        self.app.handle("agent.create", {"name": "target-agent"})
        self.responses = []
        original_response = self.app._response
        def response(line):
            result = original_response(line)
            self.responses.append(result)
            return result
        self.app._response = response
        # AF_UNIX paths cap at 104 bytes: this test's pytest tmp_path is 151
        # chars, so the app's own socket path cannot be bound.  The rig owns
        # the daemon socket, so it binds a short one and points both the
        # server and the harness client at it.
        self.short_socket = Path("/tmp") / f"lark-rig-{uuid.uuid4().hex[:8]}.sock"
        self.server = _DroppingDaemonSocket(self.short_socket, self.app)
        self.server._drop_next = False
        self.server.start()
        self.message = LarkInboundMessage(event_id="evt-original", message_id="om_frozen_request", chat_id="oc_dm", chat_type="p2p", text="full original body " + "x" * 4000, sender_id="ou_sender", sender_type="user", message_type="text")
        self.state_path = root / "lark.sqlite3"
        self.logs = _Logs()
        self.platform = _Platform(self.message)
        self.adapter = LarkAdapter(state=LarkStateStore(self.state_path), lark=self.platform, harness=_Harness(self.short_socket), routes=_Routes(), channel_actor_id="channel:lark:test", logger=self.logs, org_context_path=root / "no-org-context", transient_retry_budget=budget, transient_retry_backoff_initial=.001, transient_retry_backoff_max=.002)

    def close(self):
        self.release.set()
        self.server.stop()
        self.short_socket.unlink(missing_ok=True)
        self.adapter.close()
        self.app._close()
        self.app._close_home_lease()
        self.recipient.close()


@pytest.fixture
def make_rig(tmp_path):
    rigs = []
    def make(**kwargs):
        rig = _Rig(tmp_path, **kwargs)
        rigs.append(rig)
        return rig
    yield make
    for rig in rigs:
        rig.close()


def test_post_commit_late_receipt_returns_committed_truth_without_failure_notice(make_rig):
    rig = make_rig()
    outcome = rig.adapter.handle_inbound(rig.message)
    assert rig.entered.is_set() and not rig.settled.is_set()
    assert outcome.status == "forwarded"
    assert rig.recipient.has_received(outcome.harness_message_id)
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) is None
    assert rig.adapter.dead_letters() == () and rig.platform.replies == []
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)


def test_precommit_timeout_is_unknown_then_same_frozen_retry_converges(make_rig):
    rig = make_rig(phase="before-commit", budget=3)
    original_read = rig.facade._reads.submission_result
    def release_after_unknown(correlation, message_id):
        result = original_read(correlation, message_id)
        assert result is None
        assert not rig.recipient.has_received(message_id)
        rig.release.set()
        return result
    rig.facade._reads.submission_result = release_after_unknown
    outcome = rig.adapter.handle_inbound(rig.message)
    assert outcome.status == "forwarded"
    first = rig.responses[0]["error"]
    assert first["code"] == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert first["data"]["commandAdmission"] == "accepted"
    assert first["data"]["outcomeKnown"] is False
    assert len(rig.server.requests) == 2
    assert rig.server.requests[0]["params"] == rig.server.requests[1]["params"]
    assert len(rig.recipient.pending_messages(_Routes.target.actor_id)) == 1
    assert rig.platform.replies == [] and rig.adapter.dead_letters() == ()


def test_unknown_exhaustion_survives_process_rebuild_and_pin_change(make_rig):
    rig = make_rig(phase="before-commit")
    outcome = rig.adapter.handle_inbound(rig.message)
    assert outcome.status == "error" and outcome.ack_error == "SubmitOutcomeUnknownError"
    assert rig.entered.is_set()
    assert rig.recipient.pending_messages(_Routes.target.actor_id) == ()
    [letter] = rig.adapter.dead_letters()
    assert letter.reason == "forward-outcome-unknown"
    assert not rig.adapter._state.seen(rig.message.event_id)
    assert not rig.adapter._state.seen_message(rig.message.message_id)
    frozen = rig.adapter._state.pending_inbound_submission(rig.message.message_id)
    assert len(json.loads(frozen)["request"]["text"]) > 4000
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)
    assert len(rig.platform.replies) == 1
    assert "可能已经收到" in rig.platform.replies[0][1]
    rig.adapter.close()
    rig.release.set()
    assert rig.settled.wait(3)
    # Real process reconstruction: no parent adapter/request object survives.
    script = "\nimport json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom tests.daemon.adapters.lark.outbound.test_lark_submission_outcome_unknown import _Platform,_Routes\nfrom hyprial.daemon.impl.adapters.lark import LarkAdapter,LarkInboundMessage,LarkStateStore,ActorTarget\nfrom hyprial.daemon.impl.adapters.lark.worker.daemon_link import _Harness\nclass ChangedRoutes(_Routes):\n    target=ActorTarget('agent:test-owner:lark-idempotency-node:new-target','new-target','new-target')\n    def pinned_actor(self,conversation_id):\n        raise AssertionError('recovery must not resolve a changed pin')\nmessage=LarkInboundMessage(**json.loads(sys.argv[4]))\nadapter=LarkAdapter(state=LarkStateStore(Path(sys.argv[2])),lark=_Platform(message),harness=_Harness(Path(sys.argv[3])),routes=ChangedRoutes(),channel_actor_id='channel:lark:test',org_context_path=Path(sys.argv[2]).parent/'no-context')\ntry:\n    result=adapter.recover_message(message.message_id)\n    print(json.dumps({'status':result.status,'messageId':result.harness_message_id,'pending':adapter._state.pending_inbound_submission(message.message_id),'deadLetters':len(adapter.dead_letters())}))\nfinally:adapter.close()\n"
    changed = replace(rig.message, text="changed platform text", event_id="new-event", content_status="unsupported")
    completed = subprocess.run([sys.executable, "-c", script, str(Path(__file__).resolve().parents[5] / 'tests'), str(rig.state_path), str(rig.short_socket), json.dumps(asdict(changed))], text=True, capture_output=True, check=True, timeout=15)
    recovered = json.loads(completed.stdout)
    assert recovered == {"status": "forwarded", "messageId": rig.message_id, "pending": None, "deadLetters": 0}
    assert len(rig.recipient.pending_messages(_Routes.target.actor_id)) == 1
    assert len(rig.server.requests) == 2
    assert rig.server.requests[0]["params"] == rig.server.requests[1]["params"]
    assert rig.server.requests[-1]["params"]["message"] == rig.message.text


@pytest.mark.parametrize("failure", ["disconnect", "malformed-result", "daemon-error"])
def test_committed_request_with_unusable_reply_remains_unknown(make_rig, failure):
    rig = make_rig(phase="unblocked")
    original = rig.app._response
    first = True
    def response(line):
        nonlocal first
        result = original(line)
        if first:
            first = False
            if failure == "malformed-result":
                return {"version": result["version"], "id": result["id"], "result": {"ok": True}}
            if failure == "daemon-error":
                return {"version": result["version"], "id": result["id"], "error": {"code": ipc_errors.DAEMON_ERROR, "message": "post-commit failure"}}
        return result
    rig.app._response = response
    rig.server._drop_next = failure == "disconnect"
    outcome = rig.adapter.handle_inbound(rig.message)
    assert outcome.status == "error" and outcome.ack_error == "SubmitOutcomeUnknownError"
    assert rig.recipient.has_received(rig.message_id)
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id)
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)
    assert len(rig.server.requests) == 1  # No blanket retry of DAEMON_ERROR.
    assert rig.adapter.recover_message(rig.message.message_id).status == "forwarded"
    assert len(rig.recipient.pending_messages(_Routes.target.actor_id)) == 1


def test_definitive_rejection_releases_request_and_keeps_failure_alarm(make_rig):
    rig = make_rig(phase="unblocked")
    def rejected(_method, _params, *, _trusted_message_origin=None):
        raise ipc_errors.DaemonRequestError(ipc_errors.UNSUPPORTED_TARGET, "explicit pre-admission refusal")
    rig.app._timed_handle = rejected
    outcome = rig.adapter.handle_inbound(rig.message)
    assert outcome.status == "error"
    assert rig.recipient.pending_messages(_Routes.target.actor_id) == ()
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) is None
    assert len(rig.server.requests) == 1
    assert any(event == "terminal.failure" for event, _ in rig.logs.events)
    assert any("not delivered" in text for _, text, _ in rig.platform.replies)


@pytest.mark.parametrize(
    ("target", "agent_rows", "expected_code"),
    [
        (
            "agent:test-owner:lark-idempotency-node:foo",
            (
                AliasRecord(AliasKind.AGENT, "foo", "a.first"),
                AliasRecord(AliasKind.AGENT, "foo", "a.second"),
            ),
            ipc_errors.AMBIGUOUS_TARGET,
        ),
        ("ghost", (), ipc_errors.TARGET_IS_NODE),
    ],
)
def test_alias_refusals_are_definitive_pre_admission_outcomes(
    make_rig, target, agent_rows, expected_code
):
    rig = make_rig(phase="unblocked")
    user = AliasRecord(AliasKind.USER, "test-owner", "u.owner")
    device = AliasRecord(
        AliasKind.DEVICE, "lark-idempotency-node", "d.device"
    )
    source = StaticAliasSource(
        (user, device, *agent_rows),
        local={AliasKind.USER: user, AliasKind.DEVICE: device},
    )
    rig.app._alias_resolver = AliasResolver(source)
    rig.adapter._routes.target = ActorTarget(target, target, target)

    outcome = rig.adapter.handle_inbound(rig.message)

    assert outcome.status == "error"
    assert outcome.ack_error == "DaemonRequestError"
    assert outcome.ack_error != "SubmitOutcomeUnknownError"
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) is None
    [letter] = rig.adapter.dead_letters()
    assert letter.detail == expected_code


def test_unknown_local_canonical_target_keeps_the_queued_send_contract(make_rig):
    rig = make_rig(phase="unblocked")
    user = AliasRecord(AliasKind.USER, "test-owner", "u.owner")
    device = AliasRecord(
        AliasKind.DEVICE, "lark-idempotency-node", "d.device"
    )
    rig.app._alias_resolver = AliasResolver(
        StaticAliasSource(
            (user, device),
            local={AliasKind.USER: user, AliasKind.DEVICE: device},
        )
    )
    target = "agent:test-owner:lark-idempotency-node:ghost"
    rig.adapter._routes.target = ActorTarget(target, target, target)

    outcome = rig.adapter.handle_inbound(rig.message)

    assert outcome.status == "forwarded"
    assert len(rig.recipient.pending_messages(target)) == 1
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) is None
    assert rig.adapter.dead_letters() == ()
    assert rig.platform.replies == []


@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_pending_submission_capacity_refuses_before_ipc(make_rig, monkeypatch, limit):
    rig = make_rig(phase="unblocked")
    if limit == "count":
        monkeypatch.setattr(state_module, "MAX_PENDING_INBOUND_SUBMISSIONS", 1)
        rig.adapter._state.record_pending_inbound_submission("older", "old-event", "immutable older custody")
    else:
        monkeypatch.setattr(state_module, "MAX_PENDING_INBOUND_SUBMISSION_BYTES", 1)
    outcome = rig.adapter.handle_inbound(rig.message)
    assert outcome.status == "submission-capacity-exhausted"
    assert rig.server.requests == [] and rig.platform.replies == []
    if limit == "count":
        assert rig.adapter._state.pending_inbound_submission("older") == "immutable older custody"


def test_close_stops_unknown_retry_and_retains_frozen_request(make_rig):
    rig = make_rig(phase="before-commit", budget=5)
    original = rig.facade._reads.submission_result
    closer = []
    def closing(correlation, message_id):
        result = original(correlation, message_id)
        thread = threading.Thread(target=rig.adapter.close)
        closer.append(thread)
        thread.start()
        assert rig.adapter._closing.wait(2)
        return result
    rig.facade._reads.submission_result = closing
    outcome = rig.adapter.handle_inbound(rig.message)
    assert outcome.ack_error == "SubmitOutcomeUnknownError"
    closer[0].join(3)
    assert not closer[0].is_alive()
    assert len(rig.server.requests) == 1 and rig.platform.replies == []
    reopened = LarkStateStore(rig.state_path)
    try:
        assert reopened.pending_inbound_submission(rig.message.message_id)
    finally:
        reopened.close()
    assert rig.adapter._handle_inbound(rig.message).ack_error == "inbound-closing"
    assert rig.adapter.recover_message(rig.message.message_id).ack_error == "inbound-closing"
    assert rig.adapter.reconcile_recent().retryable_errors == ("adapter: closing",)
    assert len(rig.server.requests) == 1
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)


def test_unadmitted_inbox_command_does_not_claim_submission_unknown(make_rig, monkeypatch):
    from hyprial.daemon.impl.inbox.contracts.api import InboxMessage, DeliveryLifecycle
    rig = make_rig(phase="unblocked")
    with monkeypatch.context() as patch:
        patch.setattr(rig.coordinator, "submit", lambda _command: PortAdmission.OVERLOADED)
        with pytest.raises(InboxAuthorityUnavailable) as error:
            rig.facade.submit(InboxMessage("not-admitted", "conversation", "sender", "recipient", b"body", "request", DeliveryLifecycle.DURABLE_SERVICE, 1))
    assert getattr(error.value, "code", None) != ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert rig.recipient.pending_messages(_Routes.target.actor_id) == ()


def test_corrupt_frozen_identity_never_reroutes_or_sends(make_rig):
    rig = make_rig(phase="before-commit")
    assert rig.adapter.handle_inbound(rig.message).ack_error == "SubmitOutcomeUnknownError"
    rig.release.set()
    assert rig.settled.wait(3)
    state = rig.adapter._state
    frozen = json.loads(state.pending_inbound_submission(rig.message.message_id))
    frozen["idempotencyKey"] = "different-key"
    with state._transaction() as db:
        db.execute("UPDATE pending_inbound_submissions SET request = ? WHERE message_id = ?", (json.dumps(frozen), rig.message.message_id))
    before = len(rig.server.requests)
    assert rig.adapter.recover_message(rig.message.message_id).ack_error == "SubmitOutcomeUnknownError"
    assert len(rig.server.requests) == before
    assert state.pending_inbound_submission(rig.message.message_id)
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)


def test_explicit_negative_submission_result_releases_pending_custody(make_rig):
    rig = make_rig(phase="unblocked")
    def rejected(method, params, *, _trusted_message_origin=None):
        from uuid import NAMESPACE_URL, uuid5
        key = params["idempotencyKey"]
        target = params["to"][0]
        message_id = str(uuid5(NAMESPACE_URL, f"hyprial:send:{key}:{target}:0"))
        return {"ok": False, "operationId": key, "conversationId": params["conversationId"], "messageId": message_id, "deliveries": [{"target": target, "messageId": message_id, "accepted": False, "queued": False}]}
    rig.app._timed_handle = rejected
    assert rig.adapter.handle_inbound(rig.message).status == "error"
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) is None
    assert any(event == "terminal.failure" for event, _ in rig.logs.events)
    assert rig.recipient.pending_messages(_Routes.target.actor_id) == ()


def test_unknown_code_is_explicitly_not_a_generic_transient():
    error = ipc_errors.SubmitOutcomeUnknownError("unknown", {"outcomeKnown": False})
    assert error.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert isinstance(error, ipc_errors.DaemonRequestError)
    assert not isinstance(error, ipc_errors.TransientDaemonError)
    assert ipc_errors.transient_error_from_code(error.code, str(error)) is None


def test_new_refusal_does_not_erase_previous_unknown_custody(make_rig):
    rig = make_rig(phase="before-commit")
    assert rig.adapter.handle_inbound(rig.message).ack_error == "SubmitOutcomeUnknownError"
    frozen = rig.adapter._state.pending_inbound_submission(rig.message.message_id)
    rig.release.set()
    assert rig.settled.wait(3)
    assert rig.recipient.has_received(rig.message_id)
    def rejected(_method, _params, *, _trusted_message_origin=None):
        raise ipc_errors.DaemonRequestError(ipc_errors.UNSUPPORTED_TARGET, "later request refused")
    rig.app._timed_handle = rejected
    assert rig.adapter.recover_message(rig.message.message_id).ack_error == "SubmitOutcomeUnknownError"
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) == frozen
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)
    assert len(rig.platform.replies) == 1


def test_late_negative_receipt_is_not_promoted_to_success(make_rig):
    from hyprial.daemon.impl.inbox.contracts.api import InboxMessage, DeliveryLifecycle
    rig = make_rig()
    rig.transport.is_online = lambda _recipient: False
    result = rig.facade.submit(InboxMessage("late-negative", "conversation", "sender", "recipient", b"body", "request", DeliveryLifecycle.ONLINE_ONLY, 1))
    assert rig.entered.is_set() and not rig.settled.is_set()
    assert result.accepted is False and result.queued is False
    assert result.code == "TARGET_OFFLINE"
    assert not rig.recipient.has_received(result.message_id)
