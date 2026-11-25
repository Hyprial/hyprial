"""The unified request path owns authorization effects and durable receipts."""

import time

import pytest

from hyprial.daemon.impl.adapters.lark.credentials.authorization import service as authorization
from hyprial.kernel import DaemonRequestError
from tests.daemon.application.adapters.authorization.test_status import app as app_fixture
from tests.daemon.adapters.lark.identity.authorization.test_service import Registrar

app = app_fixture

CAPABILITIES = ["message-send", "receive-p2p", "receive-group-mention"]


@pytest.fixture
def registrar(monkeypatch):
    value = Registrar()
    monkeypatch.setattr(authorization, "AuthorizationEffect", value)
    return value


def begin(app, key="test-key"):
    return app.handle("adapter.authorization.begin", {"name": "demo", "capabilities": CAPABILITIES, "operationKey": key})


def test_request_begin_poll_cancel_keeps_one_worker(app, registrar):
    first = begin(app)
    registrar.auth.value = {"status": "awaiting_user", "verificationUrl": "https://accounts.feishu.cn/authorize?public=1", "expiresIn": 600}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = app.handle("adapter.authorization.operation", {"operationId": first["operationId"]})
        if result["status"] == "awaiting_user":
            break
        time.sleep(.01)
    assert result["status"] == "awaiting_user"
    assert result["expiresAt"] > time.time() * 1000
    assert "operationKey" not in result
    assert begin(app)["operationId"] == first["operationId"]
    ended = app.handle("adapter.authorization.cancel", {"operationId": first["operationId"]})
    assert ended["status"] == "cancelled"
    assert registrar.auth.closed and len(registrar.jobs) == 1
    assert begin(app)["status"] == "cancelled"
    assert "synthetic-only" not in str(ended)


@pytest.mark.parametrize("method,params,origin,code", [
    ("begin", {"name": "demo", "capabilities": CAPABILITIES, "operationKey": "x"}, "worker", "CALLER_NOT_AUTHORIZED"),
    ("begin", {"name": "absent", "capabilities": CAPABILITIES, "operationKey": "x"}, None, "ADAPTER_NOT_FOUND"),
    ("begin", {"name": "demo", "capabilities": ["chat-read"], "operationKey": "x"}, None, "INVALID_ARGUMENT"),
    ("operation", {"operationId": "missing"}, "worker", "CALLER_NOT_AUTHORIZED"),
    ("cancel", {"operationId": "missing"}, "worker", "CALLER_NOT_AUTHORIZED"),
    ("begin", {"name": "demo", "capabilities": CAPABILITIES, "operationKey": "x", "actor": "forged"}, None, "INVALID_ARGUMENT"),
])
def test_admission_precedes_effect(app, registrar, method, params, origin, code):
    with pytest.raises(DaemonRequestError) as error:
        app.handle("adapter.authorization." + method, params, _trusted_message_origin=origin)
    assert error.value.code == code
    assert registrar.jobs == []


def test_daemon_close_reaps_pending_authorization(app, registrar):
    begin(app)
    deadline = time.monotonic() + 3
    while not registrar.jobs and time.monotonic() < deadline:
        time.sleep(.01)
    app._close()
    assert registrar.auth.closed
