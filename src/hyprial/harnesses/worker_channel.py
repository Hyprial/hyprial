"""Per-worker Harness MCP identity for daemon-managed headless harnesses.

A daemon-launched headless worker must reach the Harness daemon as its OWN
canonical actor, not inherit the coordinator's ambient MCP configuration.  This
module mints the worker's harness-bridge MCP server config -- pinned to this
daemon with the worker's identity -- so the worker's ``harness_whoami`` returns
the worker, ``harness_read`` reads the worker's canonical inbox, and
``harness_send`` signs as the worker.  The daemon registers the same
``(actor, sessionRef)`` as a session its fence accepts (see
``DaemonApplication._worker_sessions``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from hyprial.home import child_state_environment

# The harness tools a managed worker is pre-authorized to call, mirroring the
# interactive path's allow-list, so a headless worker's proactive send or read
# never stalls on an unanswerable permission prompt.
WORKER_HARNESS_TOOLS = (
    "harness_ack",
    "harness_delegate",
    "harness_progress",
    "harness_read",
    "harness_reply",
    "harness_send",
    "harness_targets",
    "harness_whoami",
)


@dataclass(frozen=True, slots=True)
class WorkerChannel:
    """One managed worker's own Harness identity and daemon wiring."""

    actor: str
    session_ref: str
    hyprial_home: Path
    state_dir: Path
    mcp_server: dict[str, object]
    allowed_tools: tuple[str, ...]

    def pi_environment(self) -> dict[str, str]:
        """The pi carrier for this channel: identity + daemon pinning as env.

        Pi has no MCP support, so the worker's harness tools arrive via the
        bundled ``pi_harness_bridge.ts`` extension instead of an MCP server;
        the extension reads this environment to sign daemon IPC calls with the
        worker's own canonical actor and to reach THIS daemon's socket.
        """

        return {
            "HYPRIAL_WORKER_ACTOR": self.actor,
            "HYPRIAL_WORKER_SESSION_REF": self.session_ref,
            **child_state_environment(self.hyprial_home, self.state_dir),
        }


def build_worker_channel(
    *,
    actor: str,
    session_ref: str,
    hyprial_home: Path,
    state_dir: Path,
    python_executable: str | None = None,
) -> WorkerChannel:
    """Compose the worker's harness-bridge stdio server pinned to this daemon.

    The server runs ``hyprial mcp agent-channel`` with the worker's canonical actor
    and a fresh session ref; its environment is sourced from the daemon (not the
    worker's inherited environment) so the stdio child always targets this
    daemon's socket even under an ambient production override.
    """

    if not actor or not session_ref:
        raise ValueError("worker channel requires actor and session_ref")
    server: dict[str, object] = {
        "type": "stdio",
        "command": python_executable or sys.executable,
        "args": [
            "-m",
            "hyprial.cli",
            "mcp",
            "agent-channel",
            "--actor",
            actor,
            "--session-ref",
            session_ref,
        ],
        "env": child_state_environment(hyprial_home, state_dir),
    }
    return WorkerChannel(
        actor=actor,
        session_ref=session_ref,
        hyprial_home=hyprial_home,
        state_dir=state_dir,
        mcp_server=server,
        allowed_tools=tuple(
            f"mcp__harness-bridge__{tool}" for tool in WORKER_HARNESS_TOOLS
        ),
    )
