"""Authorization admission is durable before effects and never widens consent."""

import json
import time

import pytest

from hyprial.daemon.impl.adapters.lark.credentials.authorization.service import LarkAuthorizationService
from tests.daemon.adapters.lark.identity.authorization.test_service import Registrar, eventually, home as home_fixture

home = home_fixture

REQUIRED = ("message-send", "receive-p2p", "receive-group-mention")


def make_service(home, tmp_path, registrar):
    return LarkAuthorizationService(home, tmp_path / "state", registrar=registrar,
                                    receipt_path=tmp_path / "receipts" / "authorization.json")


def test_operation_key_reuses_terminal_receipt_without_new_effect(home, tmp_path):
    registrar = Registrar()
    service = make_service(home, tmp_path, registrar)
    try:
        first = service.start("test", capabilities=REQUIRED, operation_key="first")
        registrar.auth.value = {"status": "denied"}
        eventually(service, first["operationId"], "denied")
        replay = service.start("test", capabilities=REQUIRED, operation_key="first")
        assert replay["operationId"] == first["operationId"]
        assert replay["status"] == "denied"
        assert len(registrar.jobs) == 1
        with pytest.raises(ValueError, match="operation key"):
            service.start("test", capabilities=(*REQUIRED, "chat-read"), operation_key="first")
    finally:
        service.close()


def test_selected_permissions_reach_worker_without_implicit_extra_grants(home, tmp_path):
    registrar = Registrar()
    service = make_service(home, tmp_path, registrar)
    try:
        started = service.start("test", capabilities=(*REQUIRED, "chat-read"), operation_key="first")
        registrar.auth.value = {"status": "completed", "authorization": {
            "adapter": "test", "appId": "cli_test", "authorization": "authorized",
            "capabilities": list(REQUIRED), "missingCapabilities": [], "checkedAt": 1000,
        }}
        result = eventually(service, started["operationId"], "failed")
        assert result["errorCode"] == "REQUESTED_SCOPES_MISSING"
        assert set(registrar.jobs[0]["capabilities"]) == {*REQUIRED, "chat-read"}
        assert "group-history" not in registrar.jobs[0]["capabilities"]
    finally:
        service.close()


def test_recovery_keeps_unknown_receipt_and_does_not_launch(home, tmp_path):
    registrar = Registrar()
    service = make_service(home, tmp_path, registrar)
    first = service.start("test", capabilities=REQUIRED, operation_key="first")
    registrar.auth.value = {"status": "unknown"}
    eventually(service, first["operationId"], "unknown")
    path = tmp_path / "receipts" / "authorization.json"
    before_close = path.read_bytes()
    service.close()
    path.write_bytes(before_close)
    second_registrar = Registrar()
    recovered = make_service(home, tmp_path, second_registrar)
    try:
        assert recovered.operation(first["operationId"])["status"] == "unknown"
        again = recovered.start("test", capabilities=REQUIRED, operation_key="different-key")
        assert again["operationId"] == first["operationId"]
        assert second_registrar.jobs == []
        assert "fixture-secret" not in path.read_text()
        assert "verificationUrl" not in json.loads(path.read_text())["operations"][0]
    finally:
        recovered.close()


@pytest.mark.parametrize("capabilities", [(), ("chat-read",), (*REQUIRED, "invented")])
def test_invalid_consent_never_starts_worker(home, tmp_path, capabilities):
    registrar = Registrar()
    service = make_service(home, tmp_path, registrar)
    try:
        with pytest.raises(ValueError):
            service.start("test", capabilities=capabilities, operation_key="first")
        assert registrar.jobs == []
    finally:
        service.close()


def test_receipt_failure_before_admission_does_not_start_worker(home, tmp_path, monkeypatch):
    from hyprial.daemon.impl.adapters.lark.credentials.authorization import receipts

    registrar = Registrar()
    service = make_service(home, tmp_path, registrar)
    def unavailable(*args, **kwargs):
        raise OSError("synthetic private failure")
    monkeypatch.setattr(receipts, "write_private_file_atomic", unavailable)
    try:
        with pytest.raises(RuntimeError, match="receipt"):
            service.start("test", capabilities=REQUIRED, operation_key="first")
        assert registrar.jobs == []
    finally:
        service.close()


def test_cancel_reaps_worker_even_if_receipt_storage_fails(home, tmp_path, monkeypatch):
    from hyprial.daemon.impl.adapters.lark.credentials.authorization import receipts

    registrar = Registrar()
    service = make_service(home, tmp_path, registrar)
    first = service.start("test", capabilities=REQUIRED, operation_key="first")
    deadline = time.monotonic() + 3
    while not registrar.jobs and time.monotonic() < deadline:
        time.sleep(.01)
    def unavailable(*args, **kwargs):
        raise OSError("synthetic private failure")
    monkeypatch.setattr(receipts, "write_private_file_atomic", unavailable)
    try:
        with pytest.raises(RuntimeError, match="receipt"):
            service.cancel(first["operationId"])
        assert registrar.auth.closed
        assert service.operation(first["operationId"])["status"] == "unknown"
    finally:
        service.close()


@pytest.mark.parametrize("state", ["starting", "awaiting_user", "checking"])
def test_recovery_of_an_interrupted_operation_never_restores_handoff(home, tmp_path, state):
    from hyprial.daemon.impl.adapters.lark.credentials.authorization.receipts import AuthorizationReceipts

    path = tmp_path / "receipts" / "authorization.json"
    AuthorizationReceipts(path).save({"id": {
        "operationId": "id", "operationKey": "first", "adapter": "test", "appId": "cli_test", "status": state,
        "authorization": {"adapter": "test", "appId": "cli_test", "authorization": "unconfirmed", "capabilities": [], "checkedAt": 1000},
        "capabilities": list(REQUIRED), "verificationUrl": "https://accounts.feishu.cn/authorize", "expiresAt": time.time() + 600,
    }})
    registrar = Registrar()
    service = make_service(home, tmp_path, registrar)
    try:
        result = service.operation("id")
        assert result["status"] == "unknown" and "verificationUrl" not in result
        assert registrar.jobs == []
    finally:
        service.close()


def test_worker_passes_only_explicit_capabilities_to_existing_library(home, tmp_path, monkeypatch):
    from hyprial.daemon.impl.adapters.lark.credentials.authorization import worker
    from hyprial.daemon.impl.adapters.lark.credentials import onboarding

    requested = []
    monkeypatch.setattr(onboarding, "reauthorize_lark_app", lambda **kwargs: requested.extend(kwargs["capabilities"]))
    monkeypatch.setattr(worker, "scope_inventory", lambda *args: {"appId": "cli_test"})
    worker.run({"home": str(home), "stateDir": str(tmp_path), "action": "authorize", "adapter": "test",
                "appId": "cli_test", "capabilities": [*REQUIRED, "chat-read"]}, lambda value: None)
    assert requested == [*REQUIRED, "chat-read"]
