"""A-03: the migrated link group keeps its real-daemon CLI bytes.

Each case starts a fresh daemon (`python -m hyprial.cli daemon run`) in an
isolated home/state root and then runs the real CLI as a subprocess.  No mock
request port is involved: this is the byte-parity gate for moving
`link.list/stats/add/drop` onto MethodSpec-bound handlers.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import assert_root_local_environment, isolated_environment

ROOT = Path(__file__).resolve().parents[4]
GOLDEN = Path(__file__).with_name("golden.json")
OWNER = "a03-owner"
NODE = "a03-node"
ALICE = f"agent:{OWNER}:{NODE}:alice"
BOB = f"agent:{OWNER}:{NODE}:bob"


def _environment(root: Path) -> dict[str, str]:
    for name in ("home", "state", "user"):
        (root / name).mkdir(parents=True, mode=0o700)
    (root / "home" / "settings.json").write_text(
        json.dumps({"version": 1, "owner": OWNER}), encoding="utf-8"
    )
    env = isolated_environment(root)
    env.update(
        {
            "HOME": str(root / "user"),
            "HYPRIAL_HOME": str(root / "home"),
            "HARNESS_STATE_DIR": str(root / "state"),
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_OWNER": OWNER,
            "HYPRIAL_NODE_ID": NODE,
            "HYPRIAL_LINKS_BYPASS": "0",
        }
    )
    env.pop("HARNESS_SOCKET_PATH", None)
    assert_root_local_environment(root, env)
    return env


def _ipc(socket_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    frame = (
        json.dumps(
            {"version": 1, "id": uuid.uuid4().hex, "method": method, "params": params},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(20)
    try:
        client.connect(str(socket_path))
        client.sendall(frame)
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise OSError("daemon closed IPC before answering")
            response.extend(chunk)
    finally:
        client.close()
    return json.loads(response.partition(b"\n")[0])


def _start_daemon(root: Path, env: dict[str, str]) -> tuple[subprocess.Popen[str], Any]:
    log = (root / "daemon.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "hyprial.cli", "daemon", "run"],
        cwd=root,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    state = Path(env["HARNESS_STATE_DIR"])
    socket_path = state / "daemon.sock"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.close()
            raise AssertionError(
                f"daemon exited {process.returncode}:\n"
                + (root / "daemon.log").read_text(encoding="utf-8", errors="replace")
            )
        try:
            answer = _ipc(socket_path, "ping", {})
        except (OSError, ValueError):
            time.sleep(0.1)
            continue
        result = answer.get("result", answer)
        if result.get("running") is True and result.get("restorePending") is not True:
            return process, log
        time.sleep(0.1)
    log.close()
    process.terminate()
    process.wait(timeout=30)
    raise AssertionError(
        "daemon did not become ready:\n"
        + (root / "daemon.log").read_text(encoding="utf-8", errors="replace")
    )


def _run_cli(env: dict[str, str], argv: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "hyprial.cli", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return {
        "argv": argv,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exitCode": completed.returncode,
    }


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _cases() -> dict[str, list[str]]:
    return {
        "list_text": ["agent", "link", "list", "--from", ALICE],
        "list_json": ["agent", "link", "list", "--from", ALICE, "--json"],
        "stats_text": ["agent", "link", "stats", "--from", ALICE],
        "stats_json": ["agent", "link", "stats", "--from", ALICE, "--json"],
        "add_text": ["agent", "link", "add", BOB, "--from", f"user:{OWNER}", "--on-behalf-of", ALICE],
        "add_json": [
            "agent", "link", "add", BOB, "--from", f"user:{OWNER}", "--on-behalf-of", ALICE, "--json",
        ],
        "drop_text": ["agent", "link", "drop", BOB, "--from", f"user:{OWNER}", "--on-behalf-of", ALICE],
        "drop_json": [
            "agent", "link", "drop", BOB, "--from", f"user:{OWNER}", "--on-behalf-of", ALICE, "--json",
        ],
    }


@pytest.mark.parametrize("case", sorted(_cases()))
def test_link_cli_bytes_match_the_a02_baseline(case: str) -> None:
    # xdist adds an extra pytest tmp directory; a short explicit root keeps the
    # AF_UNIX socket below macOS's 104-byte sun_path limit.
    with tempfile.TemporaryDirectory(
        prefix="a03-link-", dir=hyprial_test_tmp_root()
    ) as raw:
        root = Path(raw) / case
        root.mkdir()
        env = _environment(root)
        process, log = _start_daemon(root, env)
        try:
            actual = _run_cli(env, _cases()[case])
        finally:
            process.terminate()
            process.wait(timeout=30)
            log.close()

    expected = _golden()[case]
    assert actual["exitCode"] == expected["exitCode"], actual
    assert actual["stdout"].encode() == expected["stdout"].encode(), actual
    assert actual["stderr"].encode() == expected["stderr"].encode(), actual


def test_link_error_codes_streams_and_exit_status_are_preserved() -> None:
    with tempfile.TemporaryDirectory(
        prefix="a03-link-", dir=hyprial_test_tmp_root()
    ) as raw:
        root = Path(raw) / "errors"
        root.mkdir()
        env = _environment(root)
        process, log = _start_daemon(root, env)
        try:
            cases = {
                "empty_from": (
                    ["agent", "link", "list", "--from", "", "--json"],
                    "INVALID_ARGUMENT",
                ),
                "operator_without_agent": (
                    ["agent", "link", "add", BOB, "--from", f"user:{OWNER}", "--json"],
                    "CALLER_NOT_AUTHORIZED",
                ),
                "bare_agent_without_session": (
                    ["agent", "link", "add", BOB, "--from", ALICE, "--json"],
                    "SENDER_UNRESOLVED",
                ),
            }
            empty_peer_cases = {
                "empty_peer_json": (
                    ["agent", "link", "add", "", "--from", f"user:{OWNER}", "--on-behalf-of", ALICE, "--json"],
                    '{"ok":false,"code":"INVALID_ARGUMENT","error":"peer must be a non-empty string"}\n',
                    "",
                ),
                "empty_peer_text": (
                    ["agent", "link", "add", "", "--from", f"user:{OWNER}", "--on-behalf-of", ALICE],
                    "",
                    "hyprial: peer must be a non-empty string\n",
                ),
            }
            for name, (argv, stdout, stderr) in empty_peer_cases.items():
                actual = _run_cli(env, argv)
                assert actual["exitCode"] == 1, (name, actual)
                assert actual["stdout"] == stdout, (name, actual)
                assert actual["stderr"] == stderr, (name, actual)

            for name, (argv, code) in cases.items():
                actual = _run_cli(env, argv)
                assert actual["exitCode"] == 1, (name, actual)
                assert actual["stderr"] == "", (name, actual)
                payload = json.loads(actual["stdout"])
                assert payload["ok"] is False, (name, payload)
                assert payload["code"] == code, (name, payload)
                assert isinstance(payload.get("error"), str) and payload["error"], payload
        finally:
            process.terminate()
            process.wait(timeout=30)
            log.close()
