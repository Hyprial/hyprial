"""Log adapter/isolated registry tests; production HTTP registration is pending."""
import json
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.daemon.impl.request_catalog.maintenance.logs import LOG_BINDINGS

from hyprial.kernel import DaemonRequestError

METHOD_REGISTRY = MethodRegistry.from_bindings(LOG_BINDINGS)


@pytest.mark.parametrize("origin", [None, "ipc"])
def test_safe_projection_and_fixed_root(tmp_path, origin):
    logs = tmp_path / "logs"
    logs.mkdir()
    secret = {"ts": "2026-11-03T00:00:00Z", "level": "warn", "component": "PRIVATE_OWNER", "event": "/private/SECRET", "message": "SECRET", "context": {"token": "SECRET"}}
    (logs / "daemon.jsonl").write_text(json.dumps(secret) + "\n")
    app = SimpleNamespace(state_dir=tmp_path, epoch="test")
    page = METHOD_REGISTRY.invoke(app, "log.page", {}, trusted_message_origin=origin)
    assert page["entries"] == [{"timestamp": "2026-11-03T00:00:00+00:00", "level": "warn", "component": "other", "event": "other"}]
    assert "SECRET" not in json.dumps(page)


@pytest.mark.parametrize("params", [{"path": "/private"}, {"owner": "other"}, {"cursor": None}, {"limit": None}, {"limit": 0}, {"limit": 1001}, {"limit": True}])
def test_rejects_browser_scope_and_invalid_limits(tmp_path, params):
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(SimpleNamespace(state_dir=tmp_path, epoch="test"), "log.page", params)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_unavailable_and_expired_are_not_empty_success(tmp_path):
    app = SimpleNamespace(state_dir=tmp_path, epoch="test")
    page = METHOD_REGISTRY.invoke(app, "log.page", {})
    assert page["partial"]
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, "log.page", {"cursor": "expired"})
    assert caught.value.code == "LOG_CURSOR_INVALID"


@pytest.mark.parametrize("origin", ["worker", "agent:foreign:node:agent", "unknown"])
def test_forwarded_identity_cannot_read_operator_logs(tmp_path, origin):
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(SimpleNamespace(state_dir=tmp_path, epoch="test"), "log.page", {}, trusted_message_origin=origin)
    assert caught.value.code == "LOG_ACCESS_DENIED"
