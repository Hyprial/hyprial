from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import isolated_environment

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.support.runtime._daemon_diagnostics import daemon_log_summary, write
from hyprial.daemon.impl.application import (
    TEARDOWN_BUDGETED_SECONDS,
    DaemonApplication,
    DaemonRequestError,
)


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


ROOT = Path(__file__).resolve().parents[4]
HYPRIAL = ROOT / ".venv" / "bin" / "hyprial"
FAKE_WORKER = ROOT / "tests" / "fixtures" / "lark" / "fake_lark_worker.py"
DAEMON_STOP_TIMEOUT_SECONDS = TEARDOWN_BUDGETED_SECONDS + 5.0
# These tests assert adapter lifecycle/persistence, not cold-start latency.
# A loaded two-CPU CI container exhausted init's default 15 s with its child
# still alive and only 0.88 CPU seconds consumed. Match the E2E startup budget;
# leave time for init to emit its own diagnostic before the harness times out.
DAEMON_INIT_READY_TIMEOUT_SECONDS = 45.0
DAEMON_INIT_COMMAND_TIMEOUT_SECONDS = DAEMON_INIT_READY_TIMEOUT_SECONDS + 15.0


def _write_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)


def _config(home: Path) -> None:
    gateways = []
    for index in range(4):
        name = f"lark-{index + 1}"
        gateways.append(
            {
                "provider": "lark",
                "name": name,
                "appId": f"cli_test_app_{index + 1}",
                "credentialRef": name,
                "routes": [
                    {
                        "name": "squire",
                        "type": "direct",
                        "nativeId": "agent:test:target",
                    }
                ],
                "defaultRoute": "squire",
            }
        )
        _write_json(
            home / "secrets" / f"{name}.json",
            {"appSecret": f"never-print-secret-{index + 1}"},
        )
    _write_json(home / "channels.json", {"version": 1, "gateways": gateways})


def _command(
    env: dict[str, str],
    *args: str,
    timeout: float = 20.0,
) -> dict[str, object]:
    command = [str(HYPRIAL), *args, "--json"]
    if args[:1] == ("init",):
        # ``--no-service``: this home is disposable, and the per-user
        # supervisor is boot-persistent.  Without it every run installed a
        # KeepAlive launchd/systemd job for a home the test deletes
        # (2026-10-30).
        command.extend(
            [
                "--no-service",
                "--ready-timeout",
                str(DAEMON_INIT_READY_TIMEOUT_SECONDS),
            ]
        )
        timeout = max(timeout, DAEMON_INIT_COMMAND_TIMEOUT_SECONDS)
    completed = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        try:
            failure = json.loads(completed.stdout)
        except json.JSONDecodeError:
            failure = None
        if isinstance(failure, dict) and failure.get("code") in {
            "DAEMON_START_TIMEOUT",
            "DAEMON_START_FAILED",
        }:
            data = failure.get("data")
            bounded = data if isinstance(data, dict) else {}
            state_dir = Path(env["HARNESS_STATE_DIR"])
            write(
                "daemon-adapter-lifecycle-command",
                {
                    "kind": "child-start-timeout",
                    "code": failure["code"],
                    "startupEnvelope": {
                        key: bounded.get(key)
                        for key in (
                            "lastStartupPhase",
                            "phasesSeen",
                            "elapsedSeconds",
                            "child",
                        )
                    },
                    "daemonLog": daemon_log_summary(state_dir),
                },
            )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "never-print-secret" not in completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


def test_lark_package_imports_in_a_fresh_process() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import hyprial.daemon.impl.adapters.lark"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_provider_lifecycle_rejects_lark_without_persisting_desired_state(
    tmp_path: Path, own_daemon_application,
) -> None:
    app = own_daemon_application(DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="adapter-contract",
        hyprial_home=tmp_path / "home",
        **_app_injections(),
    ))
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]

    with pytest.raises(DaemonRequestError, match="hyprial adapter start") as failure:
        app.handle("lifecycle.start", {"provider": "lark", "name": "wrong-seam"})

    assert failure.value.code == "USE_ADAPTER_COMMAND"
    assert app.desired_state.load().harnesses == ()


@pytest.mark.skipif(not HYPRIAL.exists(), reason="editable hyprial executable is unavailable")
def test_four_lark_adapters_are_online_and_restore_after_daemon_restart(
) -> None:
    # Keep the daemon's AF_UNIX socket path short on both Linux and macOS.
    # ``/private/tmp`` is macOS-specific; its portable short alias is ``/tmp``.
    with tempfile.TemporaryDirectory(prefix="hyprial-adapter-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        home = root / "home"
        state = root / "state"
        _config(home)
        env = isolated_environment(root)
        env.update(
            {
                "HYPRIAL_HOME": str(home),
                "HARNESS_STATE_DIR": str(state),
                "HYPRIAL_NODE_ID": "adapter-contract",
                "HYPRIAL_LARK_WORKER_COMMAND": json.dumps(
                    [sys.executable, str(FAKE_WORKER)]
                ),
                # A developer shell may export production zenoh endpoints;
                # inheriting them makes this daemon race the production
                # listener for its port.  The test daemon stays isolated.
                "HYPRIAL_ZENOH_LISTEN": "",
                "HYPRIAL_ZENOH_CONNECT": "",
            }
        )

        _command(env, "init")
        try:
            for index in range(4):
                result = _command(env, "adapter", "start", f"lark-{index + 1}")
                assert result["adapter"]["status"] == "online"  # type: ignore[index]

            listed = _command(env, "adapter", "list")
            assert [item["name"] for item in listed["adapters"]] == [  # type: ignore[index]
                "lark-1",
                "lark-2",
                "lark-3",
                "lark-4",
            ]
            assert {item["status"] for item in listed["adapters"]} == {"online"}  # type: ignore[index]
            from hyprial.daemon.impl.desired_state import DesiredStateStore

            desired = DesiredStateStore(
                state / "desired-state.json"
            ).load().to_json()
            assert [
                (item["provider"], item["name"]) for item in desired["adapters"]
            ] == [
                ("lark", "lark-1"),
                ("lark", "lark-2"),
                ("lark", "lark-3"),
                ("lark", "lark-4"),
            ]

            _command(
                env,
                "daemon",
                "stop",
                timeout=DAEMON_STOP_TIMEOUT_SECONDS,
            )
            _command(env, "init")
            deadline = time.monotonic() + 10
            while True:
                listed = _command(env, "adapter", "list")
                if len(listed.get("adapters", [])) == 4 and {
                    item["status"] for item in listed["adapters"]  # type: ignore[index]
                } == {"online"}:
                    break
                assert time.monotonic() < deadline, listed
                time.sleep(0.1)

            stopped = _command(env, "adapter", "stop", "lark-4")
            assert stopped["adapter"]["status"] == "stopped"  # type: ignore[index]
            from hyprial.daemon.impl.desired_state import DesiredStateStore

            desired = DesiredStateStore(
                state / "desired-state.json"
            ).load().to_json()
            assert [item["name"] for item in desired["adapters"]] == [
                "lark-1",
                "lark-2",
                "lark-3",
            ]
        finally:
            subprocess.run(
                [str(HYPRIAL), "daemon", "stop", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=DAEMON_STOP_TIMEOUT_SECONDS,
                check=False,
            )


@pytest.mark.skipif(not HYPRIAL.exists(), reason="editable hyprial executable is unavailable")
def test_public_ipc_stop_persistence_failure_keeps_recoverable_desired_intent(
) -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-adapter-fault-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        home = root / "home"
        state = root / "state"
        _config(home)
        env = isolated_environment(root)
        env.update(
            {
                "HYPRIAL_HOME": str(home),
                "HARNESS_STATE_DIR": str(state),
                "HYPRIAL_NODE_ID": "adapter-fault-contract",
                "HYPRIAL_LARK_WORKER_COMMAND": json.dumps(
                    [sys.executable, str(FAKE_WORKER)]
                ),
                "HYPRIAL_ZENOH_LISTEN": "",
                "HYPRIAL_ZENOH_CONNECT": "",
            }
        )

        _command(env, "init")
        # The save surface is the SQLite store file since U0a-2; hiding it
        # behind a directory makes every desired-state write fail.
        desired_path = state / "lifecycle-operations.sqlite3"
        saved_desired_path = state / "lifecycle-operations.sqlite3.saved"
        try:
            started = _command(env, "adapter", "start", "lark-1")
            assert started["adapter"]["status"] == "online"  # type: ignore[index]
            desired_path.replace(saved_desired_path)
            desired_path.mkdir()
            failed = subprocess.run(
                [str(HYPRIAL), "adapter", "stop", "lark-1", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            assert failed.returncode != 0, (failed.stdout, failed.stderr)
        finally:
            if desired_path.is_dir():
                desired_path.rmdir()
            if saved_desired_path.exists():
                saved_desired_path.replace(desired_path)

        try:
            from hyprial.daemon.impl.desired_state import DesiredStateStore

            desired = DesiredStateStore(
                state / "desired-state.json"
            ).load().to_json()
            assert any(
                item["provider"] == "lark" and item["name"] == "lark-1"
                for item in desired["adapters"]
            )

            deadline = time.monotonic() + 10
            while True:
                status = _command(env, "adapter", "status", "lark-1")["adapter"]
                if status["status"] == "online":  # type: ignore[index]
                    break
                assert time.monotonic() < deadline, status
                time.sleep(0.1)

            _command(
                env,
                "daemon",
                "stop",
                timeout=DAEMON_STOP_TIMEOUT_SECONDS,
            )
            _command(env, "init")
            deadline = time.monotonic() + 10
            while True:
                status = _command(env, "adapter", "status", "lark-1")["adapter"]
                if status["status"] == "online":  # type: ignore[index]
                    break
                assert time.monotonic() < deadline, status
                time.sleep(0.1)

            stopped = _command(env, "adapter", "stop", "lark-1")
            assert stopped["adapter"]["status"] == "stopped"  # type: ignore[index]
            from hyprial.daemon.impl.desired_state import DesiredStateStore

            desired = DesiredStateStore(
                state / "desired-state.json"
            ).load().to_json()
            assert not any(
                item["provider"] == "lark" and item["name"] == "lark-1"
                for item in desired["adapters"]
            )
        finally:
            subprocess.run(
                [str(HYPRIAL), "daemon", "stop", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=DAEMON_STOP_TIMEOUT_SECONDS,
                check=False,
            )
