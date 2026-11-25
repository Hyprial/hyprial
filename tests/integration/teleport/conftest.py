"""Two isolated real daemons joined by real Zenoh on loopback, for agent teleport.

Decision record:

- Both daemons are ``contract/e2e/_daemon_runner.IsolatedDaemon`` instances
  (``hyprial init --no-service`` from the tree's own ``.venv``) with temp
  homes, their own sockets, their own node ids, ``HYPRIAL_NETWORK_ISOLATED=1``
  and one dynamic loopback Zenoh endpoint: the source listens, the target
  connects.  ``HOME`` is also redirected into the temp root so no product
  fallback can read the operator's real ``~/.claude``.
- The worker is a ``claude`` headless agent with an agent home.  The model
  protocol is the repository's double ``tests/fixtures/harnesses/
  fake_claude_sdk.py``, injected through the daemon-side SDK bootstrap seam
  ``HYPRIAL_BUNDLED_SDK_BOOTSTRAP`` exactly like the P2 two-daemon chain; the
  auth gate is satisfied by an operator env grant (``ANTHROPIC_AUTH_TOKEN``,
  a dummy value), which the teleport export seals for the target.
- The real ``claude`` CLI writes the session transcript; the double does not.
  The test therefore writes the transcript the vendor binary would own at
  ``<home>/state/sessions/claude/<encoded cwd>/<session>.jsonl`` and starts
  the worker with that ``sessionRef``, so the agent is resumable.
- ``PATH`` starts with a stub directory holding ``ssh``, ``rsync`` and
  ``scp`` that only append their argv to ``stub-calls.log`` and exit 97 (none
  of these binaries exists on the test machine, so a PATH lookup is the only
  way the product could reach one).  It also holds a ``claude`` stub, because
  the teleport target reports the harness as available only when
  ``shutil.which("claude")`` finds something; the SDK bootstrap seam means
  the stub is never the process that runs, and calling it is recorded too.
- Every teleport is driven through the public surface only: the raw
  ``teleport.run`` daemon request and the ``hyprial agent teleport`` CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.teleport.errors import TELEPORT_CHUNK_BYTES
from hyprial.daemon.impl.transfer.archive.session_files import claude_project_dir_name

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "contract/e2e"))

from _daemon_runner import (  # noqa: E402
    IsolatedDaemon,
    free_tcp_endpoint,
    hyprial_executable,
    short_temp_root,
)

SOURCE_NODE = "tp-source"
TARGET_NODE = "tp-target"
FAKE_CLAUDE = REPO / "tests/fixtures/harnesses/fake_claude_sdk.py"
CHUNK = TELEPORT_CHUNK_BYTES
STUBBED = ("ssh", "rsync", "scp", "claude")
TELEPORT_WAIT_SECONDS = 400.0
WORKER_START_SECONDS = 60.0


@dataclass
class Pair:
    """One source and one target daemon plus the shared stub witness."""

    root: Path
    source: IsolatedDaemon
    target: IsolatedDaemon
    calls_log: Path

    def stub_calls(self) -> list[str]:
        if not self.calls_log.exists():
            return []
        return self.calls_log.read_text(encoding="utf-8").splitlines()


@dataclass(frozen=True)
class Worker:
    name: str
    home: Path
    session_ref: str
    transcript: Path


def _stub_directory(root: Path, calls_log: Path) -> Path:
    directory = root / "bin"
    directory.mkdir()
    for name in STUBBED:
        stub = directory / name
        stub.write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{calls_log}"\nexit 97\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return directory


def _daemon(
    root: Path, sub: str, node: str, stubs: Path, **endpoint: str
) -> IsolatedDaemon:
    daemon = IsolatedDaemon(hyprial_executable(), root / sub, node, **endpoint)
    user_home = root / sub / "user-home"
    user_home.mkdir(parents=True)
    daemon.env["HOME"] = str(user_home)
    daemon.env["PATH"] = f"{stubs}{os.pathsep}{daemon.env['PATH']}"
    daemon.env["HYPRIAL_BUNDLED_SDK_BOOTSTRAP"] = str(FAKE_CLAUDE)
    daemon.env["FAKE_CLAUDE_MODE"] = "settled"
    return daemon


def _stop(daemon: IsolatedDaemon) -> None:
    if not (daemon.root / "state" / "daemon.sock").exists():
        return
    try:
        daemon.stop()
        daemon.wait_stopped(timeout=60.0)
    except Exception:
        try:
            daemon.sigterm()
        except Exception:
            pass


@pytest.fixture(scope="module")
def pair() -> Iterator[Pair]:
    root = Path(tempfile.mkdtemp(prefix="tp-", dir=short_temp_root()))
    calls_log = root / "stub-calls.log"
    stubs = _stub_directory(root, calls_log)
    endpoint = free_tcp_endpoint()
    source = _daemon(root, "src", SOURCE_NODE, stubs, listen=endpoint)
    target = _daemon(root, "tgt", TARGET_NODE, stubs, connect=endpoint)
    try:
        source.start()
        target.start()
        yield Pair(root, source, target, calls_log)
    finally:
        _stop(source)
        _stop(target)
        shutil.rmtree(root, ignore_errors=True)


def request(daemon: IsolatedDaemon, method: str, params: dict[str, Any], *,
            timeout: float = 60.0) -> dict[str, Any]:
    """One raw IPC frame, so a typed refusal keeps its code, message and data."""

    frame = json.dumps(
        {"version": 1, "id": uuid.uuid4().hex, "method": method, "params": params},
        separators=(",", ":"),
    ).encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(daemon.root / "state" / "daemon.sock"))
        client.sendall(frame + b"\n")
        data = bytearray()
        while b"\n" not in data:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise OSError("daemon closed IPC before responding")
            data.extend(chunk)
    finally:
        client.close()
    return json.loads(data.partition(b"\n")[0])


def ok(daemon: IsolatedDaemon, method: str, params: dict[str, Any]) -> dict[str, Any]:
    document = request(daemon, method, params)
    assert "error" not in document, (method, document)
    return document["result"]


def teleport(daemon: IsolatedDaemon, agent: str, to: str, *, dry_run: bool = False
             ) -> dict[str, Any]:
    params: dict[str, Any] = {"agent": agent, "to": to}
    if dry_run:
        params["dryRun"] = True
    return request(daemon, "teleport.run", params, timeout=TELEPORT_WAIT_SECONDS)


def connector(daemon: IsolatedDaemon, name: str) -> dict[str, Any] | None:
    for item in daemon.ps(transient_timeout=30.0).get("connectors", []):
        if item.get("name") == name:
            return item
    return None


def running(daemon: IsolatedDaemon, name: str) -> bool:
    item = connector(daemon, name)
    return bool(item is not None and item.get("running"))


def wait_running(daemon: IsolatedDaemon, name: str,
                 timeout: float = WORKER_START_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running(daemon, name):
            return True
        time.sleep(0.25)
    return False


def agent_rows(daemon: IsolatedDaemon) -> dict[str, str]:
    """The daemon's own identity rows, read from its private test database."""

    database = daemon.root / "state" / "agents.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        return dict(conn.execute("SELECT actor, uri FROM agents").fetchall())


def desired_harness(daemon: IsolatedDaemon, name: str) -> dict[str, Any] | None:
    """The daemon's desired-state row for ``name`` (session ref, cwd, status)."""

    database = daemon.root / "state" / "lifecycle-operations.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT harness, name, cwd, session_ref, status FROM harnesses "
            "WHERE name=?",
            (name,),
        ).fetchone()
    return None if row is None else dict(row)


def agent_home(daemon: IsolatedDaemon, name: str) -> Path:
    return daemon.root / "home" / "agents" / name


def staging_entries(daemon: IsolatedDaemon) -> list[str]:
    root = daemon.root / "state" / "teleport" / "staging"
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def tree_digest(root: Path) -> dict[str, str]:
    """Relative path -> sha256 of every regular file below ``root``."""

    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digests[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


def _private_dirs(*directories: Path) -> None:
    for directory in directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)


def seed_worker(daemon: IsolatedDaemon, name: str, *, payload_bytes: int) -> Worker:
    """A running headless claude agent with an agent home and a resumable session.

    ``payload_bytes`` of random data go into ``workspace/payload.bin`` so the
    packed home needs more than one teleport chunk.
    """

    ok(daemon, "agent.create", {"name": name})
    ok(daemon, "agent.ensure-home", {"name": name})
    entry = f"env-{name}"
    ok(daemon, "agent.secret.provider-write",
       {"entryId": entry, "fieldName": "token", "value": f"sk-dummy-{name}"})
    ok(daemon, "agent.secret.grant", {
        "actor": name,
        "grantId": "g-token",
        "source": "user-provider",
        "entryId": entry,
        "fieldName": "token",
        "environmentNames": ["ANTHROPIC_AUTH_TOKEN"],
        "revision": 1,
    })
    home = agent_home(daemon, name)
    workspace = home / "workspace"
    (workspace / "payload.bin").write_bytes(os.urandom(payload_bytes))
    (workspace / "notes.txt").write_text(f"teleport fixture for {name}\n")
    session_ref = str(uuid.uuid4())
    sessions = home / "state" / "sessions"
    project = sessions / "claude" / claude_project_dir_name(str(workspace))
    _private_dirs(sessions, sessions / "claude", project)
    transcript = project / f"{session_ref}.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "sessionId": session_ref, "cwd": str(workspace)})
        + "\n",
        encoding="utf-8",
    )
    os.chmod(transcript, 0o600)
    started = ok(daemon, "lifecycle.start", {
        "provider": "claude",
        "name": name,
        "headless": True,
        "cwd": str(workspace),
        "sessionRef": session_ref,
    })
    assert started.get("sessionRef") == session_ref, started
    assert wait_running(daemon, name), f"{name} never reported running on the source"
    return Worker(name, home, session_ref, transcript)


def stop_worker(daemon: IsolatedDaemon, name: str) -> None:
    try:
        request(daemon, "down", {"name": name})
    except OSError:
        pass
