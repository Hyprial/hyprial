"""Offline authority pin and migration status for tailnets the import has not migrated yet."""
from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, confirm, notice


def _offline_operation(services, body):
    """Run ``body(tool)`` against the local store; map tool errors to CLI errors."""
    from hyprial.daemon import OfflineMembership

    services.require_initialized_hyprial_home()
    try:
        with OfflineMembership(services._hyprial_home(), services._state_dir()) as tool:
            return body(tool)
    except services.CliError:
        raise
    except Exception as error:
        raise services.CliError(getattr(error, 'code', 'TAILNET_MAINTENANCE_FAILED'), str(error)) from error


def org_pin_authority(
    space_id: str = typer.Argument(..., help='Org space ID (tailnet ID), not its display name.'),
    device: str = typer.Option(..., '--device', help='Owner deviceId to pin as the tailnet authority.'),
    control_port: int = typer.Option(7448, '--control-port', min=1, max=65535, help='Authority control port.'),
    yes: bool = typer.Option(False, '--yes', help='Confirm the pin without an interactive prompt.'),
    json_output: bool = typer.Option(False, '--json', help='Emit JSON only.'),
) -> None:
    """Pin the import authority for an org whose membership table does not exist yet.

    Works with the daemon stopped or running: it reads the local directory rows
    and writes only `forwarding.tailnet.authorities[<spaceId>]` in settings.json.
    Refuses with MEMBERSHIP_ALREADY_INITIALIZED once the sidecar table exists
    (then `hyprial org repin` is the tool). Restart the daemon afterwards; start
    the pinned authority device first.
    """
    services = get_services()

    def operation():
        def body(tool):
            preview = tool.pin_authority(space_id, device, confirmed=False, control_port=control_port)
            notice(f"Pin tailnet {space_id} ({preview['org']}) authority to device {device}; "
                   f"owner devices with rows: {preview['candidates']}. Only settings.json changes; "
                   'restart the daemon afterwards and start the authority device first.')
            if not yes and (not services._stdin_isatty() or not confirm('Confirm authority pin?')):
                raise services.CliError('CONFIRMATION_REQUIRED', 'Re-run with --yes after reviewing the candidate devices.')
            return {'ok': True, **tool.pin_authority(space_id, device, confirmed=True, control_port=control_port)}
        return _offline_operation(services, body)

    services._execute(operation, json_output=json_output)


def _render_status(data: Mapping[str, Any]) -> str:
    lines = ['hyprial org migration-status']
    for row in data.get('spaces', []):
        receipt = row.get('receipt') or {}
        rows = row.get('rows', {})
        error = row.get('lastError') or {}
        lines.append(
            f"- {row.get('org')} ({row.get('spaceId')}): state={row.get('state')} "
            f"skip={row.get('skipReason')} pinned={row.get('pinnedDeviceId') or 'no'} "
            f"receipt={receipt.get('state') or 'none'} sidecar={'yes' if row.get('sidecarInitialized') else 'no'} "
            f"rows accepted={len(rows.get('accepted', []))} rejected={len(rows.get('rejected', []))} "
            f"dropped={len(rows.get('dropped', []))} lastError={error.get('code') or 'none'}")
    if len(lines) == 1:
        lines.append('- no org spaces in the local store')
    return '\n'.join(lines)


def org_migration_status(
    json_output: bool = typer.Option(False, '--json', help='Emit JSON only.'),
) -> None:
    """Per org space: pin, receipt state, accepted/rejected/dropped rows and last sidecar error (offline)."""
    services = get_services()

    def operation():
        return CliResult(_offline_operation(services, lambda tool: {'ok': True, 'spaces': tool.migration_status()}),
                         render=_render_status)

    services._execute(operation, json_output=json_output)
