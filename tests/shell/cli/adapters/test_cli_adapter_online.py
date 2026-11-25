"""Real daemon IPC regressions for adapter registration and removal."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import assert_root_local_environment, isolated_environment

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hyprial.daemon.impl.bootstrap.adapter_registration import RouteInput, add_lark_gateway

ROOT = Path(__file__).resolve().parents[4]


def _cli(environment: dict[str, str], *args: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "hyprial.cli", *args, "--json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@contextmanager
def _online_daemon(*, seed_adapter: bool = False) -> Iterator[tuple[Path, dict[str, str]]]:
    with tempfile.TemporaryDirectory(
        prefix="hyprial-adapter-online-", dir=hyprial_test_tmp_root()
    ) as raw:
        home = Path(raw)
        if seed_adapter:
            add_lark_gateway(
                hyprial_home=home,
                name="feishu",
                app_id="cli_app_123",
                app_secret="synthetic-test-secret",
                routes=[RouteInput(name="dm", native_id="oc_native_1")],
            )
        environment = isolated_environment(home)
        environment.update(
            HYPRIAL_HOME=str(home),
            HARNESS_STATE_DIR=str(home),
            HYPRIAL_OWNER="adapter-online-test",
            HYPRIAL_NODE_ID="adapter-online-node",
            HYPRIAL_NETWORK_ISOLATED="1",
            HYPRIAL_LARK_BASE_URL="http://127.0.0.1:9",
            HYPRIAL_PEER_DISCOVERY="0",
            HYPRIAL_ZENOH_LISTEN="",
            HYPRIAL_ZENOH_CONNECT="",
        )
        environment.pop("HYPRIAL_LARK_APP_ID", None)
        environment.pop("HYPRIAL_LARK_APP_SECRET", None)
        assert_root_local_environment(home, environment)
        daemon = subprocess.Popen(
            [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 30
            while not (home / "daemon.sock").exists():
                assert daemon.poll() is None, "isolated daemon exited during startup"
                assert time.monotonic() < deadline, "isolated daemon did not start"
                time.sleep(0.05)
            yield home, environment
        finally:
            if daemon.poll() is None:
                os.kill(daemon.pid, signal.SIGTERM)
            daemon.wait(timeout=20)


def test_onboard_refreshes_running_daemon() -> None:
    with _online_daemon() as (home, environment):
        environment["HYPRIAL_LARK_APP_ID"] = "cli_app_123"
        environment["HYPRIAL_LARK_APP_SECRET"] = "synthetic-test-secret"
        result = _cli(environment, "adapter", "add", "feishu")
        assert result["daemonReload"]["added"] == ["feishu"]
        assert _cli(environment, "adapter", "status", "feishu")["adapter"]["name"] == "feishu"
        assert (home / "secrets" / "lark-feishu.json").stat().st_mode & 0o777 == 0o600


def test_online_daemon_honors_test_tmp_root(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(prefix="ao-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        monkeypatch.setenv("HYPRIAL_TEST_TMP_ROOT", str(root))
        with _online_daemon() as (home, _environment):
            assert home.parent == root


def test_remove_while_daemon_online_clears_registry_and_view() -> None:
    with _online_daemon(seed_adapter=True) as (home, environment):
        _cli(environment, "agent", "create", "--name", "worker")
        shown = _cli(environment, "agent", "cell", "show", "worker")
        cell = shown["cell"]
        members = list(cell["members"])
        params = dict(cell["params"])
        member_data = dict(params.get("cell.members") or {})
        member_data["feishu"] = {"adapter": "feishu"}
        if "cell.entry" not in params and members:
            params["cell.entry"] = members[0]["ref"]
        params["cell.members"] = member_data
        fragment = home / "cell-worker.json"
        fragment.write_text(
            json.dumps(
                {
                    "members": [*members, {"ref": "feishu", "role": "adapter"}],
                    "params": params,
                }
            ),
            encoding="utf-8",
        )
        applied = _cli(
            environment,
            "agent", "cell", "apply", "worker",
            "--expected-revision", str(cell["revision"]),
            "--file", str(fragment),
        )
        assert applied["revision"] == cell["revision"] + 1
        result = _cli(environment, "adapter", "remove", "feishu")
        assert result["removed"] is True
        assert result["unboundAgent"] is None
        assert result["credentialRemoved"] is True
        assert result["name"] == "feishu"
        assert result["provider"] == "lark"
        assert not {"channelsPath", "secretPath", "secretKept"}.intersection(result)
        assert result["daemonReload"]["removed"] == []
        assert json.loads((home / "channels.json").read_text())["gateways"] == []
        assert not (home / "secrets" / "lark-feishu.json").exists()
        assert _cli(environment, "adapter", "list")["adapters"] == []

        from hyprial.identity.impl.agents import AgentRegistry

        registry = AgentRegistry(
            home / "agents.sqlite3",
            owner="adapter-online-test",
            machine="adapter-online-node",
        )
        try:
            assert not hasattr(registry, "pins")
            assert registry.require("worker") is not None
        finally:
            registry.close()
