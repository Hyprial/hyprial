"""Claude Code ``Stop`` hook: record one turn-ended pulse for the channel child.

This module is the hook's entry point, so it imports only the standard library:
the hook runs once per turn under a Claude Code timeout, and importing
``hyprial.mcp`` or ``hyprial.cli`` costs about a second on a loaded host,
enough to be cancelled and silently lose the pulse.  The channel child owns the
directory and forwards pulses through its session fence
(``hyprial.mcp.channel``).
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


def signal_turn_ended(path: Path) -> None:
    """Create one durable, per-launch Stop pulse without daemon I/O."""

    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    for _attempt in range(3):
        pulse = path / secrets.token_hex(16)
        try:
            descriptor = os.open(pulse, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        return
    raise OSError("could not allocate a unique turn pulse")


def main(argv: list[str] | None = None) -> int:
    """Hook entry point: silent, and exit 0 on every error.

    A Stop hook is telemetry, never a gate: output could reach Claude's
    context, and exit 2 would continue the conversation.
    """

    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 2 or args[0] != "--signal-dir":
            return 0
        signal_turn_ended(Path(args[1]).expanduser().resolve())
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
