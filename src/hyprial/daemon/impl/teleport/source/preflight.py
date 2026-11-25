# Decision record (file header; no inline comments by repository rule):
# - §4 step 0 is local and pure: every refusal here happens before any request
#   leaves the node and before anything is stopped.
# - Only agents with a home, a headless claude/codex/pi harness, no pinned
#   adapter and a cwd that is unset or exactly ``<home>/workspace`` move.  The
#   target always lands the workspace inside the landed home, so a cwd
#   elsewhere would silently stop pointing at the moved files.
# - ``select_routines`` returns every routine the port lists for the agent,
#   enabled or not: all of them travel with it (slice 6).
"""Local preflight checks of an agent teleport."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.teleport.source.errors import TeleportRunError
from hyprial.daemon.impl.teleport.source.ports import RoutineRef, SourceAgent

SUPPORTED_HARNESSES = ("claude", "codex", "pi")


def _unsupported(message: str) -> TeleportRunError:
    return TeleportRunError("TELEPORT_RUNTIME_UNSUPPORTED", message)


def _is_workspace(cwd: Path, home: Path) -> bool:
    expected = home / "workspace"
    if cwd == expected:
        return True
    try:
        return cwd.resolve() == expected.resolve()
    except OSError:
        return False


def check_agent(agent: SourceAgent, *, to: str, node_id: str) -> None:
    """Refuse an agent or target that cannot take part in a teleport."""

    if agent.home is None:
        raise _unsupported(
            f"agent {agent.name!r} has no agent home; run "
            f"`hyprial agent ensure-home {agent.name}` first"
        )
    if not agent.headless:
        raise _unsupported(f"agent {agent.name!r} is not a headless worker")
    if agent.harness not in SUPPORTED_HARNESSES:
        raise _unsupported(
            f"harness {agent.harness!r} cannot be teleported; supported: "
            + ", ".join(SUPPORTED_HARNESSES)
        )
    if agent.adapter_bindings is None:
        raise _unsupported(
            f"adapter bindings of agent {agent.name!r} are unknown (cell "
            "runtime unavailable or its cell is not loaded); retry later"
        )
    if agent.adapter_bindings:
        raise _unsupported(
            f"agent {agent.name!r} is bound to adapters "
            f"({', '.join(agent.adapter_bindings)}); remove the cell adapter "
            "member first"
        )
    if agent.cwd is not None and not _is_workspace(agent.cwd, agent.home):
        raise _unsupported(
            f"agent {agent.name!r} runs in {agent.cwd}, outside its home "
            f"workspace {agent.home / 'workspace'}"
        )
    if to == node_id:
        raise TeleportRunError(
            "TELEPORT_WRONG_TARGET", f"{to!r} is this node; pick another target"
        )


def select_routines(listed: list[RoutineRef]) -> tuple[RoutineRef, ...]:
    """The routines that travel with the agent: every listed one, in order."""

    return tuple(listed)
