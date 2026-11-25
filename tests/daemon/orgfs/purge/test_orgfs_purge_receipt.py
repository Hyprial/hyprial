"""Isolated real-CLI daemon root shared by daemon home-isolation tests.

Decision (orgfs-minimal D4a, 2026-11-25): the G3 content-purge receipt tests
drove ``org fs purge plan`` / ``org fs purge run PLAN_ID`` through the real CLI.
Content purge left every public surface (owner Q4), so those three tests are
gone; the request-layer refusal is pinned in
``tests/daemon/ipc/retired_work/test_orgfs_content_purge_removed.py``.  The
facade-level purge tests in this directory stay until package S deletes the
old implementation.  ``_IsolatedCli`` stays because
``tests/daemon/application/composition/service/test_home_isolation.py`` builds
it to prove the daemon builder owns its home and XDG roots.

Isolation: the CLI fixture owns a temporary ``HOME``, ``HYPRIAL_HOME`` and
``HARNESS_STATE_DIR`` under one test root, unsets ``HYPRIAL_NODE_ID`` (the
home inside that root carries its own minted node id) and unsets the worker/tool-profile
bindings.  Nothing ambient survives into either subprocess.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from tests.support.runtime.environment import (
    assert_root_local_environment,
    home_with_node_id,
    isolated_environment,
)

OWNER = "g3-owner"
_SOCKET_TIMEOUT_SECONDS = 5.0
_READY_TIMEOUT_SECONDS = 90.0
_CLI_TIMEOUT_SECONDS = 180


class _IsolatedCli:
    """One isolated daemon root plus the real CLI that talks to it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        environment = isolated_environment(root)
        self.home = Path(environment["HOME"])
        self.hyprial_home = root / "hyprial"
        self.state = root / "state"
        for path in (self.home, self.hyprial_home, self.state):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        home_with_node_id(self.hyprial_home)
        self.write_owner(OWNER)
        self.socket_path = self.state / "daemon.sock"
        environment.update({
            "HOME": str(self.home),
            "HYPRIAL_HOME": str(self.hyprial_home),
            "HARNESS_STATE_DIR": str(self.state),
            # Endpoints-less and discovery-free: this run cannot reach, or be
            # reached by, any real mesh.
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "UV_TOOL_DIR": str(root / "uv-tools"),
        })
        # The suite's autouse fixtures supply an identity, a node id and worker
        # bindings for in-process tests; this run's identity comes from its own
        # settings.json and its node id from its own root, so no ambient value
        # may leak into either subprocess.
        for key in (
            "HARNESS_SOCKET_PATH",
            "HYPRIAL_OWNER",
            "HYPRIAL_NODE_ID",
            "HYPRIAL_MANAGED_WORKER",
            "HYPRIAL_WORKER_ACTOR",
            "HYPRIAL_WORKER_SESSION_REF",
            "HYPRIAL_TOOL_PROFILE_ID",
        ):
            environment.pop(key, None)
        # Pin the two invariants the review asked for, so a future edit cannot
        # silently reintroduce an ambient node id or the runner's HOME.
        assert "HYPRIAL_NODE_ID" not in environment
        assert environment["HOME"] == str(self.home)
        assert_root_local_environment(root, environment)
        self.environment = environment
        self.daemon: subprocess.Popen[bytes] | None = None

    def write_owner(self, owner: str) -> None:
        (self.hyprial_home / "settings.json").write_text(
            json.dumps({"version": 1, "owner": owner}), encoding="utf-8"
        )

    def start(self) -> None:
        assert self.daemon is None
        self.daemon = subprocess.Popen(
            [sys.executable, "-m", "hyprial.cli", "daemon", "run"],
            env=self.environment,
            stdout=(self.root / "daemon.out").open("ab"),
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._ready():
                return
            time.sleep(0.2)
        raise AssertionError(
            "daemon did not become ready\n"
            + (self.root / "daemon.out").read_text(errors="replace")[-8000:]
        )

    def stop(self) -> None:
        daemon, self.daemon = self.daemon, None
        assert daemon is not None
        daemon.terminate()
        daemon.wait(timeout=60)

    def close(self) -> None:
        if self.daemon is not None:
            self.stop()

    def _ready(self) -> bool:
        frame = json.dumps(
            {
                "version": 1,
                "id": uuid.uuid4().hex,
                "method": "ping",
                "params": {},
            },
            separators=(",", ":"),
        ).encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(_SOCKET_TIMEOUT_SECONDS)
                client.connect(str(self.socket_path))
                client.sendall(frame + b"\n")
                buffer = bytearray()
                while b"\n" not in buffer:
                    chunk = client.recv(64 * 1024)
                    if not chunk:
                        return False
                    buffer.extend(chunk)
        except OSError:
            return False
        answer = json.loads(buffer.partition(b"\n")[0]).get("result") or {}
        return (
            answer.get("running") is True
            and answer.get("restorePending") is not True
        )

    def run(self, *args: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, "-m", "hyprial.cli", *args, "--json"],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"non-JSON stdout rc={completed.returncode} "
                f"out={completed.stdout!r} err={completed.stderr[-2000:]!r}"
            ) from exc
        assert isinstance(payload, dict)
        return completed.returncode, payload

    def ok(self, *args: str) -> dict:
        code, payload = self.run(*args)
        assert (code, payload.get("ok")) == (0, True), payload
        return payload

    def refused(self, *args: str) -> dict:
        code, payload = self.run(*args)
        assert (code, payload.get("ok")) == (1, False), payload
        return payload
