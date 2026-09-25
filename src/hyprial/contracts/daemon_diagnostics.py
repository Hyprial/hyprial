"""Bounded names shared by daemon startup and its launch diagnostics."""

from __future__ import annotations

from enum import StrEnum


class DaemonStartupPhase(StrEnum):
    """Every phase emitted by ``daemon.start.begin``.

    The daemon accepts this enum at the emission seam and the CLI derives its
    diagnostic allowlist from the same enum.  A phase therefore cannot be
    emitted without also being admitted by the launch-time reader.
    """

    ACTOR_RUNTIME = "actor-runtime"
    IPC_SERVER = "ipc-server"
    PID_FILE = "pid-file"
    USAGE_CACHE = "usage-cache"
    AUTOUPDATE = "autoupdate"
    RESTORE_ADAPTERS = "restore-adapters"
    RESTORE_HARNESSES = "restore-harnesses"
    RESTORE_PROVIDER_AUTH = "restore-provider-auth"


DAEMON_STARTUP_PHASES = frozenset(phase.value for phase in DaemonStartupPhase)

