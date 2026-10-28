"""Transfer admission: which table speaks for a name, and its entrypoint.

A migration name is resolved against BOTH tables the daemon keeps --
``state.harnesses`` (managed headless workers) and
``state.interactive_sessions`` (CLI-hosted TUI sessions).  Exactly one table
must speak for the name: a name present in both is an ambiguity the operator
resolves, never a silent "first table wins".  The hit carries the
``entrypoint`` the P2 fence keys on, so a TUI can never fall through a
headless-only assumption.

This module owns no storage and no IPC: it reads a ``DesiredState`` value and
turns it into a decision (or a refusal with its wire code).  The caller keeps
the authority to raise and to run the rest of the transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyprial.kernel import HarnessLaunchSpec, agent_uri_actor, ipc_errors

from hyprial.daemon.impl.desired_state import DesiredState, InteractiveSession
from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
    ENTRYPOINT_HEADLESS,
    ENTRYPOINT_TUI,
    P2_CHAIN_MODE,
    chain_is_proven,
)

#: The harness behind an interactive carrier session.  The session document
#: does not store a harness; the runtime prefix is authoritative when present
#: and the carrier ``source`` is the fallback, exactly as the pre-cutover
#: session-agent import derives it (``composition/domains.py``).
_SESSION_RUNTIME_HARNESSES = frozenset({"claude", "pi", "codex"})
_SESSION_SOURCE_HARNESSES = {
    "claude-channel": "claude",
    "pi-extension": "pi",
    "codex-app-server": "codex",
}


class TransferAdmissionError(ValueError):
    """A transfer refusal that already carries its wire code and data."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = dict(data or {})


@dataclass(frozen=True, slots=True)
class TransferTarget:
    """The one table that spoke for a transfer name, and its entrypoint."""

    name: str
    harness: str
    entrypoint: str
    spec: HarnessLaunchSpec | None = None
    session: InteractiveSession | None = None


def session_harness(session: InteractiveSession) -> str | None:
    """The harness behind one interactive session, or ``None`` when unreadable."""

    runtime = (session.runtime or "").split("_", 1)[0]
    if runtime in _SESSION_RUNTIME_HARNESSES:
        return runtime
    return _SESSION_SOURCE_HARNESSES.get(session.source)


def _session_local_name(session: InteractiveSession) -> str:
    return agent_uri_actor(session.actor) or session.actor


def resolve_transfer_target(
    state: DesiredState, *, name: str, harness: str | None = None
) -> TransferTarget:
    """Resolve ``name`` across both tables; fail loudly on an ambiguity.

    A unique hit is used, whether it is a managed harness (``headless``) or an
    interactive session (``tui``).  A hit in both tables refuses with the same
    ``TRANSFER_AMBIGUOUS`` code the two-harness case uses: the operator must
    remove or rename one, never have one silently chosen.
    """

    from hyprial.daemon.impl.harnesses.capabilities import DECLARED_HARNESSES

    harness_matches = [
        spec
        for spec in state.harnesses
        if spec.name == name and spec.harness in DECLARED_HARNESSES
        and (harness is None or spec.harness == harness)
    ]
    session_matches = [
        session
        for session in state.interactive_sessions
        if _session_local_name(session) == name
        and (harness is None or session_harness(session) == harness)
    ]
    if harness_matches and session_matches:
        raise TransferAdmissionError(
            ipc_errors.TRANSFER_AMBIGUOUS,
            f"{name!r} is registered in both managed harnesses and interactive "
            "sessions on this node; refusing instead of choosing one -- stop "
            "or rename one side and retry",
            {"name": name, "tables": ["providers", "interactiveSessions"]},
        )
    if len(harness_matches) > 1:
        raise TransferAdmissionError(
            ipc_errors.TRANSFER_AMBIGUOUS,
            f"more than one managed harness named {name!r}; "
            "pass --harness to disambiguate",
            {"name": name},
        )
    if session_matches:
        session = session_matches[0]
        resolved = session_harness(session)
        if resolved is None:
            raise TransferAdmissionError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"interactive session {name!r} does not name a transferable "
                "harness",
                {"name": name, "entrypoint": ENTRYPOINT_TUI},
            )
        return TransferTarget(name, resolved, ENTRYPOINT_TUI, None, session)
    if harness_matches:
        spec = harness_matches[0]
        return TransferTarget(name, spec.harness, ENTRYPOINT_HEADLESS, spec, None)
    raise TransferAdmissionError(
        ipc_errors.TRANSFER_WORKER_NOT_FOUND,
        f"no managed harness or interactive session named {name!r} in "
        "desired state",
        {"name": name},
    )


def transferable_spec(target: TransferTarget) -> HarnessLaunchSpec:
    """The entrypoint fence, then the headless spec the transport needs.

    A (harness, mode, entrypoint) no run proved is refused BEFORE any
    headless-only assumption (``is_streaming_spec``) is made, so a TUI can
    never be read as a streaming worker.  ``codex`` gets an explicit message
    because its refusal is not a silent gap: the codex ``--tmux`` detached
    form now exists (S5), but no two-daemon run has proved the cross-machine
    TUI for codex yet, so the same unproven-entrypoint fence applies and is
    stated explicitly for the harness the fence names.
    """

    if target.entrypoint == ENTRYPOINT_HEADLESS:
        if target.spec is not None:
            return target.spec
        raise TransferAdmissionError(
            ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
            f"{target.harness}:{target.name} has no headless launch spec",
            {"harness": target.harness, "name": target.name},
        )
    data = {
        "harness": target.harness,
        "name": target.name,
        "entrypoint": target.entrypoint,
        "mode": P2_CHAIN_MODE,
    }
    if target.harness == "codex":
        # The detached form (S5) now exists, so this is no longer "codex has
        # no --tmux"; it is the same unproven-entrypoint fence as every other
        # TUI, stated explicitly for the harness the fence names.
        raise TransferAdmissionError(
            ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
            "codex TUI sessions cannot be transferred yet: the codex --tmux "
            "detached form now exists, but no two-daemon run has proved the "
            "cross-machine TUI for codex, so the entrypoint fence refuses it",
            data,
        )
    if not chain_is_proven(target.harness, P2_CHAIN_MODE, target.entrypoint):
        raise TransferAdmissionError(
            ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
            f"agent-home P2 transfer is unsupported for {target.harness}:"
            f"{target.name} entrypoint={target.entrypoint}: the P2 chain has "
            "not been proven on two real daemons for this entrypoint; "
            "refusing to transfer the interactive TUI",
            data,
        )
    raise TransferAdmissionError(
        ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
        f"agent-home P2 transfer for {target.harness}:{target.name} "
        f"entrypoint={target.entrypoint} is proven but the TUI transport is "
        "not implemented in this slice",
        data,
    )
