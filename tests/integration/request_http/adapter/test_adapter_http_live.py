"""Real isolated HTTP entry tests for configured adapter lifecycle writes."""

from __future__ import annotations

import json
import sys
import time
import urllib.error

import pytest

from tests.integration.request_http.conftest import DaemonProcess, running_http


@pytest.fixture
def configured_adapter_http(isolation):
    channels = {
        "version": 1,
        "gateways": [
            {"provider": "lark", "name": name, "appId": f"app-{name}",
             "credentialRef": f"lark-{name}", "routes": []}
            for name in ("ready", "fail", "slow")
        ],
    }
    (isolation.home / "channels.json").write_text(json.dumps(channels), encoding="utf-8")
    secret_dir = isolation.home / "secrets"
    secret_dir.mkdir()
    for name in ("ready", "fail", "slow"):
        (secret_dir / f"lark-{name}.json").write_text(
            json.dumps({"appSecret": f"isolated-secret-{name}"}), encoding="utf-8",
        )
    worker = isolation.root / "adapter-worker.py"
    worker.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "name = sys.argv[1]\n"
        "with Path(os.environ['HYPRIAL_TEST_MARKER']).open('a') as marker: marker.write(name + '\\n')\n"
        "if name == 'fail': sys.exit(2)\n"
        "if name == 'slow': time.sleep(1)\n"
        "os.write(int(os.environ['HYPRIAL_LARK_READY_FD']), json.dumps({'status': 'online', 'name': name}).encode() + b'\\n')\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    isolation.env["HYPRIAL_LARK_WORKER_COMMAND"] = json.dumps([sys.executable, str(worker)])
    marker = isolation.root / "adapter-starts.txt"
    isolation.env["HYPRIAL_TEST_MARKER"] = str(marker)
    daemon = DaemonProcess(isolation)
    try:
        daemon.start()
    except AssertionError as error:
        events = [line for path in isolation.root.rglob("daemon.jsonl")
                  for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                  if '"event":"daemon.start.failed"' in line]
        raise AssertionError(f"{error}\n{events}") from error
    try:
        with running_http(isolation) as http:
            yield http, marker
    finally:
        daemon.stop()


def test_adapter_lifecycle_http_reaches_isolated_real_daemon(http_server) -> None:
    status, _, catalog = http_server.request("GET", "/daemon/request")
    assert status == 200
    methods = {item["name"]: item for item in catalog["methods"]}
    assert all(methods[name]["mutates"] is True and methods[name]["retry"] == "never"
               for name in ("adapter.start", "adapter.stop", "adapter.reload"))
    assert all(methods[name]["state"] == "available"
               for name in ("adapter.start", "adapter.stop", "adapter.reload"))

    status, _, refused = http_server.request(
        "POST", "/daemon/request/adapter.start", body={"name": "missing"}, token="wrong",
    )
    assert (status, refused["code"], refused["dispatch"]) == (
        401, "HTTP_UNAUTHORIZED", "not-dispatched",
    )

    for name in ("adapter.start", "adapter.stop"):
        status, _, result = http_server.request(
            "POST", f"/daemon/request/{name}", body={"name": "missing"},
        )
        assert (status, result["code"], result["dispatch"]) == (
            404, "ADAPTER_NOT_FOUND", "dispatched",
        )

    status, _, reloaded = http_server.request(
        "POST", "/daemon/request/adapter.reload", body={},
    )
    assert status == 200, reloaded
    assert reloaded["dispatch"] == "dispatched"
    assert reloaded["result"] == {"ok": True, "added": [], "updated": [],
                                  "removed": [], "removedRunning": []}


def test_configured_adapter_start_status_stop_and_failure(configured_adapter_http) -> None:
    http, marker = configured_adapter_http
    status, _, started = http.request(
        "POST", "/daemon/request/adapter.start", body={"name": "ready"},
    )
    assert status == 200, started
    assert started["dispatch"] == "dispatched"
    assert started["result"]["changed"] is True
    assert started["result"]["adapter"]["online"] is True

    status, _, observed = http.request(
        "POST", "/daemon/request/adapter.status", body={"name": "ready"},
    )
    assert status == 200, observed
    assert observed["result"]["adapter"]["online"] is True

    status, _, repeated = http.request(
        "POST", "/daemon/request/adapter.start", body={"name": "ready"},
    )
    assert status == 200, repeated
    assert repeated["result"]["changed"] is False
    assert marker.read_text(encoding="utf-8").splitlines().count("ready") == 1

    status, _, stopped = http.request(
        "POST", "/daemon/request/adapter.stop", body={"name": "ready"},
    )
    assert status == 200, stopped
    assert stopped["result"]["changed"] is True
    assert stopped["result"]["adapter"]["online"] is False

    status, _, failed = http.request(
        "POST", "/daemon/request/adapter.start", body={"name": "fail"},
    )
    assert status >= 400, failed
    assert failed["dispatch"] == "dispatched"
    assert failed["code"] in {"ADAPTER_START_FAILED", "ADAPTER_START_TIMEOUT"}
    assert marker.read_text(encoding="utf-8").splitlines().count("fail") == 1


def test_configured_adapter_transport_timeout_stays_unknown_until_status(
    configured_adapter_http,
) -> None:
    http, marker = configured_adapter_http
    with pytest.raises((TimeoutError, urllib.error.URLError)):
        http.request(
            "POST", "/daemon/request/adapter.start", body={"name": "slow"},
            timeout=0.1,
        )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, _, observed = http.request(
            "POST", "/daemon/request/adapter.status", body={"name": "slow"},
        )
        if status == 200 and observed["result"]["adapter"]["online"] is True:
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed-out write never became observable through adapter.status")
    assert marker.read_text(encoding="utf-8").splitlines().count("slow") == 1
    status, _, stopped = http.request(
        "POST", "/daemon/request/adapter.stop", body={"name": "slow"},
    )
    assert status == 200, stopped
