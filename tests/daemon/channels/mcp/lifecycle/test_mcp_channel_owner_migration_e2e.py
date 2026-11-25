"""Shared isolated harness for real-daemon claude Channel lifecycle tests.

``IsolatedHarness`` runs a REAL daemon subprocess and REAL ``python -m
hyprial.daemon.impl.mcp.entry claude-channel`` stdio children under an isolated
HYPRIAL_HOME/HARNESS_STATE_DIR/UV_TOOL_DIR; other channel tests import it.  The
owner-switch scenario that once lived here was deleted with the owner migration
(L3): a home is bound to one account and its owner never changes in place.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import assert_root_local_environment, isolated_environment

import inspect
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

pytestmark = pytest.mark.anyio

OWNER_A = "owner-a"
OWNER_B = "owner-b"
NODE = "node-e2e"
ACTOR = "worker"
SESSION_REF = "ref-e2e"
ACTOR_A = f"agent:{OWNER_A}:{NODE}:{ACTOR}"
ACTOR_B = f"agent:{OWNER_B}:{NODE}:{ACTOR}"


async def _wait_until(
    predicate: Any, *, timeout: float = 30.0, interval: float = 0.2
) -> None:
    """Async on purpose: a blocking sleep here would stall the very stdio
    child this file observes (its notifications ride the same event loop)."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        outcome = predicate()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return
        await anyio.sleep(interval)
    raise AssertionError("condition was not met within the timeout")


def _blocking_wait_until(
    predicate: Any, *, timeout: float = 30.0, interval: float = 0.2
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not met within the timeout")


class IsolatedHarness:
    """One isolated daemon home plus IPC plumbing; teardown is by PID."""

    def __init__(self, root: Path, *, membership: bool = False) -> None:
        self.membership = membership
        self.root = root
        self.home = root / "home"
        self.state = root / "state"
        self.home.mkdir(parents=True, mode=0o700)
        self.state.mkdir(parents=True)
        self.socket_path = self.state / "daemon.sock"
        environment = isolated_environment(root)
        environment.update({
            "HYPRIAL_HOME": str(self.home),
            "HARNESS_STATE_DIR": str(self.state),
            "HYPRIAL_NODE_ID": NODE,
            # An endpoints-less node is deliberately isolated: this run must
            # not touch (or be touched by) any real mesh.
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            # WP2: the real link gate is wired now.  Link files live in an org
            # directory space, so this scenario creates one under each owner
            # instead of binding a permissive test double.
            # Peer discovery would derive this "isolated" node's tailnet
            # address and dial real peers; off keeps the endpoint lists empty
            # and the daemon truly standalone.
            "HYPRIAL_PEER_DISCOVERY": "0",
            "UV_TOOL_DIR": str(root / "uv-tools"),
        })
        # The suite's autouse fixtures pin HYPRIAL_OWNER for in-process tests; this
        # run's owner comes from the isolated settings.json (and moves A -> B),
        # so the injected value must not leak into either subprocess.
        environment.pop("HYPRIAL_OWNER", None)
        assert_root_local_environment(root, environment)
        self.environment = environment
        self.daemon: subprocess.Popen[Any] | None = None

    def write_owner(self, owner: str) -> None:
        (self.home / "settings.json").write_text(
            json.dumps({"owner": owner}), encoding="utf-8"
        )

    def start_daemon(self, *, command: list[str] | None = None, pass_fds: tuple[int, ...] = ()) -> None:
        assert self.daemon is None
        self.daemon = subprocess.Popen(
            command or [
                sys.executable, "-m",
                "tests.support.tailnet_daemon" if self.membership else "hyprial.cli",
                "daemon", "run",
            ],
            env=self.environment,
            pass_fds=pass_fds,
            stdout=(self.root / "daemon.out").open("w"),
            stderr=subprocess.STDOUT,
        )
        # Daemon bring-up happens before any async context: blocking waits
        # are fine here and only here.
        try:
            _blocking_wait_until(
                lambda: self.socket_path.exists() and self._ready(), timeout=60.0
            )
        except AssertionError:
            parts = []
            for name in ("daemon.out", "state/logs/daemon.jsonl"):
                path = self.root / name
                if path.exists():
                    parts.append(
                        f"--- {name} tail ---\n"
                        + path.read_text(errors="replace")[-8000:]
                    )
            raise AssertionError(
                "daemon did not become ready;\n" + "\n".join(parts)
            ) from None

    def stop_daemon(self) -> None:
        daemon, self.daemon = self.daemon, None
        assert daemon is not None
        # By PID only — never a pattern-matched kill.
        daemon.terminate()
        daemon.wait(timeout=30)

    def _ready(self) -> bool:
        """Serving AND restore settled -- the milestone the test's next calls need.

        `ps` is a heavy method: with the readiness split (#332) the daemon
        binds its socket at the serving boundary and refuses heavy methods
        with DAEMON_RESTORING until restore settles; `ipc` raises on that
        envelope, so polling `ps` turned a normal mid-restore answer into
        "daemon did not become ready" whenever the poller beat the restore
        thread (dev push a7727aa8, unit 4514, under full-suite load).  Ask
        the light `ping` instead and wait for `restorePending` to clear: the
        first calls this test makes (agent.create, session registration) are
        heavy and need exactly that.
        """
        try:
            answer = self.ipc("ping", {})
        except (OSError, AssertionError):
            return False
        return answer.get("running") is True and answer.get("restorePending") is not True

    def ipc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        # A fresh request id per call: sends derive their idempotency key from
        # it, so a fixed id makes the second message a duplicate of the first.
        frame = json.dumps(
            {"version": 1, "id": uuid.uuid4().hex, "method": method, "params": params},
            separators=(",", ":"),
        ).encode()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(15.0)
        try:
            client.connect(str(self.socket_path))
            client.sendall(frame + b"\n")
            response = bytearray()
            while b"\n" not in response:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    raise OSError("daemon closed IPC before responding")
                response.extend(chunk)
        finally:
            client.close()
        document = json.loads(response.partition(b"\n")[0])
        error = document.get("error")
        if isinstance(error, dict):
            raise AssertionError(f"daemon rejected {method}: {error}")
        result = document.get("result")
        assert isinstance(result, dict)
        return result

    def channel_command(self) -> list[str]:
        # F1 contract: the child carries the BARE actor name.
        return [
            sys.executable,
            "-m",
            "hyprial.daemon.impl.mcp.entry",
            "claude-channel",
            "--actor",
            ACTOR,
            "--session-ref",
            SESSION_REF,
            "--cwd",
            str(self.root),
            "--command",
            "claude",
        ]

    def diagnostics(self) -> str:
        parts = [f"ps sessions: {self._safe_ps()}"]
        parts.append(
            "tree: "
            + "\n".join(
                str(path.relative_to(self.root))
                for path in sorted(self.root.rglob("*"))
                if path.is_file()
            )
        )
        for path in sorted(self.root.rglob("*.jsonl")):
            text = path.read_text(errors="replace")
            session_lines = [
                line for line in text.splitlines() if "session" in line or "ipc" in line
            ]
            parts.append(
                f"--- {path.relative_to(self.root)} ({len(text)} bytes) ---\n"
                + ("\n".join(session_lines[-15:]) or text[-1500:])
            )
        return "\n".join(parts)

    def _safe_ps(self) -> str:
        try:
            result = self.ipc("ps", {})
            return json.dumps(
                {
                    "daemon": result.get("daemon"),
                    "interactiveSessions": result.get("interactiveSessions"),
                }
            )[:600]
        except Exception as error:  # noqa: BLE001 - diagnostics must not raise
            return f"<ps failed: {error}>"

    async def wait_for_online(self, expected_actor: str) -> None:
        """Wait until the (never-restarted) child is live on the new daemon."""

        def online() -> bool:
            sessions = self.ipc("ps", {}).get("interactiveSessions")
            if not isinstance(sessions, list):
                return False
            return any(
                isinstance(item, dict)
                and item.get("actor") == expected_actor
                and item.get("sessionRef") == SESSION_REF
                and item.get("online") is True
                and item.get("channelCurrentThisGeneration") is True
                for item in sessions
            )

        try:
            await _wait_until(online, timeout=45.0)
        except AssertionError as error:
            raise AssertionError(
                f"{error}\nchannel never came back online as {expected_actor}; "
                f"diagnostics:\n{self.diagnostics()}"
            ) from error

    async def registered_agent_uri(self, name: str) -> str:
        """The official address of agent ``name`` once its record exists."""

        found: list[str] = []

        def recorded() -> bool:
            try:
                agent = self.ipc("agent.get", {"name": name})["agent"]
            except (OSError, AssertionError):
                return False
            found.append(str(agent["uri"]))
            return True

        await _wait_until(recorded, timeout=45.0)
        return found[-1]

    async def wait_for_registration(self, expected_actor: str) -> None:
        def confirmed() -> bool:
            sessions = self.ipc("ps", {}).get("interactiveSessions")
            if not isinstance(sessions, list):
                return False
            return any(
                isinstance(item, dict)
                and item.get("actor") == expected_actor
                and item.get("sessionRef") == SESSION_REF
                and item.get("channelConfirmed") is True
                for item in sessions
            )

        try:
            await _wait_until(confirmed, timeout=45.0)
        except AssertionError as error:
            raise AssertionError(
                f"{error}\nchannel never registered as {expected_actor}; "
                f"diagnostics:\n{self.diagnostics()}"
            ) from error


@pytest.fixture()
def harness() -> Iterator[IsolatedHarness]:
    root = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprial-e2e-"))
    # Explicit local authority double; org.create retains its membership-init
    # prerequisite. The real CLI daemon and channel still run as subprocesses.
    value = IsolatedHarness(root, membership=True)
    try:
        yield value
    finally:
        if value.daemon is not None:
            value.stop_daemon()
        shutil.rmtree(root, ignore_errors=True)
