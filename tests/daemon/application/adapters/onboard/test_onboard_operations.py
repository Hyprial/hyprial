"""New app creation is an owned daemon operation, never a CLI subprocess."""

import json
import time

import pytest

from hyprial.kernel import DaemonRequestError
from tests.daemon.application.adapters.authorization.test_status import app as app_fixture

app = app_fixture


class Effect:
    def __init__(self):
        self.value = None
        self.closed = False

    def snapshot(self):
        return self.value

    def close(self):
        self.closed = True


@pytest.fixture
def factory(monkeypatch):
    from hyprial.daemon.impl.adapters.lark.credentials.onboard import service

    effects = []

    def create(job):
        assert job["action"] == "onboard"
        effect = Effect()
        effects.append(effect)
        return effect

    monkeypatch.setattr(service, "AuthorizationEffect", create)
    return effects


def begin(app, name="new-demo", key="create-key"):
    return app.handle("adapter.onboard.begin", {"provider": "lark", "name": name, "operationKey": key})


def await_status(app, identity, wanted):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = app.handle("adapter.onboard.operation", {"operationId": identity})
        if result["status"] in wanted:
            return result
        time.sleep(.01)
    raise AssertionError(result)


def await_effect(factory):
    deadline = time.monotonic() + 3
    while not factory and time.monotonic() < deadline:
        time.sleep(.01)
    assert factory
    return factory[-1]


def test_new_app_is_saved_once_and_never_exposes_secret(app, factory):
    result = begin(app)
    effect = await_effect(factory)
    effect.value = {"status": "awaiting_user", "verificationUrl": "https://accounts.feishu.cn/authorize?public=1", "expiresIn": 600}
    prompt = await_status(app, result["operationId"], {"awaiting_user"})
    assert prompt["expiresAt"] > time.time() * 1000
    assert begin(app)["operationId"] == result["operationId"]
    effect.value = {"status": "completed", "credential": {"appId": "cli_created", "appSecret": "synthetic-onboard-secret"}}
    saved = await_status(app, result["operationId"], {"succeeded"})
    assert saved["configured"] is True
    assert saved["appId"] == "cli_created"
    assert "synthetic-onboard-secret" not in json.dumps(saved)
    assert "verificationUrl" not in saved
    assert begin(app)["operationId"] == result["operationId"]
    assert effect.closed and len(factory) == 1
    assert app.handle("adapter.onboard.operation", {"name": "new-demo"}) == saved
    journal = app.state_dir / "adapter-onboard" / "receipts.json"
    assert "synthetic-onboard-secret" not in journal.read_text()
    assert "verificationUrl" not in journal.read_text()


def test_cancel_reaps_worker_without_claiming_remote_rollback(app, factory):
    started = begin(app)
    effect = await_effect(factory)
    result = app.handle("adapter.onboard.cancel", {"operationId": started["operationId"]})
    assert result["status"] == "cancelled"
    assert result["remoteOutcomeUnknown"] is True
    assert effect.closed
    assert begin(app)["status"] == "cancelled"
    assert not (app.hyprial_home / "secrets/lark-new-demo.json").exists()


@pytest.mark.parametrize("params,origin,code", [
    ({"provider": "lark", "name": "new-demo", "operationKey": "x"}, "worker", "CALLER_NOT_AUTHORIZED"),
    ({"provider": "lark", "name": "demo", "operationKey": "x"}, None, "ADAPTER_EXISTS"),
    ({"provider": "lark", "name": "../bad", "operationKey": "x"}, None, "INVALID_ARGUMENT"),
    ({"provider": "lark", "name": "new-demo", "operationKey": "x", "actor": "forged"}, None, "INVALID_ARGUMENT"),
])
def test_admission_precedes_creation(app, factory, params, origin, code):
    with pytest.raises(DaemonRequestError) as error:
        app.handle("adapter.onboard.begin", params, _trusted_message_origin=origin)
    assert error.value.code == code
    assert not factory


def test_name_collision_after_remote_creation_never_overwrites(app, factory):
    started = begin(app)
    effect = await_effect(factory)
    app.handle("adapter.register", {"provider": "lark", "name": "new-demo", "appId": "cli_other", "appSecret": "other-synthetic"})
    effect.value = {"status": "completed", "credential": {"appId": "cli_created", "appSecret": "created-synthetic"}}
    result = await_status(app, started["operationId"], {"unknown"})
    assert result["configured"] is False
    assert result["appId"] == "cli_created"
    assert result["errorCode"] == "APP_CREATED_NOT_SAVED"
    assert "cli_other" in (app.hyprial_home / "channels.json").read_text()
    assert begin(app)["operationId"] == started["operationId"]


def test_restarted_receipt_does_not_restart_remote_creation(app, factory):
    from hyprial.daemon.impl.adapters.lark.credentials.onboard.service import LarkOnboardingService

    started = begin(app)
    effect = await_effect(factory)
    app._close_adapter_operations()
    assert effect.closed
    service = LarkOnboardingService(app.hyprial_home, app.state_dir, register=lambda *_: pytest.fail("must not commit"))
    try:
        result = service.current_operation("new-demo")
        assert result["operationId"] == started["operationId"]
        assert result["status"] == "unknown"
        assert service.start("new-demo", "another-key")["operationId"] == result["operationId"]
        assert len(factory) == 1
    finally:
        service.close()


def test_receipt_failure_before_effect_fences_creation(app, factory, monkeypatch):
    from hyprial.daemon.impl.adapters.lark.credentials.onboard import journal

    def fail(*_args, **_kwargs):
        raise OSError("synthetic failure")

    monkeypatch.setattr(journal, "write_private_file_atomic", fail)
    with pytest.raises(DaemonRequestError) as error:
        begin(app)
    assert error.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert not factory
    with pytest.raises(DaemonRequestError):
        begin(app, key="different-key")
    assert not factory


def test_cancel_write_failure_still_reaps_worker(app, factory, monkeypatch):
    from hyprial.daemon.impl.adapters.lark.credentials.onboard import journal

    started = begin(app)
    effect = await_effect(factory)

    def fail(*_args, **_kwargs):
        raise OSError("synthetic failure")

    monkeypatch.setattr(journal, "write_private_file_atomic", fail)
    with pytest.raises(DaemonRequestError) as error:
        app.handle("adapter.onboard.cancel", {"operationId": started["operationId"]})
    assert error.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert effect.closed
    result = app.handle("adapter.onboard.operation", {"operationId": started["operationId"]})
    assert result["status"] == "unknown"


def test_worker_uses_official_creation_and_ignores_ambient_app(monkeypatch):
    import lark_oapi
    from hyprial.daemon.impl.adapters.lark.credentials.authorization.worker import run

    calls = []
    published = []

    def register(callback, **kwargs):
        calls.append(kwargs)
        callback({"url": "https://accounts.feishu.cn/authorize", "expire_in": 600})
        return {"client_id": "cli_created", "client_secret": "synthetic-created"}

    monkeypatch.setenv("HYPRIAL_LARK_APP_ID", "cli_ambient")
    monkeypatch.setenv("HYPRIAL_LARK_APP_SECRET", "synthetic-ambient")
    monkeypatch.setattr(lark_oapi, "register_app", register)
    run({"action": "onboard", "adapter": "new-demo"}, published.append)
    assert len(calls) == 1 and calls[0]["create_only"] is True
    assert "app_id" not in calls[0]
    assert published[-1]["credential"]["appId"] == "cli_created"
    assert published[0]["status"] == "awaiting_user"


def test_uncertain_local_save_is_not_reported_as_definitely_unsaved(app, factory, monkeypatch):
    def uncertain(*_args):
        raise DaemonRequestError("SUBMIT_OUTCOME_UNKNOWN", "synthetic uncertain save")

    monkeypatch.setattr(app, "_register_adapter_configuration", uncertain)
    started = begin(app)
    effect = await_effect(factory)
    effect.value = {"status": "completed", "credential": {"appId": "cli_created", "appSecret": "synthetic-only"}}
    result = await_status(app, started["operationId"], {"unknown"})
    assert result["errorCode"] == "APP_CONFIGURATION_UNCONFIRMED"
    assert result["nextAction"] == "inspect"
    assert begin(app, key="second-key")["operationId"] == started["operationId"]
    assert len(factory) == 1


def test_worker_cleanup_failure_cannot_claim_completion(app, factory, monkeypatch):
    started = begin(app)
    effect = await_effect(factory)

    def refuse():
        raise RuntimeError("synthetic custody failure")

    monkeypatch.setattr(effect, "close", refuse)
    effect.value = {"status": "completed", "credential": {"appId": "cli_created", "appSecret": "synthetic-only"}}
    result = await_status(app, started["operationId"], {"unknown"})
    assert result["configured"] is True
    assert result["errorCode"] == "APP_WORKER_CLEANUP_UNCONFIRMED"
    assert begin(app, key="second-key")["operationId"] == started["operationId"]
    monkeypatch.setattr(effect, "close", lambda: setattr(effect, "closed", True))
