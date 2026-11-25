"""Login's best-effort follow-ups: invite pickup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


def _pickup_org_invites_after_login(
    home: Path, *, json_output: bool, services: Any = None
) -> JsonObject:
    """One best-effort per-invite pickup after a verified login (§5.3, A2)."""
    from hyprial.shell.impl.cli.commands.login.invites import invite_confirmer
    from hyprial.shell.impl.invites import pending as pending_module
    from hyprial.shell.impl.invites.join import invite_request

    active_services = get_services() if services is None else services
    return pending_module.pickup_after_login(
        home,
        request=invite_request(active_services.runtime, json_output=json_output),
        confirm=invite_confirmer(active_services, json_output=json_output),
    )
