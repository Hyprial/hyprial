"""Login's best-effort follow-ups: invite pickup and supervised-stop recovery.

Split from ``flow.py`` (structural line limit).  Neither touches the identity
commit: both run after it (pickup) or after an uncommitted failure (restore).
"""

from __future__ import annotations

from pathlib import Path

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


def _pickup_org_invites_after_login(home: Path) -> JsonObject:
    """One best-effort ``org pending`` run after a verified login (§5.3, D12).

    Any failure — no device record, a refused refresh, an unreachable
    account server — is reported in the result's ``pending`` field as a
    warning; it never changes the login result or its exit code.  The
    join itself is the daemon's (IPC ``org.join``); this only orchestrates.
    """
    services = get_services()
    try:
        from hyprial.shell.impl.invites import pending as pending_module
        from hyprial.shell.impl.invites.join import request_join

        outcome = pending_module.run_pending(
            home,
            accept_all=True,
            confirm=None,
            ipc_join=lambda link: request_join(services.runtime, link),
            ipc_list=lambda: services.runtime.request("org.list"),
            now=None,
        )
    except Exception as error:  # noqa: BLE001 - D12: best effort by ruling
        return {
            "status": "failed",
            "code": str(getattr(error, "code", "INVITE_PICKUP_FAILED")),
            "message": str(error),
        }
    return {
        "status": "ok",
        "joined": outcome.get("joined", []),
        "failed": outcome.get("failed", []),
        "skipped": outcome.get("skipped", 0),
        "warnings": outcome.get("warnings", []),
    }


def _restart_supervised_after_failed_switch() -> JsonObject:
    from hyprial.shell.impl.cli.commands.daemon.service import (
        installed_supervisor,
        request_supervised_start,
    )

    try:
        supervisor = installed_supervisor()
        if supervisor is None:
            return {"status": "not-attempted", "reason": "service-not-installed"}
        request_supervised_start(supervisor)
    except Exception as error:  # noqa: BLE001 - reported, never masks the login error
        return {"status": "failed", "code": getattr(error, "code", type(error).__name__)}
    return {"status": "requested"}
