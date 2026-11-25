"""Native daemon HTTP with controlled IPC replies, not a model/vendor acceptance.

UA2: the Agent's reply to ``user:<owner>`` is delivered to this node's
``user:<owner>`` actor once the user agent ensure has bound it (``targets``
then lists the owner as ``user-agent``), so ``message.reply`` answers
``queued`` false and the row stays pending until the conversation recorder
acknowledges it.  Confirmation reads the retained durable row and holds on
either side of that acknowledgement.
"""
import json
import time

from hyprial.identity import DeviceRecord, write_device_record, write_settings_identity
from hyprial.kernel import daemon_request_once
from tests.integration.request_http.conftest import DaemonProcess, OWNER, running_http


def _eventually(label, read, check, *, timeout=30.0):
    deadline = time.monotonic() + timeout
    observed = read()
    while not check(observed):
        assert time.monotonic() < deadline, f"{label} not reached in {timeout:g}s: {observed!r}"
        time.sleep(0.1)
        observed = read()


def test_first_run_http_attempt_requires_its_current_reply(isolation):
    write_settings_identity(
        OWNER, mode="casdoor", issuer="https://issuer.example.test",
        user_id="p3-synthetic-user", hyprial_home=isolation.home,
    )
    write_device_record(isolation.home, DeviceRecord(
        device_id="p3-device", owner=OWNER, server_public="nodekey:server",
        client_public="nodekey:client", key_generation=1,
        device_uid="12345678123442348234123456789abc", name="p3-test",
    ))
    secrets = isolation.home / "secrets"
    secrets.mkdir(mode=0o700, exist_ok=True)
    login = secrets / "login.json"
    login.write_text(json.dumps({
        "version": 1, "issuer": "https://issuer.example.test",
        "refreshToken": "synthetic-not-a-real-token", "obtainedAt": "2026-11-18T00:00:00Z",
        "identityGeneration": 1,
    }))
    login.chmod(0o600)
    daemon = DaemonProcess(isolation)
    daemon.start()
    try:
        def ipc(method, params):
            return daemon_request_once(method, params, socket_path=isolation.socket_path, timeout=10)

        created = ipc("agent.create", {
            "name": "p3-probe", "preferredHarness": "pi",
            "provider": "deepseek", "model": "deepseek-flash",
        })
        agent = created.get("agent", created)
        uri = agent["uri"]
        ipc("agent.secret.provider-write", {
            "entryId": "p3-probe", "fieldName": "key", "value": "synthetic-not-a-real-model-key",
        })
        ipc("agent.secret.grant", {
            "actor": "p3-probe", "grantId": "p3-probe", "source": "user-provider",
            "entryId": "p3-probe", "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
        })
        with running_http(isolation) as http:
            status, _, response = http.request("POST", "/daemon/request/onboarding.firstRun.begin", body={
                "operationKey": "p3-http-probe-0123456789", "agentUri": uri,
            })
            assert status == 200, response
            attempt = response["result"]["attempt"]
            assert attempt["state"] == "pending"
            status, _, rejected = http.request("POST", "/daemon/request/onboarding.firstRun.confirm", body={
                "attemptId": attempt["attemptId"], "operationKey": attempt["operationKey"],
                "expectedAttemptRevision": attempt["revision"], "expectedReceiptRevision": 0,
            })
            assert status >= 400, rejected
            status, _, readback = http.request("POST", "/daemon/request/onboarding.status", body={})
            assert status == 200, readback
            assert readback["result"]["firstRun"]["confirmed"] == []

            sent = ipc("message.send", {
                "from": f"user:{OWNER}", "to": [uri],
                "message": "P3 controlled request", "summary": "P3 controlled request",
                "idempotencyKey": attempt["sendKey"], "conversationId": "p3-probe-conversation",
            })
            status, _, bound = http.request("POST", "/daemon/request/onboarding.firstRun.bind", body={
                "attemptId": attempt["attemptId"], "operationKey": attempt["operationKey"],
                "expectedRevision": attempt["revision"], "requestMessageId": sent["messageId"],
                "conversationId": sent["conversationId"],
            })
            assert status == 200, bound
            attempt = bound["result"]["attempt"]
            params = {
                "attemptId": attempt["attemptId"], "operationKey": attempt["operationKey"],
                "expectedAttemptRevision": attempt["revision"], "expectedReceiptRevision": 0,
            }
            status, _, premature = http.request("POST", "/daemon/request/onboarding.firstRun.confirm", body=params)
            assert status >= 400 and premature["code"] == "FIRST_RUN_REPLY_UNVERIFIED", premature
            _eventually(
                "user:<owner> bound to the local user agent",
                lambda: ipc("targets", {"kind": "user"})["targets"],
                lambda rows: any(
                    row.get("targetUri") == f"user:{OWNER}" and row.get("status") == "user-agent"
                    for row in rows
                ),
            )
            replied = ipc("message.reply", {
                "from": uri, "messageId": sent["messageId"],
                "summary": "Controlled reply", "message": "Controlled reply",
            })
            assert sent["replyPathUnavailable"] is True
            assert replied["replied"] is True
            assert replied["queued"] is False
            status, _, confirmed = http.request("POST", "/daemon/request/onboarding.firstRun.confirm", body=params)
            assert status == 200, confirmed
            result = confirmed["result"]
            assert result["replyMessageId"] == replied["replyMessageId"]
            assert result["firstRun"]["confirmed"] == ["configuration", "reply"]
            assert result["firstRun"]["revision"] == 1
            assert result["attempt"]["receiptRevision"] == 0
            status, _, repeated = http.request("POST", "/daemon/request/onboarding.firstRun.confirm", body=params)
            assert status == 200 and repeated["result"] == result, repeated
            _eventually(
                "reply to user:<owner> acknowledged",
                lambda: ipc("message.pending.list", {"from": f"user:{OWNER}"})["messages"],
                lambda pending: pending == [],
            )
            status, _, acknowledged = http.request("POST", "/daemon/request/onboarding.firstRun.confirm", body=params)
            assert status == 200 and acknowledged["result"] == result, acknowledged
            status, _, restored = http.request("POST", "/daemon/request/onboarding.status", body={})
            assert status == 200, restored
            assert restored["result"]["firstRunAttempt"]["state"] == "confirmed"
    finally:
        daemon.stop()
