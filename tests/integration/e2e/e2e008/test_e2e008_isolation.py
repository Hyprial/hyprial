"""Isolation contracts for the shared daemon runner and E2E-008."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]
SCENARIOS = REPO / "contract" / "e2e"
RUN_008 = SCENARIOS / "run-cc-tui-worker.py"
if str(SCENARIOS) not in sys.path:
    sys.path.insert(0, str(SCENARIOS))

import _daemon_runner as runner  # noqa: E402

AMBIENT_IDENTITY_KEYS = (
    "HYPRIAL_WORKER_ACTOR",
    "HYPRIAL_WORKER_SESSION_REF",
    "HARNESS_SOCKET_PATH",
    "PI_SESSION_ID",
    "PI_SESSION_FILE",
    "PI_CODING_AGENT",
    "PI_PROVIDER",
    "PI_MODEL",
    "PI_REASONING_LEVEL",
)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _e2e008_temp_context() -> ast.Call:
    tree = ast.parse(RUN_008.read_text(encoding="utf-8"))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    for node in ast.walk(main):
        if not isinstance(node, ast.withitem) or not isinstance(
            node.context_expr, ast.Call
        ):
            continue
        call = node.context_expr
        if _call_name(call) == "scenario_tempdir" and call.args:
            if isinstance(call.args[0], ast.Constant) and call.args[0].value == "e2e-008":
                return call
        if _call_name(call) == "TemporaryDirectory":
            prefix = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "prefix"),
                None,
            )
            if isinstance(prefix, ast.Constant) and "e2e-008" in str(prefix.value):
                return call
    raise AssertionError("E2E-008 tempdir context is missing")


def test_e2e008_uses_short_failure_preserving_tempdir() -> None:
    call = _e2e008_temp_context()
    directory = next(
        (
            keyword.value
            for keyword in call.keywords
            if keyword.arg in {"directory", "dir"}
        ),
        None,
    )

    assert (
        isinstance(directory, ast.Call)
        and _call_name(directory) == "short_temp_root"
    ), "E2E-008 tempdir directory is not short_temp_root()"
    assert _call_name(call) == "scenario_tempdir", (
        "E2E-008 tempdir does not preserve the scenario root on failure"
    )


def test_isolated_daemon_defaults_peer_discovery_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "1")

    daemon = runner.IsolatedDaemon(Path("/bin/echo"), tmp_path, "test-node")

    assert daemon.env["HYPRIAL_PEER_DISCOVERY"] == "0", (
        "IsolatedDaemon did not force HYPRIAL_PEER_DISCOVERY=0"
    )


@pytest.mark.parametrize("ambient_key", AMBIENT_IDENTITY_KEYS)
def test_isolated_daemon_child_does_not_receive_ambient_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ambient_key: str,
) -> None:
    monkeypatch.setenv(ambient_key, f"ambient-{ambient_key}")
    daemon = runner.IsolatedDaemon(Path("/bin/echo"), tmp_path, "test-node")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys; print(os.environ.get(sys.argv[1], '<missing>'))",
            ambient_key,
        ],
        env=daemon.env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )

    assert completed.stdout.strip() == "<missing>", (
        f"isolated child inherited ambient identity {ambient_key}"
    )


def test_real_isolated_daemon_has_no_discovered_or_effective_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "1")
    executable = runner.hyprial_executable()
    with runner.scenario_tempdir(
        "e2e008-isolation-test", directory=runner.short_temp_root()
    ) as root:
        daemon = runner.IsolatedDaemon(executable, root, "e2e008-isolation-test")
        try:
            daemon.start()
            events: list[dict[str, Any]] = [
                json.loads(line)
                for line in (root / "state" / "logs" / "daemon.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            resolved = next(
                event
                for event in events
                if event.get("event") == "zenoh.endpoints.resolved"
            )
            assert (
                resolved.get("discovery"),
                resolved.get("configured"),
                resolved.get("discovered"),
                resolved.get("effective"),
            ) == ("disabled", 0, 0, 0)

            status = daemon.request("ps", {})
            assert status.get("zenoh", {}).get("connect") == []
        finally:
            daemon.stop()
            daemon.wait_stopped()


def test_real_isolated_daemon_keeps_one_explicit_loopback_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "1")
    executable = runner.hyprial_executable()
    with runner.scenario_tempdir(
        "e2e008-loopback-test", directory=runner.short_temp_root()
    ) as root:
        endpoint = runner.free_tcp_endpoint()
        daemon = runner.IsolatedDaemon(
            executable,
            root,
            "e2e008-loopback-test",
            connect=endpoint,
        )
        try:
            daemon.start()
            events: list[dict[str, Any]] = [
                json.loads(line)
                for line in (root / "state" / "logs" / "daemon.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            resolved = next(
                event
                for event in events
                if event.get("event") == "zenoh.endpoints.resolved"
            )
            assert (
                resolved.get("discovery"),
                resolved.get("configured"),
                resolved.get("discovered"),
                resolved.get("effective"),
            ) == ("disabled", 1, 0, 1)

            status = daemon.request("ps", {})
            assert status.get("zenoh", {}).get("connect") == [endpoint]
        finally:
            daemon.stop()
            daemon.wait_stopped()
