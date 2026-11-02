"""Previous-release machine argv compatibility for the process entry.

These argv forms are emitted by 0.5.4 into timers, harness configs and hooks.
They deliberately bypass Typer registration: machine continuity must not
recreate retired operator commands or expose them in any help inventory.
Keeping the rewrite table here gives tests and the entry one owner that is
not the command-registration surface.
"""
from __future__ import annotations

from dataclasses import dataclass

_LEGACY_CLI_REWRITES = {
    ("autoupdate", "run"): ("daemon", "upgrade", "run"),
    ("upgrade", "run"): ("daemon", "upgrade", "run"),
}
_LEGACY_MCP_SUBCOMMANDS = frozenset(
    {
        "agent-channel",
        "claude-channel",
        "claude-channel-recover",
        "dsh-carrier",
    }
)


@dataclass(frozen=True, slots=True)
class _MachineCompatibility:
    target: str
    argv: tuple[str, ...]


def resolve_machine_compatibility(
    argv: tuple[str, ...],
) -> _MachineCompatibility | None:
    prefix = argv[:2]
    replacement = _LEGACY_CLI_REWRITES.get(prefix)
    if replacement is not None and argv[2:] in {(), ("--json",)}:
        return _MachineCompatibility("typer", (*replacement, *argv[2:]))
    if (
        len(argv) >= 2
        and argv[0] == "mcp"
        and argv[1] in _LEGACY_MCP_SUBCOMMANDS
        and not {"-h", "--help"}.intersection(argv[2:])
    ):
        return _MachineCompatibility("mcp", argv[1:])
    return None


def run_machine_mcp_entry(argv: tuple[str, ...]) -> int:
    from hyprial.daemon import run_mcp_entry

    return run_mcp_entry(list(argv))
