"""Host lookup keeps read failures separate from the original send outcome."""

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.gui.adapter import AppAdapterUnavailable
from hyprial.shell.impl.gui.adapter.authz import AuthorizationMatrix, matrix_denial
from hyprial.shell.impl.gui.adapter.envelope import VerifiedRequest
from hyprial.shell.impl.gui.adapter.host import daemon_request_callable, handle_request
from tests.shell.gui.adapter.test_app_chat_adapter import Recorder, make_adapter

BODY = {"text": "frozen draft", "idempotencyKey": "original-key"}
ACCEPTED = {"status": "accepted", "commandId": "original-key",
            "conversationId": "conv-1", "messageId": "authoritative-id"}


def test_adapter_and_host_lookup_only_original_request():
    recorder = Recorder(ACCEPTED, {"status": "unknown", "reason": "not-found"})
    adapter = make_adapter(recorder)
    assert adapter.send_lookup("s-1", BODY) == ACCEPTED
    frame = handle_request(adapter, {
        "operation": "sessions.send.lookup",
        "params": {"sessionId": "s-1", "request": BODY},
    })
    assert frame == {"ok": True, "result": {"status": "unknown", "reason": "not-found"}}
    assert [method for method, _ in recorder.calls] == ["message.send.lookup"] * 2
    assert recorder.calls[0][1] == {
        "from": adapter._sender, "target": adapter.session_binding("s-1").targets[0],
        "conversationId": "conv-1", **BODY,
    }
    assert adapter.capabilities()["operations"]["sessions.send.lookup"]["supported"]


@pytest.mark.parametrize("answer", [
    {}, {**ACCEPTED, "commandId": "wrong"}, {**ACCEPTED, "conversationId": "wrong"},
    {**ACCEPTED, "messageId": ""}, {"status": "failed"},
    {"status": "unknown", "reason": []},
    DaemonRequestError("SENDER_NOT_AUTHENTICATED", "expired read authorization"),
    OSError("lost connection"),
])
def test_unreliable_lookup_never_claims_original_send_failed(answer):
    adapter = make_adapter(Recorder(answer))
    frame = handle_request(adapter, {
        "operation": "sessions.send.lookup",
        "params": {"sessionId": "s-1", "request": BODY},
    })
    assert frame["ok"] is False
    assert frame["error"]["definitive"] is False


@pytest.mark.parametrize("code", [
    "SUBMIT_OUTCOME_UNKNOWN", "INVALID_JSON", "INVALID_RESPONSE",
    "IPC_RESPONSE_TOO_LARGE", "VERSION_MISMATCH",
])
def test_uncertain_send_error_envelope_is_not_a_rejection(code):
    adapter = make_adapter(Recorder(DaemonRequestError(code, "uncertain")))
    with pytest.raises(AppAdapterUnavailable):
        adapter.send("s-1", BODY)


def test_daemon_request_wrapper_preserves_error_data(monkeypatch):
    import hyprial.kernel as kernel
    details = {"commandAdmission": "accepted", "outcomeKnown": False}

    def request(method, params, **kwargs):
        raise kernel.RequestPortError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "pending", details)

    monkeypatch.setattr(kernel, "daemon_request", request)
    with pytest.raises(DaemonRequestError) as caught:
        daemon_request_callable()("message.send", {})
    assert caught.value.data == details


@pytest.mark.parametrize("params", [
    {"sessionId": "missing", "request": BODY},
    {"sessionId": "s-1", "request": {**BODY, "from": "forged"}},
    {"sessionId": "s-1", "request": BODY, "principal": "forged"},
    {"sessionId": "s-1", "request": None},
])
def test_invalid_lookup_does_not_contact_daemon_or_reject_original_send(params):
    recorder = Recorder()
    frame = handle_request(make_adapter(recorder), {
        "operation": "sessions.send.lookup", "params": params,
    })
    assert frame["error"]["definitive"] is False
    assert recorder.calls == []


def test_lookup_requires_the_existing_session_authorization():
    binding = make_adapter(Recorder()).session_binding("s-1")
    verified = VerifiedRequest("device", binding.targets[0], "sessions.send.lookup",
                               "s-1", 1, "node")
    matrix = AuthorizationMatrix()
    assert matrix_denial(matrix, verified, binding) is None
    assert matrix_denial(matrix, verified, None) is not None
    stranger = VerifiedRequest("device", "other-author", "sessions.send.lookup",
                               "s-1", 1, "node")
    assert matrix_denial(matrix, stranger, binding) is not None
