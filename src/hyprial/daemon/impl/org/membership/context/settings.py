"""Current organization is daemon-local state, never an OrgFS membership fact."""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.kernel import DaemonRequestError, atomic_json_write, ipc_errors


def read_current(home: Path) -> str | None:
    try:
        value = json.loads((Path(home) / "state/org/current.json").read_text("utf-8"))
        if not isinstance(value, dict) or set(value) != {"current"}:
            raise ValueError("invalid current org settings")
        current = value["current"]
        if current is not None and not isinstance(current, str):
            raise ValueError("invalid current org value")
        return current
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise DaemonRequestError(ipc_errors.DAEMON_NOT_READY, "current org settings are unreadable") from error


def write_current(home: Path, current: str | None) -> None:
    try:
        atomic_json_write(Path(home) / "state/org/current.json", {"current": current})
    except OSError as error:
        raise DaemonRequestError(ipc_errors.DAEMON_NOT_READY, "cannot save current org") from error
