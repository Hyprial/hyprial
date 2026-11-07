from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root
import json
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
import pytest
from typer.testing import CliRunner
from tests.support.runtime._daemon_diagnostics import daemon_log_summary, write

from ..support import (
    FAKE_LARK_WORKER,
    _OwnedDaemonProcess,
    _capture_owned_daemon,
    _close_application_domains as _close_application_domains,
    _write_json,
    short_state_root as short_state_root,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_owned_daemon_cleanup_never_signals_an_already_reaped_birth() -> None:
    class ReapedProcess:
        pid = 4242

        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def terminate() -> None:
            raise AssertionError("a reused PID must not be signalled")

        @staticmethod
        def kill() -> None:
            raise AssertionError("a reused PID must not be signalled")

    owner = _OwnedDaemonProcess()
    owner.process = ReapedProcess()  # type: ignore[assignment]

    owner.cleanup()


def test_real_init_returns_before_delayed_adapter_is_ready_then_converges_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-slow-init-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        home = root / "home"
        state = root / "state"
        _write_json(
            home / "channels.json",
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "slow-lark",
                        "appId": "test-app",
                        "credentialRef": "lark-slow",
                        "routes": [
                            {
                                "name": "default",
                                "type": "direct",
                                "nativeId": "agent:test:target",
                            }
                        ],
                        "defaultRoute": "default",
                    }
                ],
            },
        )
        _write_json(home / "secrets" / "lark-slow.json", {"appSecret": "secret"})
        _write_json(
            state / "desired-state.json.v1",
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [
                    {
                        "provider": "lark",
                        "name": "slow-lark",
                        "headless": True,
                        "args": [],
                        "ownership": "managed",
                    }
                ],
                "interactiveSessions": [],
                "channelPins": {},
                "legacyConversationPins": {},
                "deprecatedSharedChannels": [],
                "zenoh": {"listen": [], "connect": []},
            },
        )
        # Keep adapter readiness closed until init has returned and its
        # starting status has been observed. This proves the ordering without
        # counting interpreter startup/import latency against an 8-second sleep.
        ready_release = root / "release-ready"
        convergence_timeout = 15.0
        delay_probe = root / "delay-started.json"
        stop_probe = root / "worker-stopped.json"
        delay_nonce = secrets.token_hex(16)
        environment = {
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_NODE_ID": "slow-init-test",
            "HYPRIAL_LARK_WORKER_COMMAND": json.dumps(
                [sys.executable, str(FAKE_LARK_WORKER)]
            ),
            "HYPRIAL_FAKE_LARK_READY_RELEASE": str(ready_release),
            "HYPRIAL_FAKE_LARK_DELAY_PROBE": str(delay_probe),
            "HYPRIAL_FAKE_LARK_STOP_PROBE": str(stop_probe),
            "HYPRIAL_FAKE_LARK_DELAY_NONCE": delay_nonce,
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
        }
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        with _capture_owned_daemon(monkeypatch) as owned:
            invocation = CliRunner().invoke(
                _cli_root_app,
                ["init", "--ready-timeout", "15", "--json"],
                catch_exceptions=False,
            )
            if invocation.exit_code != 0:
                try:
                    failure = json.loads(invocation.stdout)
                except json.JSONDecodeError:
                    failure = None
                if isinstance(failure, dict) and failure.get("code") in {
                    "DAEMON_START_TIMEOUT",
                    "DAEMON_START_FAILED",
                }:
                    data = failure.get("data")
                    bounded = data if isinstance(data, dict) else {}
                    write(
                        "daemon-ipc-resilience-real-init",
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
                            "daemonLog": daemon_log_summary(state),
                        },
                    )
            assert invocation.exit_code == 0, invocation.output
            restarted = json.loads(invocation.stdout)
            # The readiness answer is the ping shape: flat, and explicitly
            # still restoring: the adapter cannot become ready until this
            # test explicitly releases it after init returns.
            assert restarted["running"] is True
            assert restarted["pid"] == owned.pid
            assert restarted["phase"] == "restoring"
            assert restarted["restorePending"] is True
            assert not ready_release.exists()
            # init returning no longer implies the restore thread has even
            # reached this adapter -- that ordering was the old coupling.
            # Wait for the worker to actually start before reading its proof.
            probe_deadline = time.monotonic() + 10.0
            while not delay_probe.exists():
                assert time.monotonic() < probe_deadline, (
                    "restore never spawned the delayed worker"
                )
                time.sleep(0.05)
            proof = json.loads(delay_probe.read_text(encoding="utf-8"))
            assert proof["nonce"] == delay_nonce
            # The marker moved with the serving boundary: daemon.json now
            # means "the daemon answers ping", and restore starts after it --
            # a worker restoring in this window must see it, because the
            # socket it announces is exactly the one the worker talks to.
            assert proof["daemonMarkerExisted"] is True

            # Public CLI -> Unix IPC proof: daemon readiness is not adapter
            # readiness.  Desired custody is visible immediately, but a live
            # process with no native ready signal must never be projected as
            # online.  This assertion catches the inverse false-online
            # mutation independently of the init-return ordering above.
            #
            # While the restore gate is closed the status call is refused
            # with DAEMON_RESTORING instead of queueing behind restore, so
            # the poll first waits out the gate.
            def adapter_status_when_served() -> dict[str, Any]:
                gate_deadline = time.monotonic() + convergence_timeout
                while True:
                    status_invocation = CliRunner().invoke(
                        _cli_root_app,
                        ["adapter", "status", "slow-lark", "--json"],
                    )
                    if status_invocation.exit_code == 0:
                        return json.loads(status_invocation.stdout)["adapter"]
                    refused = (
                        json.loads(status_invocation.stdout)
                        if status_invocation.stdout.strip().startswith("{")
                        else {}
                    )
                    assert refused.get("code") == "DAEMON_RESTORING", (
                        status_invocation.stdout
                    )
                    assert time.monotonic() < gate_deadline
                    time.sleep(0.1)

            starting = adapter_status_when_served()
            assert starting["desired"] is True
            assert starting["processRunning"] is True
            assert starting["status"] == "starting", starting
            assert starting["online"] is False, starting
            ready_release.touch()

            deadline = time.monotonic() + convergence_timeout
            while True:
                adapter = adapter_status_when_served()
                if adapter["status"] == "online":
                    break
                assert adapter["desired"] is True
                assert adapter["online"] is False
                assert adapter["status"] == "starting", adapter
                assert time.monotonic() < deadline, adapter
                time.sleep(0.1)
            assert adapter["processRunning"] is True
            assert adapter["online"] is True

            marker = json.loads((state / "daemon.json").read_text(encoding="utf-8"))
            assert marker["pid"] == owned.pid
            events = [
                json.loads(line)["event"]
                for line in (state / "logs" / "daemon.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            assert "daemon.ipc.client_error" not in events
            daemon_log = (state / "logs" / "daemon.jsonl").read_text(
                encoding="utf-8"
            )
            launch_log = (state / "daemon-launch.log").read_text(encoding="utf-8")
            assert "BrokenPipe" not in daemon_log
            assert "BrokenPipe" not in launch_log
        stopped = json.loads(stop_probe.read_text(encoding="utf-8"))
        assert stopped == {
            "nonce": delay_nonce,
            "workerPid": proof["workerPid"],
        }
