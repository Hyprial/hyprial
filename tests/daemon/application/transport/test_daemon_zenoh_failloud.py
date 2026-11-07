"""Fail-loud zenoh endpoint integration: startup log, init warning, doctor.

Spins up a REAL isolated daemon (disposable HYPRIAL_HOME/HARNESS_STATE_DIR) and
verifies the documented guarantees:

- no endpoints  -> init carries a ZENOH_ENDPOINTS_UNSET warning, the daemon
                   logs an explicit warn event, and `hyprial doctor` FAILS the
                   zenoh check (never a silent green);
- endpoints set -> no endpoint warning and doctor's zenoh check passes.

The disposable HOME cannot address the real user's supervisor domain; doctor
reports that service refusal separately. Teardown stops only this daemon and
removes only exact isolated unit paths without invoking a service manager.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import isolated_environment, remove_isolated_service_files

import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HYPRIAL = ROOT / ".venv" / "bin" / "hyprial"

if not HYPRIAL.exists():
    # Contract tests must RUN, not silently skip: a skip here would mean the
    # "no endpoints must fail loudly" guarantee is untested in that
    # environment.  Failing collection is louder than skipping it.
    raise AssertionError(
        "integration daemon tests require the editable hyprial executable; "
        f"run `uv sync` in the repo root first (missing: {HYPRIAL})"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _command(env: dict[str, str], *arguments: str) -> dict[str, object]:
    completed = subprocess.run(
        [str(HYPRIAL), *arguments, "--json"],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        value = None
    # The exit status agrees with ok: doctor with a failing check exits 1.
    expected = 1 if isinstance(value, dict) and value.get("ok") is False else 0
    if completed.returncode != expected:
        raise AssertionError(
            f"hyprial {' '.join(arguments)} exited {completed.returncode}: "
            f"stdout={completed.stdout[-2000:]!r} stderr={completed.stderr[-2000:]!r}"
        )
    if not isinstance(value, dict):
        raise AssertionError(  # noqa: TRY004
            "hyprial command did not return a JSON object"
        )
    return value


def _daemon_events(state: Path) -> list[dict[str, object]]:
    log_path = state / "logs" / "daemon.jsonl"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            events = [
                json.loads(line) for line in lines if line.strip()
            ]
            if events:
                return events
        time.sleep(0.1)
    return []


def _environment(root: Path) -> dict[str, str]:
    environment = isolated_environment(root)
    environment["HYPRIAL_NODE_ID"] = "failloud-contract"
    # This file starts a REAL daemon, so an ambient HOME here means a real
    # launchd/systemd job in somebody's session.
    assert Path(environment["HOME"]).is_relative_to(root.resolve()), environment["HOME"]
    environment.pop("HYPRIAL_ZENOH_LISTEN", None)
    environment.pop("HYPRIAL_ZENOH_CONNECT", None)
    # Peer discovery is on by default, and it would decide these tests'
    # outcome by asking the HOST whether it happens to be on a tailnet: a
    # developer machine finds peers and the endpointless warning correctly
    # stops firing, while a CI container finds none and it fires. Same commit,
    # opposite results, and neither is about the behaviour under test.
    #
    # ⚠️ That is worse than a stale expectation: it passes on CI and fails
    # locally, so the disagreement reads as "something is wrong with my
    # machine" rather than "this test asks the network a question".
    #
    # These tests are about what a daemon does with NO endpoints, so the
    # discovery input is pinned off. Whether discovery correctly suppresses
    # the warning when it does find endpoints is a different property, and it
    # belongs in a test that supplies its own directory rather than borrowing
    # the host's.
    environment["HYPRIAL_PEER_DISCOVERY"] = "0"
    # Deliberate opt-out of HYPRIAL_NETWORK_ISOLATED: this file asks what a
    # PRODUCTION daemon does with no endpoints at all (the endpoints-unset
    # warning); isolation would give it a loopback listener and hide that.
    # Discovery stays pinned off, and no endpoint or forwarding var is set.
    environment["HYPRIAL_NETWORK_ISOLATED"] = "0"
    # ...and closes every other path by hand, since the switch is off:
    # no forwarding (an inherited HYPRIAL_FORWARDING_* would start a sidecar
    # that joins the tailnet) and no usage fetch.  Measured 2026-09-23: a
    # daemon of exactly this shape (discovery off, usage off, no forwarding,
    # no listen/connect) holds zero TCP/UDP sockets -- zenoh opens no default
    # listener when `listen` is empty.
    for name in [key for key in environment if key.startswith("HYPRIAL_FORWARDING_")]:
        environment.pop(name)
    environment["HYPRIAL_USAGE_DISABLE"] = "1"
    return environment


def test_endpointless_daemon_warns_in_log_init_and_fails_doctor() -> None:
    # Keep the daemon's AF_UNIX socket path short on both Linux and macOS.
    # ``/private/tmp`` is macOS-specific; its portable short alias is ``/tmp``.
    with tempfile.TemporaryDirectory(prefix="hyprial-failloud-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        env = _environment(root)
        try:
            init = _command(env, "init", "--no-service")
            warnings = init.get("warnings")
            assert isinstance(warnings, list), init
            assert [item.get("code") for item in warnings if isinstance(item, dict)] == [
                "ORG_CONTEXT_ABSENT",
                "ZENOH_ENDPOINTS_UNSET",
                # A production-shaped daemon whose owner has no squire is
                # told so at init (Allen 2026-09-26, squire first).
                "SQUIRE_NOT_CONFIGURED",
            ]

            events = _daemon_events(root / "state")
            unset = [e for e in events if e.get("event") == "zenoh.endpoints.unset"]
            assert unset, f"daemon.jsonl missing warn event: {events[-3:]}"
            assert unset[0]["level"] == "warn"
            assert unset[0]["component"] == "zenoh"

            doctor = _command(env, "doctor")
            assert doctor["ok"] is False, doctor
            checks = {
                item["name"]: item
                for item in doctor["checks"]
                if isinstance(item, dict)
            }
            assert checks["daemon"]["status"] == "ok", checks["daemon"]
            assert checks["zenoh"]["status"] == "fail"
            assert "hyprial init --listen" in checks["zenoh"]["action"]["command"]
        finally:
            try:
                _command(env, "daemon", "stop")
            finally:
                remove_isolated_service_files(root, env)


def test_configured_daemon_has_no_endpoint_warning_and_passes_zenoh_check() -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-failloud-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        env = _environment(root)
        listen_port = _free_port()
        connect_port = _free_port()
        try:
            init = _command(
                env,
                "init",
                # These tests are about endpoints, not supervision.  Installing
                # the supervisor would leave a KeepAlive job in the CI/dev
                # session for a home this test deletes.
                "--no-service",
                "--listen",
                f"tcp/127.0.0.1:{listen_port}",
                "--connect",
                f"tcp/127.0.0.1:{connect_port}",
            )
            warnings = init.get("warnings")
            assert isinstance(warnings, list), init
            assert [item.get("code") for item in warnings if isinstance(item, dict)] == [
                "ORG_CONTEXT_ABSENT",
                "SQUIRE_NOT_CONFIGURED",
            ]

            events = _daemon_events(root / "state")
            assert not [
                e for e in events if e.get("event") == "zenoh.endpoints.unset"
            ], events

            doctor = _command(env, "doctor")
            assert doctor["ok"] is False, doctor
            service_check = next(c for c in doctor["checks"] if c["name"] == "daemon-service")
            assert service_check["metrics"]["reason"] == "service-account-home-mismatch"
            checks = {
                item["name"]: item
                for item in doctor["checks"]
                if isinstance(item, dict)
            }
            assert checks["zenoh"]["status"] == "ok"
            assert checks["zenoh"]["metrics"] == {
                "listen": [f"tcp/127.0.0.1:{listen_port}"],
                "connect": [f"tcp/127.0.0.1:{connect_port}"],
                "connectionTelemetry": {
                    "state": "unavailable",
                    "reason": "capability-absent",
                },
            }
        finally:
            try:
                _command(env, "daemon", "stop")
            finally:
                remove_isolated_service_files(root, env)
