"""S8 end to end on a real isolated daemon: attach -> send -> batch/read -> reply -> ack -> detach.

A fresh ``hyprial daemon run`` in an isolated home/state root (Zenoh
isolated, peer discovery off, links enforced), driven only through the real
CLI as subprocesses: no mock request port.  The external harness is played
by the CLI calls that name the attached agent with ``--from``: it reads with
``message batch`` (a consuming read with full bodies), replies and acks; the
daemon never starts a harness for it.  The isolated daemon has no OrgFS org, so
the cell is recorded in the local cell cache only.  Error cases check the
declared codes, ``ok:false`` on stdout, empty stderr and exit status 1.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import UNIT_USER_ID

OWNER = "s8-owner"
NODE = "s8-node"
EXT = f"agent:{OWNER}:{NODE}:ext"


def _environment(root: Path) -> dict[str, str]:
    for name in ("home", "state", "user", "tmp"):
        (root / name).mkdir(parents=True, mode=0o700)
    (root / "home" / "settings.json").write_text(
        json.dumps({"version": 1, "owner": OWNER}), encoding="utf-8"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HYPRIAL_", "HARNESS_"))
    }
    env.update(
        {
            "HOME": str(root / "user"),
            "TMPDIR": str(root / "tmp"),
            "HYPRIAL_HOME": str(root / "home"),
            "HARNESS_STATE_DIR": str(root / "state"),
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_OWNER": OWNER,
            "HYPRIAL_USER_ID": UNIT_USER_ID,
            "HYPRIAL_NODE_ID": NODE,
            "HYPRIAL_LINKS_BYPASS": "0",
        }
    )
    env.pop("HARNESS_SOCKET_PATH", None)
    return env


def _ping(socket_path: Path) -> dict[str, Any]:
    frame = json.dumps(
        {"version": 1, "id": uuid.uuid4().hex, "method": "ping", "params": {}}
    ).encode() + b"\n"
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
        cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    socket_path = Path(env["HARNESS_STATE_DIR"]) / "daemon.sock"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.close()
            raise AssertionError(
                f"daemon exited {process.returncode}:\n"
                + (root / "daemon.log").read_text(encoding="utf-8", errors="replace")
            )
        try:
            answer = _ping(socket_path)
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


def _cli(env: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", *argv],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def _ok(env: dict[str, str], *argv: str) -> dict[str, Any]:
    done = _cli(env, *argv, "--json")
    assert done.returncode == 0, (argv, done.stdout, done.stderr)
    payload = json.loads(done.stdout)
    assert payload["ok"] is True, (argv, payload)
    return payload


def _refused(env: dict[str, str], code: str, *argv: str) -> None:
    done = _cli(env, *argv, "--json")
    assert done.returncode == 1, (argv, done.stdout, done.stderr)
    assert done.stderr == "", (argv, done.stderr)
    payload = json.loads(done.stdout)
    assert payload["ok"] is False and payload["code"] == code, (argv, payload)


def _batch_ids(env: dict[str, str]) -> list[str]:
    batch = _ok(env, "message", "batch", "--from", "ext")
    return [str(item["messageId"]) for item in batch["items"]]


def test_attach_send_read_reply_ack_detach_on_a_real_daemon() -> None:
    with tempfile.TemporaryDirectory(prefix="s8-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw) / "e2e"
        root.mkdir()
        env = _environment(root)
        process, log = _start_daemon(root, env)
        try:
            attached = _ok(env, "agent", "attach", "ext")
            # Release (UUID addressing, #2546): the new identity is addressed
            # at its id address; the name stays its display/CLI alias.
            assert attached["actor"] == f"agent:{OWNER}:{NODE}:{attached['agentId']}"
            assert attached["created"] is True
            assert attached["custody"] == "external" and attached["attached"] is True
            assert attached["cell"]["state"] in {"recorded", "deferred"}
            again = _ok(env, "agent", "attach", "ext")
            assert again["created"] is False and again["changed"] is False
            assert again["agentId"] == attached["agentId"]
            text = _cli(env, "agent", "attach", "ext")
            assert text.returncode == 0 and "already attached" in text.stdout, text

            listed = _ok(env, "agent", "list")
            [row] = [item for item in listed["agents"] if item["actor"] == "ext"]
            assert row["capabilities"] == {"custody": "external", "attached": True}
            assert row["harness"] is None

            sent = _ok(env, "message", "send", "hello external", "--from",
                       f"user:{OWNER}", "--to", "ext", "--summary", "greeting")
            request = str(sent["messageId"])
            batch = _ok(env, "message", "batch", "--from", "ext")
            assert [str(item["messageId"]) for item in batch["items"]] == [request]
            assert "hello external" in batch["delivery"]["text"]
            replied = _ok(env, "message", "reply", request, "answered", "--from", "ext",
                          "--summary", "answer")
            assert replied["replyMessageId"]

            second = str(_ok(env, "message", "send", "ack me", "--from", f"user:{OWNER}",
                             "--to", "ext", "--summary", "second")["messageId"])
            assert second in _batch_ids(env) and request not in _batch_ids(env)
            _ok(env, "message", "ack", second, "--from", "ext")
            assert _batch_ids(env) == []

            detached = _ok(env, "agent", "detach", "ext")
            assert detached["attached"] is False and detached["changed"] is True
            assert detached["agentId"] == attached["agentId"]
            assert _ok(env, "agent", "detach", "ext")["changed"] is False
            kept = _ok(env, "agent", "list")
            assert [item["capabilities"] for item in kept["agents"]
                    if item["actor"] == "ext"] == [{"custody": "external", "attached": False}]

            _ok(env, "agent", "create", "--name", "managed")
            _refused(env, "AGENT_EXISTS", "agent", "attach", "managed")
            _refused(env, "INVALID_ARGUMENT", "agent", "detach", "managed")
            _refused(env, "AGENT_NOT_FOUND", "agent", "detach", "nobody")
            _refused(env, "INVALID_ARGUMENT", "agent", "attach", EXT)
        finally:
            process.terminate()
            process.wait(timeout=30)
            log.close()
