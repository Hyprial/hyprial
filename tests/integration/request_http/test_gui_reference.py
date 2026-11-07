"""Browser reference slice against real isolated IPC/H-01 and the Contract Mock.

wf-61d46aa513404f45a938d9e764f8dd55#implGRS3 (revGRS2 finding a): opt-in GUI
acceptance. The module is marked ``gui_acceptance`` and excluded from the CI
pytest shards; run it explicitly with Node 24 on PATH and
DEV01_CHROMIUM_EXECUTABLE set:

    uv run pytest -m gui_acceptance tests/integration/request_http/test_gui_reference.py

Without the environment variables the run fails loudly (BLOCKED) and never
counts as PASS. The dedicated host entry uses the product App and components,
with synthetic login input only.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from hyprial.kernel import daemon_request_once

pytestmark = pytest.mark.gui_acceptance

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "contract/daemon-request"))

from contract_mock.contract import load_contract  # noqa: E402
from contract_mock.envelope import Envelope  # noqa: E402
from contract_mock.server import create_server  # noqa: E402
from contract_mock.service import MockService  # noqa: E402


def test_shared_app_against_live_and_contract_mock(isolation, http_server):
    node = shutil.which("node")
    assert node, "BLOCKED: Node 24 required"
    assert os.environ.get("DEV01_CHROMIUM_EXECUTABLE"), "BLOCKED: DEV01_CHROMIUM_EXECUTABLE required"
    def request(method, params):
        return daemon_request_once(method, params, socket_path=isolation.socket_path, timeout=10)
    request("agent.create", {"name": "worker"})
    request("agent.provider.default", {"provider": "claude"})
    request("agent.secret.grant", {"actor": "worker", "grantId": "reference-grant", "source": "agent-private",
                                  "entryId": "reference", "fieldName": "REFERENCE_TEST", "environmentNames": ["REFERENCE_TEST"], "revision": 1})
    contract = load_contract()
    token = Envelope.token()
    service = MockService(contract, Envelope(contract), scenario="success", token=token)
    server = create_server(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = isolation.root / "gui-reference.json"
    config.write_text(json.dumps({
        "live": {"endpoint": http_server.endpoint, "token": http_server.token, "principal": "isolated-operator", "driver": "daemon"},
        "mock-success": {"endpoint": f"http://127.0.0.1:{server.server_port}", "token": token, "principal": "synthetic-operator", "driver": "contract-mock"},
    }))
    config.chmod(0o600)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**isolation.env, "HYPRIAL_GUI_REFERENCE_CONFIG": str(config), "NO_PROXY": "127.0.0.1,localhost,::1"}
    env["no_proxy"] = env["NO_PROXY"]
    origin = f"http://127.0.0.1:{port}"
    with ExitStack() as stack:
        stack.callback(server.server_close)
        stack.callback(lambda: thread.join(timeout=5))
        stack.callback(server.shutdown)
        log = stack.enter_context((isolation.root / "gui.log").open("w"))
        process = subprocess.Popen([node, "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=ROOT / "gui", env=env, stdout=log, stderr=subprocess.STDOUT)
        def close_gui():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stack.callback(close_gui)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert process.poll() is None, (isolation.root / "gui.log").read_text()
            try:
                with urllib.request.urlopen(origin + "/__gui/healthz", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("GUI readiness timed out")
        result = subprocess.run([node, "tests/reference/browser.mjs", origin], cwd=ROOT / "gui", env=env,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr + (isolation.root / "gui.log").read_text()
        assert result.stdout.count('"verdict":"PASS"') == 2
        print(result.stdout)
    assert request("agent.provider.default", {})["provider"] == "claude"
