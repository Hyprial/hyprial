"""Low-level CLI and IPC wrappers shared by the group-conversation scenario.

Every helper here talks to one isolated daemon through its public CLI or its
own socket.  Nothing reaches production: the daemon, home, socket and ports
come from the caller's daemon runner.  Moved from ``_send_links/cli.py`` in
T1b when the E2E-026/027 scenarios were deleted; the session-lease and
pending-row helpers are scenario-independent.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.kernel import CHANNEL_HEARTBEAT_INTERVAL_SECONDS, CHANNEL_PROTOCOL_VERSION


def run_cli(
    daemon: Any, *args: str, timeout: float = 30.0
) -> tuple[int, str, str, dict[str, Any] | None]:
    """Run one hyprial command, returning rc, stdout, stderr and parsed JSON."""

    completed = subprocess.run(
        [str(daemon.executable), *args],
        env=daemon.env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    parsed: dict[str, Any] | None = None
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        parsed = value
    return completed.returncode, completed.stdout, completed.stderr, parsed


def cli(daemon: Any, *args: str, exits: tuple[int, ...] = (0,), timeout: float = 30.0) -> dict[str, Any]:
    """Run one hyprial command through the runner's asserting wrapper."""

    return daemon.command(*args, exits=exits, timeout=timeout)


def ipc(daemon: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """One raw daemon IPC call over the isolated socket."""

    return daemon.request(method, params)


def register_session(
    daemon: Any, actor: str, session_ref: str, cwd: Path
) -> dict[str, Any]:
    """Register and confirm one current channel with a fresh operational lease."""

    cwd.mkdir(parents=True, exist_ok=True)
    lease = uuid4().hex
    result = ipc(
        daemon,
        "session.register",
        {
            "actor": actor,
            "sessionRef": session_ref,
            "cwd": str(cwd),
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
            "channelProtocolVersion": CHANNEL_PROTOCOL_VERSION,
            "channelLeaseToken": lease,
        },
    )
    if result.get("registered") is not True:
        raise AssertionError(f"session {actor!r} did not register: {result}")
    _heartbeat(daemon, _Channel(actor, session_ref, cwd, lease))
    bindings = getattr(daemon, "_channels", {})
    bindings[actor] = _Channel(actor, session_ref, cwd, lease)
    daemon._channels = bindings
    return result


@dataclass(frozen=True)
class _Channel:
    actor: str
    session_ref: str
    cwd: Path
    lease: str = field(repr=False)


def _heartbeat(daemon: Any, channel: _Channel) -> None:
    result = ipc(daemon, "session.heartbeat", {
        "actor": channel.actor,
        "sessionRef": channel.session_ref,
        "channelLeaseToken": channel.lease,
    })
    if result.get("alive") is not True:
        raise AssertionError(f"session {channel.actor!r} heartbeat was not live: {result}")


class SessionHeartbeat:
    """Own channel heartbeats independently of business polling and CLI latency."""

    def __init__(self, daemon: Any):
        self.daemon = daemon
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None

    def beat(self) -> None:
        for channel in tuple(getattr(self.daemon, "_channels", {}).values()):
            _heartbeat(self.daemon, channel)

    def start(self) -> None:
        if self.thread is not None:
            raise AssertionError("channel heartbeat is already started")
        self.error = None
        self.stopping.clear()
        self.beat()
        self.thread = threading.Thread(
            target=self._run, name="e2e-channel-heartbeat", daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        while not self.stopping.wait(CHANNEL_HEARTBEAT_INTERVAL_SECONDS):
            try:
                self.beat()
            except Exception as error:
                self.error = error
                return

    def check(self) -> None:
        if self.error is not None:
            raise AssertionError(f"channel heartbeat failed: {self.error}") from self.error

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=20.0)
            if self.thread.is_alive():
                raise AssertionError("channel heartbeat did not stop within 20s")
            self.thread = None
        self.check()

    def reconnect(self) -> None:
        if self.thread is not None:
            raise AssertionError("channel heartbeat must stop before reconnect")
        for channel in tuple(getattr(self.daemon, "_channels", {}).values()):
            register_session(self.daemon, channel.actor, channel.session_ref, channel.cwd)
        self.start()


def pending_rows(daemon: Any, actor: str, session_ref: str, *, fetched: bool = False) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"actor": actor, "sessionRef": session_ref}
    if fetched:
        params["fetched"] = True
    result = ipc(daemon, "message.pending.list", params)
    messages = result.get("messages")
    return [item for item in messages if isinstance(item, dict)] if isinstance(messages, list) else []


def pending_ids(daemon: Any, actor: str, session_ref: str, *, fetched: bool = False) -> list[str]:
    return [str(row["messageId"]) for row in pending_rows(daemon, actor, session_ref, fetched=fetched)]


def wait_for(description: str, probe, accept, *, timeout: float, interval: float = 0.25):
    """Poll ``probe`` until ``accept`` holds, or fail with the last reading."""

    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if accept(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"{description} did not happen within {timeout:g}s; last={last!r}")


def agent_uri(daemon: Any, name: str) -> str:
    """Use the registered agent's official address instead of inventing one."""

    agents = cli(daemon, "agent", "list", "--json").get("agents", [])
    record = next((item for item in agents if item.get("actor") == name), None)
    uri = record.get("uri") if isinstance(record, dict) else None
    if not isinstance(uri, str) or not uri.startswith("agent:") or ":a." not in uri:
        raise AssertionError(f"agent {name!r} did not publish an id address: {record}")
    return uri


def owner(daemon: Any) -> str:
    status = daemon.command("daemon", "status", "--json")
    daemon_status = status.get("daemon")
    value = daemon_status.get("owner") if isinstance(daemon_status, dict) else None
    if not isinstance(value, str) or not value:
        raise AssertionError(f"daemon owner missing from ps: {status}")
    return value
