"""App authorization operations use isolated, owned effects and verified grants."""

import json
import threading
import time

import pytest

import hyprial.daemon as daemon


def grants(state="authorized", app="cli_test"):
    return {"adapter": "test", "appId": app, "authorization": state,
            "capabilities": [], "checkedAt": 1000.0}


class Effect:
    def __init__(self, value=None):
        self.value = value
        self.closed = False

    def snapshot(self):
        return self.value

    def close(self):
        self.closed = True


class Registrar:
    def __init__(self):
        self.jobs = []
        self.inventory = grants()
        self.auth = Effect()

    def __call__(self, job):
        self.jobs.append(job)
        return Effect({"status": "completed", "authorization": self.inventory}) if job["action"] == "inventory" else self.auth


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / "secrets").mkdir(parents=True)
    (home / "channels.json").write_text(json.dumps({"version": 1, "gateways": [
        {"provider": "lark", "name": "test", "appId": "cli_test", "credentialRef": "lark-test", "routes": []},
    ]}))
    (home / "secrets" / "lark-test.json").write_text('{"appSecret":"fixture-secret-never-return"}')
    return home


@pytest.fixture
def service(home, tmp_path):
    registrar = Registrar()
    service = daemon.create_lark_authorization_service(home, tmp_path / "state", registrar=registrar)
    yield service, registrar
    service.close()


def eventually(service, operation_id, status):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = service.operation(operation_id)
        if result["status"] == status:
            return result
        time.sleep(0.01)
    pytest.fail(f"operation did not reach {status}: {result}")


def test_public_service_queries_app_grants_and_reuses_active_authorization(service):
    service, registrar = service
    assert service.status("test")["authorization"] == "authorized"
    first = service.start("test")
    second = service.start("test")
    assert first["operationId"] == second["operationId"]
    registrar.auth.value = {"status": "awaiting_user", "verificationUrl": "https://accounts.feishu.cn/authorize?x=1", "expiresIn": 600}
    pending = eventually(service, first["operationId"], "awaiting_user")
    assert pending["expiresAt"] > time.time()
    assert pending["authorization"]["authorization"] == "authorized"
    assert "fixture-secret" not in json.dumps(pending)
    registrar.auth.value = {"status": "completed", "authorization": grants()}
    complete = eventually(service, first["operationId"], "succeeded")
    assert "verificationUrl" not in complete
    assert registrar.auth.closed


@pytest.mark.parametrize("result", ["denied", "expired", "cancelled"])
def test_authorization_attempt_failure_does_not_revoke_retained_grants(service, result):
    service, registrar = service
    service.status("test")
    first = service.start("test")
    registrar.auth.value = {"status": result}
    complete = eventually(service, first["operationId"], result)
    assert complete["authorization"]["authorization"] == "authorized"


@pytest.mark.parametrize("state,expected", [("unauthorized", "failed"), ("unconfirmed", "unknown")])
def test_sdk_success_requires_independent_scope_readback(service, state, expected):
    service, registrar = service
    first = service.start("test")
    registrar.auth.value = {"status": "completed", "authorization": grants(state)}
    assert eventually(service, first["operationId"], expected)["authorization"]["authorization"] == state


def test_unknown_attempt_is_not_blindly_restarted(service):
    service, registrar = service
    first = service.start("test")
    registrar.auth.value = {"status": "unknown"}
    eventually(service, first["operationId"], "unknown")
    assert service.start("test")["operationId"] == first["operationId"]
    assert service.status("test")["authorization"] == "authorized"
    assert service.operation(first["operationId"])["status"] == "succeeded"


def test_cancel_and_close_stop_owned_effects_and_ignore_late_success(service):
    service, registrar = service
    first = service.start("test")
    registrar.auth.value = {"status": "awaiting_user", "verificationUrl": "https://accounts.feishu.cn/authorize", "expiresIn": 600}
    eventually(service, first["operationId"], "awaiting_user")
    assert service.cancel(first["operationId"])["status"] == "cancelled"
    assert registrar.auth.closed
    registrar.auth.value = {"status": "completed", "authorization": grants()}
    service.close()
    assert service.operation(first["operationId"])["status"] == "cancelled"
    with pytest.raises(RuntimeError):
        service.start("test")


def test_start_missing_app_never_launches_or_creates_one(service):
    service, registrar = service
    result = service.start("missing")
    assert result["status"] == "failed"
    assert result["authorization"]["authorization"] == "unconfigured"
    assert registrar.jobs == []


def test_inventory_and_startup_deadlines_close_hung_effects(home, tmp_path):
    effects = []

    def registrar(_job):
        effect = Effect()
        effects.append(effect)
        return effect

    service = daemon.create_lark_authorization_service(home, tmp_path, registrar=registrar,
        inventory_timeout=0.05, startup_timeout=0.05)
    try:
        assert service.status("test")["authorization"] == "unconfirmed"
        started = service.start("test")
        eventually(service, started["operationId"], "unknown")
        assert effects[-1].closed
        assert service.start("test")["operationId"] == started["operationId"]
        assert len(effects) == 2  # One inventory and one authorization; no restart.
    finally:
        service.close()
    assert all(effect.closed for effect in effects)


def test_expiry_and_untrusted_url_are_not_exposed(service):
    service, registrar = service
    started = service.start("test")
    registrar.auth.value = {"status": "awaiting_user", "verificationUrl": "javascript:alert(1)", "expiresIn": 600}
    result = eventually(service, started["operationId"], "failed")
    assert "verificationUrl" not in result


def test_concurrent_start_allocates_one_operation(service):
    service, registrar = service
    results = []
    workers = [threading.Thread(target=lambda: results.append(service.start("test"))) for _ in range(5)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)
    assert len({result["operationId"] for result in results}) == 1


def test_current_operation_recovers_active_and_completed_attempt(service):
    service, registrar = service
    assert service.current_operation("test") is None
    result = service.start("test")
    assert service.current_operation("test")["operationId"] == result["operationId"]
    registrar.auth.value = {"status": "completed", "authorization": grants()}
    eventually(service, result["operationId"], "succeeded")
    assert service.current_operation("test")["status"] == "succeeded"


def test_capacity_rejects_excess_authorization_without_unbounded_queue(home, tmp_path):
    configuration = json.loads((home / "channels.json").read_text())
    gateway = configuration["gateways"][0]
    configuration["gateways"] = [{**gateway, "name": f"app-{i}", "appId": f"cli_{i}", "credentialRef": f"lark-{i}"} for i in range(5)]
    for i in range(5):
        (home / "secrets" / f"lark-{i}.json").write_text('{"appSecret":"isolated-fixture"}')
    (home / "channels.json").write_text(json.dumps(configuration))
    service = daemon.create_lark_authorization_service(home, tmp_path, registrar=lambda _: Effect())
    try:
        operations = [service.start(f"app-{i}") for i in range(5)]
        assert operations[-1]["status"] == "failed"
        assert operations[-1]["errorCode"] == "AUTHORIZATION_BUSY"
    finally:
        service.close()


def test_cleanup_failure_is_unknown_and_secret_safe(home, tmp_path):
    class BrokenEffect(Effect):
        def close(self):
            raise RuntimeError("private-token-must-not-leak")

    service = daemon.create_lark_authorization_service(home, tmp_path,
        registrar=lambda _: BrokenEffect({"status": "awaiting_user", "verificationUrl": "https://accounts.feishu.cn/a", "expiresIn": 600}))
    started = service.start("test")
    eventually(service, started["operationId"], "awaiting_user")
    try:
        result = service.cancel(started["operationId"])
        assert result["status"] == "unknown"
        assert "private-token" not in str(result)
    finally:
        try:
            service.close()
        except RuntimeError as error:
            assert "private-token" not in str(error)
