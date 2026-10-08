"""DSH worker home/env preparation and shared turn-timeout vocabulary."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import yaml

from hyprial.kernel import HarnessLaunchSpec

from hyprial.daemon.impl.harnesses.streaming.protocol  import (
    resolve_turn_timeout_seconds,
)

if TYPE_CHECKING:
    pass

# Old substring set (kept verbatim) UNION the boundary-limited ``key``/``auth``
# set.  The union is deliberate: the substring set catches names like
# ``DEEPSEEKAPIKEY`` / ``ACCESSTOKEN`` / ``BEARERTOKEN`` / ``API-KEY`` /
# ``DB_PASSWORDS``, the boundary set catches ``MY_KEY`` / ``SERVICE_AUTH``.
_SECRET_ENV_NAME = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|passwd|credential"
    r"|(?:^|_)(?:key|auth)(?:$|_))"
)

def _environment_secrets(environment: Mapping[str, str]) -> tuple[str, ...]:
    """The env values that must never appear in output, longest first."""

    values = {
        value
        for name, value in environment.items()
        if value and len(value) >= 8 and _SECRET_ENV_NAME.search(name)
    }
    return tuple(sorted(values, key=len, reverse=True))

_DSH_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")

def _option_value(args: tuple[str, ...], option: str) -> str | None:
    for index, value in enumerate(args):
        if value == option and index + 1 < len(args):
            return args[index + 1]
        prefix = f"{option}="
        if value.startswith(prefix):
            return value[len(prefix) :]
    return None

def _positive_float(value: str | None, *, default: float, label: str) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a number") from error
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed

def dsh_worker_home(state_dir: Path, name: str) -> Path:
    """The fixed private ``DSH_HOME`` for one managed dsh worker.

    ``DSH_HOME`` holds the session ``storages/``, the copied agent preset,
    and any user patch, so it must never be shared between workers.  The
    daemon already owns the only state root (``worker_channel.state_dir`` /
    ``DaemonApplication.state_dir``); this names the per-worker directory
    under it.  The name is sanitized because a DSH profile directory is a
    path component.
    """

    safe = _DSH_UNSAFE_NAME.sub("-", name)
    if not safe or safe in {".", ".."}:
        raise ValueError("DSH worker name cannot form a directory component")
    return Path(state_dir) / "dsh" / safe / "home"

def _validate_client_arguments(spec: HarnessLaunchSpec) -> None:
    """Parse the DSH client's own budgets before any child exists.

    A malformed ``--poll-interval`` / ``--turn-timeout`` (or malformed
    ``HYPRIAL_TURN_TIMEOUT_SECONDS``) is deterministic: it must fail once at
    construction instead of being re-parsed on every retry while the pump
    spawns a fresh child each time.
    """

    _positive_float(
        _option_value(spec.args, "--poll-interval"),
        default=0.25,
        label="--poll-interval",
    )
    _positive_float(
        _option_value(spec.args, "--turn-timeout"),
        default=resolve_turn_timeout_seconds(
            spec.turn_timeout_seconds,
            default=MANAGED_TURN_TIMEOUT_SECONDS,
        ),
        label="--turn-timeout",
    )

def _worker_web_patch() -> str:
    """The managed user patch that turns off the Web GUI surface context.

    A patch replaces the targeted row's whole ``config``, so all three
    ``web-runtime`` keys are restated.  ``printUrl`` must stay true: the
    banner it prints is the only machine-readable port channel.
    """

    rows = [
        {
            "id": "web-runtime",
            "config": {
                "printUrl": True,
                "surfaceContext": False,
                "trustedHosts": [],
            },
        }
    ]
    return yaml.safe_dump(rows, allow_unicode=True, default_flow_style=False, sort_keys=False)

def prepare_worker_home(home: Path) -> None:
    """Create the worker home and pin its managed Web-profile patch."""

    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    patch = home / "profiles" / "web" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = _worker_web_patch()
    try:
        if patch.read_text(encoding="utf-8") == content:
            return
    except (OSError, UnicodeError):
        pass
    temporary = patch.with_name(f".{patch.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(patch)
    finally:
        temporary.unlink(missing_ok=True)

# Retired wall-clock cap (#277: no timeout kills a turn).  The value is
# still resolved so the pre-existing ``--turn-timeout`` argument and the
# persisted ``turnTimeoutSeconds`` field keep parsing (old launch specs
# and callers must not crash), but nothing enforces it.
MANAGED_TURN_TIMEOUT_SECONDS = 3600.0
