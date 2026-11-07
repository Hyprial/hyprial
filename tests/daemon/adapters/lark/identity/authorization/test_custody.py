"""App changes and cleanup failures cannot lose ownership or exceed capacity."""

import json

import pytest

import hyprial.daemon as daemon
from tests.daemon.adapters.lark.identity.authorization.test_service import (
    Effect, Registrar, eventually, grants, home as home,
)


def configure_app(home, app):
    path = home / "channels.json"
    value = json.loads(path.read_text())
    value["gateways"][0]["appId"] = app
    path.write_text(json.dumps(value))


class UncertainEffect(Effect):
    def __init__(self, value=None):
        super().__init__(value)
        self.fail_cleanup = True
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.fail_cleanup:
            raise RuntimeError("private-cleanup-error")
        super().close()


@pytest.mark.parametrize("phase", ["unknown", "awaiting_user"])
def test_changed_app_does_not_reuse_previous_attempt(home, tmp_path, phase):
    registrar = Registrar()
    registrar.auth.value = {"status": phase, "verificationUrl": "https://accounts.feishu.cn/a", "expiresIn": 600}
    service = daemon.create_lark_authorization_service(home, tmp_path, registrar=registrar)
    try:
        first = service.start("test")
        eventually(service, first["operationId"], phase)
        configure_app(home, "cli_changed")
        previous = registrar.auth
        registrar.auth = Effect({"status": "awaiting_user", "verificationUrl": "https://accounts.feishu.cn/b", "expiresIn": 600})
        second = service.start("test")
        assert second["operationId"] != first["operationId"]
        assert second["appId"] == "cli_changed"
        assert previous.closed
        eventually(service, second["operationId"], "awaiting_user")
        assert registrar.jobs[-1]["appId"] == "cli_changed"
    finally:
        service.close()


def test_changed_app_retries_old_cleanup_before_launching_new_worker(home, tmp_path):
    registrar = Registrar()
    previous = registrar.auth = UncertainEffect({"status": "unknown"})
    service = daemon.create_lark_authorization_service(home, tmp_path, registrar=registrar)
    try:
        first = service.start("test")
        eventually(service, first["operationId"], "unknown")
        configure_app(home, "cli_changed")
        registrar.auth = Effect()
        blocked = service.start("test")
        assert blocked["status"] == "unknown"
        assert blocked["errorCode"] == "WORKER_CLEANUP_UNCONFIRMED"
        assert previous.close_calls >= 2
        assert len(registrar.jobs) == 1
        previous.fail_cleanup = False
        second = service.start("test")
        assert previous.closed
        assert second["appId"] == "cli_changed"
        assert second["operationId"] != first["operationId"]
        assert service.operation(first["operationId"])["status"] == "cancelled"
    finally:
        previous.fail_cleanup = False
        service.close()


def test_restored_app_cleans_newer_app_before_starting_a_fresh_attempt(home, tmp_path):
    registrar = Registrar()
    registrar.auth.value = {"status": "unknown"}
    service = daemon.create_lark_authorization_service(home, tmp_path, registrar=registrar)
    try:
        first = service.start("test")
        eventually(service, first["operationId"], "unknown")
        configure_app(home, "cli_changed")
        registrar.auth = Effect({"status": "awaiting_user", "verificationUrl": "https://accounts.feishu.cn/b", "expiresIn": 600})
        second = service.start("test")
        eventually(service, second["operationId"], "awaiting_user")
        configure_app(home, "cli_test")
        previous = registrar.auth
        registrar.auth = Effect()
        third = service.start("test")
        assert third["operationId"] not in {first["operationId"], second["operationId"]}
        assert third["appId"] == "cli_test"
        assert previous.closed
        assert service.operation(first["operationId"])["status"] == "cancelled"
        assert service.operation(second["operationId"])["status"] == "cancelled"
    finally:
        service.close()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_cancel_unknown_proves_cleanup_before_new_app_can_start(home, tmp_path, cleanup_fails):
    registrar = Registrar()
    previous = registrar.auth = UncertainEffect({"status": "unknown"})
    service = daemon.create_lark_authorization_service(home, tmp_path, registrar=registrar)
    try:
        observed = service.status("test")
        first = service.start("test")
        eventually(service, first["operationId"], "unknown")
        configure_app(home, "cli_changed")
        registrar.auth = Effect({"status": "awaiting_user", "verificationUrl": "https://accounts.feishu.cn/b", "expiresIn": 600})
        assert service.start("test")["operationId"] == first["operationId"]
        previous.fail_cleanup = cleanup_fails
        cancelled = service.cancel(first["operationId"])
        assert cancelled["authorization"] == observed
        assert service.current_operation("test") == cancelled
        assert len([job for job in registrar.jobs if job["action"] == "authorize"]) == 1
        if cleanup_fails:
            assert cancelled["status"] == "unknown"
            assert cancelled["errorCode"] == "WORKER_CLEANUP_UNCONFIRMED"
            assert service.start("test")["operationId"] == first["operationId"]
            assert not previous.closed
        else:
            assert cancelled["status"] == "cancelled"
            assert cancelled["errorCode"] == "AUTHORIZATION_CANCELLED"
            assert previous.closed
            second = service.start("test")
            assert second["appId"] == "cli_changed"
            assert second["operationId"] != first["operationId"]
            eventually(service, second["operationId"], "awaiting_user")
    finally:
        previous.fail_cleanup = False
        service.close()


def test_fresh_grants_cannot_reconcile_attempt_with_uncertain_custody(home, tmp_path):
    registrar = Registrar()
    effect = registrar.auth = UncertainEffect({"status": "unknown"})
    service = daemon.create_lark_authorization_service(home, tmp_path, registrar=registrar)
    try:
        first = service.start("test")
        eventually(service, first["operationId"], "unknown")
        assert service.status("test")["authorization"] == "authorized"
        operation = service.operation(first["operationId"])
        assert operation["authorization"]["authorization"] == "authorized"
        assert operation["status"] == "unknown"
        assert operation["errorCode"] == "WORKER_CLEANUP_UNCONFIRMED"
        assert service.start("test")["operationId"] == first["operationId"]
        assert len([job for job in registrar.jobs if job["action"] == "authorize"]) == 1
    finally:
        effect.fail_cleanup = False
        service.close()
    assert effect.closed


@pytest.mark.parametrize("action", ["authorize", "inventory"])
def test_uncertain_workers_keep_capacity_and_close_retries_all(home, tmp_path, action):
    handles = []

    def registrar(job):
        value = ({"status": "completed", "authorization": grants(app=job["appId"] or "cli_test")}
                 if action == "inventory" else {"status": "unknown"})
        effect = UncertainEffect(value)
        handles.append(effect)
        return effect

    service = daemon.create_lark_authorization_service(home, tmp_path, registrar=registrar)
    try:
        for i in range(4):
            if action == "authorize":
                # Independent adapters reserve separate operation ownership.
                config = json.loads((home / "channels.json").read_text())
                entry = config["gateways"][0]
                config["gateways"] = [{**entry, "name": f"test-{i}"}]
                (home / "channels.json").write_text(json.dumps(config))
                op = service.start(f"test-{i}")
                eventually(service, op["operationId"], "unknown")
            else:
                assert service.status("test")["reason"] == "WORKER_CLEANUP_UNCONFIRMED"
        assert len(handles) == 4
        assert service.status("test")["reason"] == "AUTHORIZATION_BUSY"
        assert len(handles) == 4
        with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
            service.close()
        for effect in handles:
            assert effect.close_calls >= 2
            effect.fail_cleanup = False
        service.close()
        assert all(effect.closed for effect in handles)
    finally:
        for effect in handles:
            effect.fail_cleanup = False
        service.close()
