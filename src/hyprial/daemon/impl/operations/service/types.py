"""Shared value types and errors for user-service management.

Z-8b decisions (owner-confirmed OD-Z7 option a, 2026-11-15, PR #2830):
- ``ServiceConfig`` carries the unit's program argv (``daemon_args``) so the
  module form ``[sys.executable, -m, hyprial.cli, daemon, run]`` and the
  console-script form render, attribute, and scan from one source of truth.
- ``proxy_env`` snapshots HTTP_PROXY/HTTPS_PROXY/NO_PROXY at install time;
  values render unfiltered into the 0644 unit (userinfo exposure is the
  owner-accepted known issue KP-081, registered by integration).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


LAUNCHD_LABEL = "com.hyprial.hyprial.daemon"
DEFAULT_SYSTEMD_UNIT = "hyprial.service"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def default_proxy_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The proxy variables visible at render time, in PROXY_KEYS order."""

    source = os.environ if environ is None else environ
    return {key: source[key] for key in PROXY_KEYS if key in source}

ServicePlatform = Literal["launchd", "systemd"]
SupervisionState = Literal["supervised", "unsupervised", "lost"]


class ServiceOwnershipError(RuntimeError):
    """The configured supervisor file is not owned by this Hyprial install."""

    def __init__(self, message: str, *, reason: str = "service-file-unattributed") -> None:
        super().__init__(message)
        self.reason = reason


class ServiceCommandError(RuntimeError):
    """A supervisor command could not produce a trustworthy result."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class ServiceCommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """Observed supervisor state for one home.

    ``foreign_loaded`` means the label is loaded from another unit file;
    callers must refuse to mutate it.
    """

    platform: ServicePlatform
    unit: str
    unit_path: Path
    installed: bool
    loaded: bool
    running: bool
    pid: int | None = None
    boot_persistent: bool | None = None
    changed: bool | None = None
    attributed: bool | None = None
    template_matches: bool | None = None
    serving_pid: int | None = None
    supervision: SupervisionState = "unsupervised"
    relaunch_count: int | None = None
    last_exit_code: int | None = None
    ownership_busy: bool = False
    ownership_reason: str | None = None
    backup_path: Path | None = None
    foreign_loaded: bool = False


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Paths and environment rendered into this home's service unit.

    ``path_env`` includes ``~/.local/bin`` (user-level installs: uv tools,
    pipx, codex shim) for the same reason the autoupdate timer config does:
    a service environment without it cannot spawn those harness binaries.
    ``daemon_args`` is the argument prefix after the executable: the console
    script runs ``("daemon", "run")`` and the module form runs
    ``("-m", "hyprial.cli", "daemon", "run")``. ``proxy_env`` is the install-time
    snapshot of the proxy variables (see ``default_proxy_environment``).
    """

    executable: Path
    home: Path
    hyprial_home: Path
    state_dir: Path
    path_env: str = field(
        default_factory=lambda: (
            str(Path.home() / ".local" / "bin")
            + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        )
    )
    daemon_args: tuple[str, ...] = ("daemon", "run")
    proxy_env: Mapping[str, str] = field(default_factory=default_proxy_environment)
