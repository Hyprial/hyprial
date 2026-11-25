"""Ambiguous E2E-024 sends resolve only the original public command identity."""
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from hyprial.kernel import RequestPortError
from tests.daemon.delivery.message.test_message_history import SESSION, harness as harness
from tests.support.actor_hosts import new_actor_host


ROOT = Path(__file__).resolve().parents[3]
MESSAGE_ID = "original-message-id"
TARGET = "agent:receiver:node-b:a.target"


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def helper():
    return runpy.run_path(str(ROOT / "contract/e2e/_peer_restart_send.py"))


def unknown(scenario, data=None, code="SUBMIT_OUTCOME_UNKNOWN"):
    return scenario["ScenarioRequestError"](
        "message.send", RequestPortError(code, "original outcome unknown", data or {"messageId": MESSAGE_ID}),
    )


def accepted(params):
    return {"status": "accepted", "commandId": params["idempotencyKey"],
            "conversationId": params["conversationId"], "messageId": MESSAGE_ID}


@pytest.mark.parametrize(("pending", "ack"), [(True, True), (False, True), (True, False)])
def test_unknown_send_uses_readonly_lookup_before_delivery_and_ack(monkeypatch, tmp_path, pending, ack):
    scenario = runpy.run_path(str(ROOT / "contract/e2e/run-peer-restart-relearn.py"))
    namespace = scenario["_try_send"].__globals__
    calls = []
    original = {}

    def request(method, params, **kwargs):
        calls.append((method, dict(params), kwargs))
        if method == "message.send":
            original.update(params)
            raise RequestPortError("SUBMIT_OUTCOME_UNKNOWN", "no confirmed outcome", {"messageId": MESSAGE_ID})
        if method == "message.send.lookup":
            assert params == {
                "from": scenario["DRIVER"], "target": TARGET, "text": "case-b-to-a",
                "conversationId": original["conversationId"], "idempotencyKey": original["idempotencyKey"],
            }
            return {"status": "accepted", "commandId": original["idempotencyKey"],
                    "conversationId": original["conversationId"], "messageId": MESSAGE_ID}
        assert method == "message.ack"
        assert params["messageId"] == MESSAGE_ID
        return {"acknowledged": ack}

    monkeypatch.setitem(namespace, "daemon_request_once", request)
    monkeypatch.setitem(namespace, "_actor_uri", lambda _daemon: TARGET)
    monkeypatch.setitem(namespace, "_pending_ids", lambda _daemon: {MESSAGE_ID} if pending else set())
    daemon = SimpleNamespace(root=tmp_path, node_id="test-node")
    result = scenario["_try_send"](daemon, daemon, "case-b-to-a")
    assert result["delivered"] is (pending and ack)
    assert result["messageId"] == MESSAGE_ID
    assert [row[0] for row in calls] == ["message.send", "message.send.lookup"] + (["message.ack"] if pending else [])
    if pending:
        assert result["acknowledged"] is ack
    assert result["phase"] == ("acknowledged" if pending and ack else "ack" if pending else "delivery")


def test_lookup_reuses_only_the_remaining_original_submission_budget():
    scenario = helper()
    clock = Clock()
    calls = []

    def request(_daemon, method, params, *, timeout):
        calls.append((method, dict(params), timeout))
        if method == "message.send":
            clock.sleep(2.5)
            raise unknown(scenario)
        clock.sleep(0.2)
        return {"status": "unknown", "reason": "not-found"} if len(calls) == 2 else accepted(params)

    result, evidence = scenario["submit_once"](None, request=request, sender="sender", target=TARGET,
                                                text="frozen text", clock=clock)
    assert result["messageId"] == MESSAGE_ID and evidence["recoveredByLookup"] is True
    assert [row[0] for row in calls] == ["message.send", "message.send.lookup", "message.send.lookup"]
    assert [row[2] for row in calls] == pytest.approx([4.0, 1.5, 1.25])
    assert calls[1][1] == calls[2][1]
    assert evidence["firstRequestSeconds"] == 2.5 and evidence["elapsedSeconds"] == 2.95


def test_unknown_until_the_original_deadline_fails_without_resubmitting():
    scenario = helper()
    clock = Clock()
    methods = []

    def request(_daemon, method, _params, *, timeout):
        assert 0 < timeout <= 4 - clock.now
        methods.append(method)
        if method == "message.send":
            clock.sleep(3.7)
            raise unknown(scenario)
        return {"status": "unknown", "reason": "not-found"}

    with pytest.raises(scenario["SubmissionFailure"], match="original 4s submission deadline") as caught:
        scenario["submit_once"](None, request=request, sender="sender", target=TARGET, text="text", clock=clock)
    assert methods.count("message.send") == 1 and methods.count("message.send.lookup") >= 1
    assert clock.now == 4.0 and caught.value.evidence["phase"] == "failed"


@pytest.mark.parametrize("change", [
    {"commandId": "foreign"}, {"conversationId": "foreign"}, {"messageId": "foreign"},
    {"commandId": None}, {"conversationId": None}, {"messageId": None},
    {"status": "failed"}, {"status": "unknown", "reason": "request-mismatch"},
])
def test_foreign_missing_or_terminal_lookup_never_confirms(change):
    scenario = helper()
    methods = []

    def request(_daemon, method, params, **_kwargs):
        methods.append(method)
        if method == "message.send":
            raise unknown(scenario)
        return accepted(params) | change

    with pytest.raises(scenario["SubmissionFailure"]):
        scenario["submit_once"](None, request=request, sender="sender", target=TARGET, text="text")
    assert methods == ["message.send", "message.send.lookup"]


@pytest.mark.parametrize("receipt", [
    {"messageId": ""}, {"target": TARGET},
    {"messageId": MESSAGE_ID, "target": "agent:foreign:node:a.id"},
    {"messageId": MESSAGE_ID, "idempotencyKey": "foreign"},
    {"messageId": MESSAGE_ID, "outcomeKnown": True},
])
def test_missing_or_foreign_unknown_receipt_fails_before_lookup(receipt):
    scenario = helper()
    methods = []

    def request(_daemon, method, _params, **_kwargs):
        methods.append(method)
        raise unknown(scenario, receipt)

    with pytest.raises(scenario["SubmissionFailure"]):
        scenario["submit_once"](None, request=request, sender="sender", target=TARGET, text="text")
    assert methods == ["message.send"]


@pytest.mark.parametrize("code", ["INVALID_ARGUMENT", "SENDER_NOT_AUTHENTICATED", "IPC_TIMEOUT"])
def test_other_send_errors_fail_immediately_without_lookup(code):
    scenario = helper()
    methods = []

    def request(_daemon, method, _params, **_kwargs):
        methods.append(method)
        raise unknown(scenario, code=code)

    with pytest.raises(scenario["SubmissionFailure"], match=code):
        scenario["submit_once"](None, request=request, sender="sender", target=TARGET, text="text")
    assert methods == ["message.send"]


def test_late_accepted_lookup_cannot_extend_the_original_deadline():
    scenario = helper()
    clock = Clock()

    def request(_daemon, method, params, **_kwargs):
        if method == "message.send":
            clock.sleep(3.9)
            raise unknown(scenario)
        clock.sleep(0.2)
        return accepted(params)

    with pytest.raises(scenario["SubmissionFailure"], match="original 4s submission deadline"):
        scenario["submit_once"](None, request=request, sender="sender", target=TARGET, text="text", clock=clock)


def test_ordinary_accepted_send_never_uses_lookup():
    scenario = helper()
    methods = []

    def request(_daemon, method, params, **_kwargs):
        methods.append(method)
        return {"ok": True, "operationId": params["idempotencyKey"],
                "conversationId": params["conversationId"], "messageId": MESSAGE_ID}

    result, evidence = scenario["submit_once"](None, request=request, sender="sender", target=TARGET, text="text")
    assert result["messageId"] == MESSAGE_ID and evidence["recoveredByLookup"] is False
    assert methods == ["message.send"]


def test_real_admitted_inbox_command_is_looked_up_without_resubmission(harness):
    import threading

    from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade
    from hyprial.daemon.impl.inbox.authority.errors import InboxSubmissionOutcomeUnknown

    scenario = helper()
    entered = threading.Event()
    release = threading.Event()
    methods = []
    deliveries = []
    original_service = harness.inbox

    def blocked_delivery(message):
        deliveries.append(message.message_id)
        entered.set()
        assert release.wait(3)
        return False

    harness.transport.deliver = blocked_delivery

    class Events:
        def publish(self, _event):
            pass

    coordinator = DeliveryCustodyCoordinator(
        harness.database, harness.transport, Events(),
        actor_host=new_actor_host(),
    )
    harness.app._inbox = DeliveryCustodyFacade(coordinator, harness.database, timeout=0.05)

    def request(_daemon, method, params, **_kwargs):
        methods.append(method)
        try:
            return harness.app.handle(method, params | {"sessionRef": SESSION}, _trusted_message_origin="ipc")
        except InboxSubmissionOutcomeUnknown as error:
            raise scenario["ScenarioRequestError"](method, RequestPortError(error.code, str(error), error.data)) from error

    try:
        result, evidence = scenario["submit_once"](
            None, request=request, sender=harness.actor, target=harness.reader, text="one original command",
        )
        assert entered.is_set() and not release.is_set()
        assert evidence["recoveredByLookup"] is True
        assert result["messageId"] == evidence["requestError"]["data"]["messageId"]
        assert methods[0] == "message.send" and methods.count("message.send") == 1
        assert len(methods) >= 2 and all(method == "message.send.lookup" for method in methods[1:])
        assert deliveries == [result["messageId"]]
    finally:
        release.set()
        assert coordinator.drain(3).complete
        harness.app._inbox = original_service


@pytest.mark.parametrize("saved", [True, False])
def test_complete_case_json_survives_a_log_line_larger_than_the_ci_limit(tmp_path, capsys, saved):
    import json

    scenario = runpy.run_path(str(ROOT / "contract/e2e/_peer_restart_parallel.py"))
    case = tmp_path / "case-1"
    case.mkdir()
    full = {"event": "peer-restart-path", "path": "case", "trace": "x" * 70_000}
    encoded = json.dumps(full)
    if saved:
        (case / "forwarding-evidence.json").write_text(encoded)
    log = tmp_path / "case-1.worker.log"
    log.write_text(json.dumps({"event": "peer-restart-summary", "path": "case"}) + "\n" + encoded + "\n")
    destination = tmp_path / "artifacts"
    if not saved:
        with pytest.raises(AssertionError, match="fullEvidence=False"):
            scenario["_result"](log, code=0, index=1, evidence_root=destination)
        return
    assert scenario["_result"](log, code=0, index=1, evidence_root=destination) == full
    assert (destination / "case-1.json").read_text() == encoded
    assert "full=" in capsys.readouterr().out
