"""Regression: `adapter start` must never synchronously wedge the dispatcher.

Root cause (E2E-007): the daemon serves IPC on a single thread that runs each
handler inline, and `adapter.start` waited synchronously for the Lark worker's
ready signal (bounded by the launcher's ~20s startup timeout).  A worker that is
slow -- or can never reach its gateway -- therefore blocked the whole accept
loop, so a following `shutdown` (or any command) also timed out.  The daemon-side
wait (20s) even exceeded the IPC client timeout (15s), so the caller only ever
saw an opaque hang.

These tests pin the fix at two levels:
  * the supervisor returns from `start` promptly and never reports a merely-
    running-but-unconfirmed worker as ``online`` (the liveness-is-not-readiness
    trap); and
  * a live daemon keeps serving other IPC while an adapter start is in flight.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.adapters.lark.worker.process import LarkWorkerLauncher
from hyprial.kernel import (
    ChannelConfiguration,
    LarkGatewayConfig,
    PersistentConfigStore,
)

ROOT = Path(__file__).resolve().parents[4]
HYPRIAL = ROOT / ".venv" / "bin" / "hyprial"
NEVER_READY_WORKER = ROOT / "tests" / "fixtures" / "lark" / "never_ready_lark_worker.py"


# --------------------------------------------------------------------------- #
# Fast, in-process regression at the supervisor seam.
# --------------------------------------------------------------------------- #

def _never_ready_supervisor(
    tmp_path: Path, *, startup_timeout: float, start_confirm_timeout: float
) -> AdapterRuntime:
    home = tmp_path / "home"
    secret = home / "secrets" / "lark-wedge.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(json.dumps({"appSecret": "unit-test-secret"}), encoding="utf-8")
    secret.chmod(0o600)
    launcher = LarkWorkerLauncher(
        hyprial_home=home,
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        config_store=PersistentConfigStore(home, tmp_path / "state"),
        command=[sys.executable, str(NEVER_READY_WORKER)],
        startup_timeout=startup_timeout,
    )
    channels = ChannelConfiguration(
        gateways=(
            LarkGatewayConfig(
                name="lark-wedge",
                app_id="cli_unit_test",
                credential_ref="lark-wedge",
                routes=(),
            ),
        )
    )
    return AdapterRuntime(
        launcher,
        channels,
        start_confirm_timeout=start_confirm_timeout,
    )


def test_start_returns_promptly_and_never_fakes_online_for_a_stuck_worker(
    tmp_path: Path,
) -> None:
    # The worker never signals on the dedicated fd, so it can never confirm
    # online within the launcher's 5s startup window.  Before the fix, `start`
    # blocked that whole window (then raised); after the fix it returns within
    # the bounded confirmation and reports the adapter as still starting.
    supervisor = _never_ready_supervisor(
        tmp_path, startup_timeout=5.0, start_confirm_timeout=0.5
    )
    try:
        began = time.monotonic()
        try:
            changed = supervisor.start("lark-wedge")
        except Exception as error:  # a blocking start would surface here late
            pytest.fail(f"start did not return promptly: {error!r}")
        elapsed = time.monotonic() - began

        # The core property: the caller (the daemon dispatcher) is released far
        # sooner than the 5s startup window it used to block on.
        assert elapsed < 2.0, f"start blocked the caller for {elapsed:.2f}s"
        assert changed is True

        status = supervisor.status("lark-wedge")[0]
        # The trap: the process is running, but readiness never arrived, so it
        # must NOT be reported online.
        assert status["online"] is False, status
        assert status["status"] == "starting", status
    finally:
        supervisor.stop()


# --------------------------------------------------------------------------- #
# End-to-end: a live daemon keeps serving while an adapter start is in flight.
# This exercises the real single-threaded serve loop and needs no internal
# signatures, so reverting the supervisor fix makes it fail on the timeout the
# original bug produced.
# --------------------------------------------------------------------------- #

def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _env(home: Path, state: Path) -> dict[str, str]:
    from tests.support.runtime.environment import isolated_environment

    env = isolated_environment(home.parent)
    env.update(
        {
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_NODE_ID": "adapter-wedge-contract",
            "HYPRIAL_LARK_WORKER_COMMAND": json.dumps(
                [sys.executable, str(NEVER_READY_WORKER)]
            ),
            # Never inherit a developer shell's production zenoh endpoints.
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
        }
    )
    env.pop("HARNESS_SOCKET_PATH", None)
    return env


def _cli(env: dict[str, str], *args: str, timeout: float = 30.0):
    began = time.monotonic()
    completed = subprocess.run(
        [str(HYPRIAL), *args, "--json"],
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return time.monotonic() - began, completed


@pytest.mark.skipif(not HYPRIAL.exists(), reason="editable hyprial executable is unavailable")
def test_adapter_start_does_not_wedge_the_daemon_dispatcher() -> None:
    # Keep the daemon's AF_UNIX socket path short on both Linux and macOS.
    # ``/private/tmp`` is macOS-specific; its portable short alias is ``/tmp``.
    with tempfile.TemporaryDirectory(prefix="hyprial-wedge-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        home, state = root / "home", root / "state"
        _write_json(
            home / "channels.json",
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "wedge-gw",
                        "appId": "cli_wedge_app",
                        "credentialRef": "lark-wedge-gw",
                        "routes": [
                            {
                                "name": "default",
                                "type": "direct",
                                "nativeId": "agent:test:wedge",
                            }
                        ],
                        "defaultRoute": "default",
                    }
                ],
            },
        )
        _write_json(home / "secrets" / "lark-wedge-gw.json", {"appSecret": "do-not-print"})
        env = _env(home, state)

        init_dt, init = _cli(env, "init", "--no-service")
        assert init.returncode == 0, (init.stdout, init.stderr)

        results: dict[str, tuple[float, subprocess.CompletedProcess[str]]] = {}

        def run_start() -> None:
            results["start"] = _cli(env, "adapter", "start", "wedge-gw")

        def run_probe() -> None:
            # Fire while the adapter start is in flight.  A wedged dispatcher
            # cannot answer this until the start's ready-wait elapses.
            time.sleep(0.3)
            results["probe"] = _cli(env, "daemon", "status")

        try:
            start_thread = threading.Thread(target=run_start)
            probe_thread = threading.Thread(target=run_probe)
            start_thread.start()
            probe_thread.start()
            start_thread.join()
            probe_thread.join()

            start_dt, start = results["start"]
            probe_dt, probe = results["probe"]

            # `adapter start` returns a real result well under the 15s IPC
            # client timeout the bug used to hit (the never-ready worker's ~20s
            # daemon-side wait no longer runs on the dispatcher).
            assert start.returncode == 0, (start.stdout, start.stderr)
            assert start_dt < 8.0, f"adapter start took {start_dt:.2f}s"
            started = json.loads(start.stdout)
            assert started["ok"] is True, started
            assert started["adapter"]["online"] is False, started

            # The concurrent probe was served promptly -- the dispatcher stayed
            # responsive while the adapter was still coming up.
            assert probe.returncode == 0, (probe.stdout, probe.stderr)
            assert probe_dt < 5.0, f"concurrent daemon status took {probe_dt:.2f}s (wedged)"
            assert json.loads(probe.stdout)["daemon"]["running"] is True
        finally:
            subprocess.run(
                [str(HYPRIAL), "daemon", "stop", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
