"""The host outbox reader remains the sole source of registered results."""

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_METHODS
from tests.daemon.application.messaging.test_outbox_list_hold_reason import _item
from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app
from tests.daemon.ipc.contracts.routine.test_routine_plan import plan_socket as plan_socket
from tests.daemon.workers.test_worker_relay import isolated as isolated


class _Inbox:
    def outbox_items(self):
        return [_item("held"), _item("free")]


@pytest.mark.parametrize("origin", [None, "ipc", "worker"])
def test_registered_outbox_preserves_reader_and_ignores_extra_fields(app, origin):
    app._inbox = _Inbox()
    app._restore_done.set()
    expected = app._ipc_outbox_list()
    binding = METHOD_REGISTRY.get("outbox.list")
    assert binding is not None
    assert "outbox.list" in MIGRATED_METHODS
    assert not binding.spec.http_exposed
    actual = app.handle("outbox.list", {"actor": None, "sessionRef": [], "other": {"x": True}},
                        _trusted_message_origin=origin)
    assert actual == expected
    assert "holdReason" not in actual["entries"][0]
    assert "holdReason" not in actual["entries"][1]
    Draft202012Validator(binding.spec.result_schema).validate(actual)


def test_socket_preserves_wire_validation_and_restore(plan_socket):
    from tests.daemon.workers.test_worker_relay import frame, request

    plan_socket._inbox = _Inbox()
    good = request(plan_socket.socket_path, frame("outbox.list", {"actor": None}))
    assert good["result"] == plan_socket._ipc_outbox_list()
    invalid = request(plan_socket.socket_path, {"version": 1, "id": "bad", "method": "outbox.list", "params": []})
    from hyprial.daemon.impl.request_catalog.registry import daemon_openapi

    catalog = {entry["name"]: entry for entry in daemon_openapi()["x-hyprial-methods"]}
    example = next(case for case in catalog["outbox.list"]["x-hyprial-examples"]
                   if case["name"] == "non-object")
    assert example["params"] == []
    assert invalid["error"] == example["error"] == {"code": "INVALID_ARGUMENT", "message": "params must be an object"}
    plan_socket._restore_done.clear()
    try:
        restoring = request(plan_socket.socket_path, frame("outbox.list", {}))
        assert restoring["error"]["code"] == "DAEMON_RESTORING"
    finally:
        plan_socket._restore_done.set()


def test_worker_relay_remains_closed(isolated):
    from tests.daemon.workers.test_worker_relay import frame, request

    _, _, relay, _ = isolated
    result = request(relay.socket_path, frame("outbox.list", {}))
    assert result["error"]["code"] == "METHOD_NOT_FOUND"


def test_missing_binding_fails_closed(app, monkeypatch):
    from hyprial.kernel import DaemonRequestError

    app._inbox = _Inbox()
    app._restore_done.set()
    monkeypatch.delitem(METHOD_REGISTRY._bindings, "outbox.list")
    monkeypatch.setattr(app, "_ipc_outbox_list", lambda: pytest.fail("legacy handler reached"))
    with pytest.raises(DaemonRequestError) as error:
        app.handle("outbox.list", {})
    assert error.value.code == "METHOD_NOT_FOUND"


def test_registered_outbox_has_no_legacy_shadow_or_http_exposure():
    from pathlib import Path

    router = Path("src/hyprial/daemon/impl/ipc/router.py").read_text()
    assert 'if method == "outbox.list":' not in router
    assert METHOD_REGISTRY.get("outbox.list").spec.http_exposed is False


def test_empty_outbox_and_sqlite_failure_through_socket(plan_socket):
    import sqlite3

    from tests.daemon.workers.test_worker_relay import frame, request

    class Empty:
        def outbox_items(self):
            return ()

    class Unavailable:
        def outbox_items(self):
            raise sqlite3.OperationalError("outbox read failed")

    plan_socket._inbox = Empty()
    assert request(plan_socket.socket_path, frame("outbox.list", {}))["result"] == {"ok": True, "entries": []}
    plan_socket._inbox = Unavailable()
    assert request(plan_socket.socket_path, frame("outbox.list", {}))["error"] == {
        "code": "DAEMON_ERROR", "message": "outbox read failed"
    }
