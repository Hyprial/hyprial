"""Real-daemon acceptance for an interactive Codex CLI workflow principal.

Launching Codex itself would require a model credential and network access. This
uses the lowest substituted carrier layer: a real ``session.register`` IPC frame
with Codex's production source/runtime. Everything after registration is real:
the isolated daemon process, Unix socket, CLI subprocesses, PAC fence, graph
reactor, and inbox authority.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from tests.support.runtime.environment import assert_root_local_environment, isolated_environment


OWNER = "e2e"
NODE = "e2e-pac-principal"
ACTOR = f"agent:{OWNER}:{NODE}:codex-interactive"
SESSION = "codex-interactive-session"
OUTPUT = "interactive-principal-completion"


def _ipc(socket_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid4().hex
    frame = {
        "version": 1,
        "id": request_id,
        "method": method,
        "params": params,
    }
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(15)
    try:
        client.connect(str(socket_path))
        client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        buffer = bytearray()
        while True:
            chunk = client.recv(65536)
            if not chunk:
                raise AssertionError(f"daemon closed before answering {method}")
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not line:
                    continue
                response = json.loads(line)
                if response.get("id") != request_id:
                    continue
                assert "error" not in response, response
                result = response.get("result")
                assert isinstance(result, dict), response
                return result
    finally:
        client.close()


def _cli(env: dict[str, str], *arguments: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "hyprial.cli", *arguments, "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"command {arguments!r} failed\nstdout={completed.stdout}\n"
        f"stderr={completed.stderr}"
    )
    result = json.loads(completed.stdout)
    assert isinstance(result, dict) and result.get("ok") is True, result
    return result


def _wait_for(predicate: Any, *, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (ConnectionError, OSError, AssertionError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"condition did not become true; last={last!r}")


def test_interactive_codex_cli_workflow_notice_stays_with_agent() -> None:
    temp_base = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
    with tempfile.TemporaryDirectory(prefix="hyprial-pac-principal-", dir=temp_base) as raw:
        root = Path(raw)
        home = root / "home"
        hyprial_home = root / "hyprial"
        state = root / "state"
        for directory in (home, hyprial_home, state):
            directory.mkdir(mode=0o700)
        socket_path = state / "daemon.sock"
        daemon_log = root / "daemon.log"
        environment = isolated_environment(root)
        environment.update(
            {
                "HOME": str(home),
                "HYPRIAL_HOME": str(hyprial_home),
                "HARNESS_STATE_DIR": str(state),
                "HARNESS_SOCKET_PATH": str(socket_path),
                "HYPRIAL_OWNER": OWNER,
                "HYPRIAL_NODE_ID": NODE,
                "HYPRIAL_NETWORK_ISOLATED": "1",
                "HYPRIAL_ZENOH_LISTEN": "",
                "HYPRIAL_ZENOH_CONNECT": "",
                "HYPRIAL_PEER_DISCOVERY": "0",
                "NO_PROXY": "localhost,127.0.0.1",
                "no_proxy": "localhost,127.0.0.1",
            }
        )
        assert_root_local_environment(root, environment)
        try:
            _cli(environment, "init", "--no-service")
        finally:
            _cli(environment, "daemon", "stop")
        with daemon_log.open("w", encoding="utf-8") as log:
            daemon = subprocess.Popen(
                [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
        try:
            _wait_for(socket_path.exists)
            _cli(environment, "daemon", "status")
            registered = _ipc(
                socket_path,
                "session.register",
                {
                    "actor": ACTOR,
                    "sessionRef": SESSION,
                    "cwd": str(root),
                    "command": ["codex", "app-server"],
                    "source": "codex-app-server",
                    "runtime": "codex_interactive",
                    "channelConfirmed": True,
                },
            )
            assert registered["actor"] == ACTOR

            binding_file = root / "codex-session.ref"
            binding_file.write_text(SESSION, encoding="utf-8")
            binding_file.chmod(0o600)
            agent_environment = {
                **environment,
                "HYPRIAL_AGENT_SESSION": "1",
                "HYPRIAL_WORKER_ACTOR": ACTOR,
                "HYPRIAL_WORKER_SESSION_REF_FILE": str(binding_file),
            }
            workflow = root / "workflow.yaml"
            workflow.write_text(
                "version: 2\n"
                "name: interactive-principal-e2e\n"
                "nodes:\n"
                "  - id: work\n"
                "    role: review\n"
                f"    owner: '{ACTOR}'\n"
                "    task: Complete this isolated acceptance node.\n",
                encoding="utf-8",
            )

            started = _cli(
                agent_environment,
                "workflow",
                "run",
                str(workflow),
                "--yes",
            )
            assert started["actingPrincipal"] == ACTOR
            graph_id = started["graphId"]

            def requested() -> dict[str, Any] | None:
                status = _cli(environment, "workflow", "inspect", graph_id, "--node", "work")
                node = status.get("node")
                return node if isinstance(node, dict) and node.get("requestId") else None

            node = _wait_for(requested)
            _ipc(
                socket_path,
                "session.heartbeat",
                {"actor": ACTOR, "sessionRef": SESSION},
            )
            completed = _cli(
                agent_environment,
                "workflow",
                "complete",
                graph_id,
                "work",
                "--request-id",
                node["requestId"],
                "--reason-ref",
                "output:work",
                "--output-text",
                OUTPUT,
            )
            assert completed["actingPrincipal"] == ACTOR

            def completion_entry() -> dict[str, Any] | None:
                queried = _ipc(
                    socket_path,
                    "message.query",
                    {"from": ACTOR, "view": "inbox"},
                )
                return next(
                    (
                        entry
                        for entry in queried.get("entries", [])
                        if OUTPUT in str(entry)
                    ),
                    None,
                )

            notice = _wait_for(completion_entry)
            fetched = _ipc(
                socket_path,
                "message.pending.list",
                {"from": ACTOR, "sessionRef": SESSION, "fetched": True},
            )
            assert notice["messageId"] in {
                item["messageId"] for item in fetched["messages"]
            }
            # ``user:`` notifications bypass the durable inbox and go to the
            # owner's squire.  The PAC notification journal is the authority
            # that can prove this completion was never addressed to that path.
            database = state / "pac-graph.sqlite3"
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                recipients = {
                    row[0]
                    for row in db.execute(
                        "SELECT recipient FROM notifications "
                        "WHERE instr(text, ?) > 0",
                        (OUTPUT,),
                    )
                }
            assert ACTOR in recipients
            assert f"user:{OWNER}" not in recipients
        except BaseException:
            if daemon_log.exists():
                print(daemon_log.read_text(encoding="utf-8")[-12000:])
            raise
        finally:
            if daemon.poll() is None:
                os.kill(daemon.pid, signal.SIGTERM)
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.kill(daemon.pid, signal.SIGKILL)
                daemon.wait(timeout=5)
