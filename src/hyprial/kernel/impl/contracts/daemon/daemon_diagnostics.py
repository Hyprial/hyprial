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
    ACTOR_RUNTIME_AGENTS_STORE = "actor-runtime-agents-store"
    ACTOR_RUNTIME_TRANSPORT = "actor-runtime-transport"
    ACTOR_RUNTIME_ORGFS_TRANSPORT = "actor-runtime-orgfs-transport"
    ACTOR_RUNTIME_IDENTITY_BINDINGS = "actor-runtime-identity-bindings"
    ACTOR_RUNTIME_CASDOOR_SYNC = "actor-runtime-casdoor-sync"
    ACTOR_RUNTIME_FORWARDING_RECONFIGURE = "actor-runtime-forwarding-reconfigure"
    ACTOR_RUNTIME_INBOX_STORE = "actor-runtime-inbox-store"
    ACTOR_RUNTIME_LIFECYCLE_STORE = "actor-runtime-lifecycle-store"
    SERVICE_CONNECT = "service-connect"
    IPC_SERVER = "ipc-server"
    PID_FILE = "pid-file"
    USAGE_CACHE = "usage-cache"
    AUTOUPDATE = "autoupdate"
    RESTORE_ADAPTERS = "restore-adapters"
    RESTORE_HARNESSES = "restore-harnesses"
    RESTORE_PROVIDER_AUTH = "restore-provider-auth"


DAEMON_STARTUP_PHASES = frozenset(phase.value for phase in DaemonStartupPhase)
