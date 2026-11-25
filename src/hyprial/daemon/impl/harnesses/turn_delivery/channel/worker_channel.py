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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from hyprial.kernel import child_state_environment
from hyprial.daemon.impl.harnesses.turn_delivery.channel.templates import (
    load_worker_channel_template,
)

if TYPE_CHECKING:
    from hyprial.identity import AgentRuntimeContext

WORKER_HARNESS_TOOLS = load_worker_channel_template("managed-worker").harness_tools


@dataclass(frozen=True, slots=True)
class WorkerAuthorityPolicy:
    """The adapter-produced launch option bundle for one managed worker."""

    launch_options: Mapping[str, object]
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.launch_options, Mapping):
            raise ValueError("worker authority launch options must be a mapping")
        options = dict(self.launch_options)
        if set(options) - {"allowedTools", "disallowedTools"}:
            raise ValueError(
                "worker authority launch options contain unsupported keys"
            )
        allowed = options.get("allowedTools")
        if not isinstance(allowed, list) or any(
            not isinstance(item, str) or not item for item in allowed
        ):
            raise ValueError(
                "worker authority allowedTools must be a list of non-empty strings"
            )
        denied = options.get("disallowedTools")
        if denied is not None and (
            not isinstance(denied, list)
            or any(not isinstance(item, str) or not item for item in denied)
        ):
            raise ValueError(
                "worker authority disallowedTools must be a list of non-empty strings"
            )
        if type(self.version) is not int or self.version < 1:
            raise ValueError("worker authority version must be a positive int")
        object.__setattr__(self, "launch_options", MappingProxyType(options))


@dataclass(frozen=True, slots=True)
class WorkerChannel:
    """One managed worker's own Harness identity and daemon wiring.

    ``allowed_tools``/``disallowed_tools`` are the member policy of the cell
    document (``authority``) when one is declared; without one the channel
    carries only its own bridge tool names and no deny rules.
    """

    actor: str
    session_ref: str
    hyprial_home: Path
    state_dir: Path
    mcp_server: dict[str, object]
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...] = ()
    authority: WorkerAuthorityPolicy | None = None
    # Daemon-authority identity (P1b B1 / L3): the node id and owner THIS
    # daemon resolved at startup.  They are generated values in the child
    # env sense — never passed through from a parent process environment,
    # never derived from the worker-actor URI.
    node_id: str = ""
    owner: str = ""
    runtime_context: "AgentRuntimeContext | None" = field(
        default=None, repr=False, compare=False
    )

    def identity_environment(self) -> dict[str, str]:
        """The worker's daemon-bound identity + daemon pinning as env.

        ``HYPRIAL_WORKER_ACTOR`` / ``HYPRIAL_WORKER_SESSION_REF`` let any
        harness carrier (and its subprocesses, e.g. a shell-out to
        ``workflow commands`) present the session binding this daemon minted to
        the fenced write methods.  ``HYPRIAL_MANAGED_WORKER`` is the
        worker-context MARKER (M2): marker present without the binding pair
        is a loud CLI refusal, never a silent fall-back to the human
        identity.  The pair is already in P1a's
        ``agents.environment.GENERATED_CHILD_ENVIRONMENT_NAMES`` (PR #513);
        the marker must be added alongside before the P1b cutover
        (cross-point noted in PR #536).  Boundary note (accepted risk,
        Allen 2026-09-14:
        「先不用考虑进程隔离的问题，未来将agent容器化后自然可以避免其它agent读取」):
        every subprocess of the worker inherits all three.
        """

        environment = {
            "HYPRIAL_WORKER_ACTOR": self.actor,
            "HYPRIAL_WORKER_SESSION_REF": self.session_ref,
            "HYPRIAL_MANAGED_WORKER": "1",
            **child_state_environment(self.hyprial_home, self.state_dir),
        }
        # L3 (B1): daemon-authority node/owner.  Older channels constructed
        # before the fields existed keep empty strings and omit the vars —
        # the omission is visible in the frozen env table, not silent.
        if self.node_id:
            environment["HYPRIAL_NODE_ID"] = self.node_id
        if self.owner:
            environment["HYPRIAL_OWNER"] = self.owner
        return environment

    def pi_environment(self) -> dict[str, str]:
        """The pi carrier for this channel: identity + daemon pinning as env.

        Pi has no MCP support, so the worker's harness tools arrive via the
        bundled ``pi_harness_bridge.ts`` extension instead of an MCP server;
        the extension reads this environment to sign daemon IPC calls with the
        worker's own canonical actor and to reach THIS daemon's socket.
        """

        return self.identity_environment()


def build_worker_channel(
    *,
    actor: str,
    session_ref: str,
    hyprial_home: Path,
    state_dir: Path,
    python_executable: str | None = None,
    node_id: str = "",
    owner: str = "",
    runtime_context: "AgentRuntimeContext | None" = None,
    authority: WorkerAuthorityPolicy | None = None,
    template_name: str = "managed-worker",
) -> WorkerChannel:
    """Compose the worker's harness-bridge stdio server pinned to this daemon.

    The server runs the module-private ``hyprial.daemon.impl.mcp.entry
    agent-channel`` process with the worker's canonical actor and a fresh
    session ref; its environment is sourced from the daemon (not the worker's
    inherited environment) so the stdio child always targets this daemon's
    socket even under an ambient production override.  The tool lists come
    from *authority* (the member's cell-document policy) or, without one, are
    the bridge tool names alone: there is no in-code CLI allow or deny list.
    """

    if not actor or not session_ref:
        raise ValueError("worker channel requires actor and session_ref")
    template = load_worker_channel_template(template_name)
    server: dict[str, object] = {
        "type": "stdio",
        "command": python_executable or sys.executable,
        "args": [
            "-m",
            "hyprial.daemon.impl.mcp.entry",
            "agent-channel",
            "--actor",
            actor,
        ],
        "env": {
            **child_state_environment(hyprial_home, state_dir),
            "HYPRIAL_WORKER_SESSION_REF": session_ref,
        },
    }
    return WorkerChannel(
        actor=actor,
        session_ref=session_ref,
        node_id=node_id,
        owner=owner,
        runtime_context=runtime_context,
        hyprial_home=hyprial_home,
        state_dir=state_dir,
        mcp_server=server,
        allowed_tools=(
            tuple(authority.launch_options["allowedTools"])
            if authority is not None
            else template.bridge_tools()
        ),
        disallowed_tools=(
            tuple(authority.launch_options.get("disallowedTools", ()))
            if authority is not None
            else ()
        ),
        authority=authority,
    )
