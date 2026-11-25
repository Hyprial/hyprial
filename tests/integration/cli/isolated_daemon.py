"""One isolated daemon and the real CLI as subprocesses, for CLI end-to-end tests.

``environment`` builds a private home, state root and HOME with networking
off; ``start_daemon`` runs ``python -m hyprial.cli daemon run`` there and
waits until ``ping`` reports it ready; ``run_cli`` runs one CLI command and
returns its streams and exit code.  The link parity gate and the thread and
conversation end-to-end test share these helpers.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from tests.support.runtime.environment import assert_root_local_environment, isolated_environment

OWNER = "a03-owner"
NODE = "a03-node"


def environment(root: Path) -> dict[str, str]:
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


def ipc(socket_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
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


def start_daemon(root: Path, env: dict[str, str]) -> tuple[subprocess.Popen[str], Any]:
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
            answer = ipc(socket_path, "ping", {})
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


def run_cli(env: dict[str, str], argv: list[str]) -> dict[str, Any]:
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


__all__ = ["NODE", "OWNER", "environment", "ipc", "run_cli", "start_daemon"]
