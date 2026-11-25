"""Production registry and HTTP fail closed while onboarding is unassembled."""

import json

from hyprial.kernel import daemon_request_once
from tests.integration.request_http.conftest import DaemonProcess, Isolation, running_http


def test_registered_onboarding_is_not_advertised_ready_without_runtime(isolation: Isolation):
    # An ownerless home is the unassembled case: startup installs no serving
    # account port, so the catalog must not advertise onboarding.  The shared
    # isolation fixture binds an owner (a serving home), which does assemble.
    (isolation.home / "settings.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    daemon = DaemonProcess(isolation)
    daemon.start()
    try:
        catalog = daemon_request_once(
            "daemon.catalog", {}, socket_path=isolation.socket_path, timeout=5
        )
        methods = {item["name"]: item for item in catalog["methods"]}
        names = {
            "onboarding.status", "auth.login.begin", "auth.operation.get",
            "auth.operation.cancel", "onboarding.device.ensure", "onboarding.initialize",
        }
        assert names <= set(methods)
        for name in names:
            assert methods[name]["state"] == "disabled"
            assert methods[name]["callable"] is False
            assert methods[name]["reason"] is None
        with running_http(isolation) as http:
            status, _, response = http.request(
                "POST", "/daemon/request/auth.login.begin",
                body={"intent": "login", "operationKey": "opkey-0123456789abcdef"},
            )
            assert status == 404, response
            assert response["code"] == "METHOD_NOT_FOUND"
            assert response["dispatch"] == "not-dispatched"
            assert "result" not in response
    finally:
        daemon.stop()
