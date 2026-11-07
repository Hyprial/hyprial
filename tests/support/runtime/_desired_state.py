"""Shared desired-state seeders for application/identity runtime tests.

Lives here rather than inside one test module because more than one suite
consumes it (daemon application runtime, identity provider availability);
cross-importing helpers between test modules breaks the moment either side
renames.
"""

from __future__ import annotations

import json
from pathlib import Path


def write_v1(path: Path, *, as_mailbox: bool = False) -> None:
    """Seed a v1 desired-state file with one managed pi provider.

    The launch contract (``HARNESS_CWD_MISSING``) rejects an explicit cwd
    that does not exist on this host, so the provider points at a real
    directory created beside the state file instead of a machine-specific
    absolute path.
    """

    workspace = path.parent / "workspace"
    workspace.mkdir(exist_ok=True)
    Path(f"{path}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": as_mailbox,
                "providers": [
                    {
                        "provider": "pi",
                        "name": "squire",
                        "headless": True,
                        "cwd": str(workspace),
                        "args": [],
                        "ownership": "managed",
                    }
                ],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
