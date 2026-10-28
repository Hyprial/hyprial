"""Explicit offline tailnet maintenance; never starts or stops the daemon."""
import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import confirm, notice


def org_repin(
    tailnet_id: str = typer.Argument(..., help='Tailnet ID (org spaceId), not its display name.'),
    authority: str = typer.Option(..., '--authority', help='Active new authority deviceId.'),
    control_port: int = typer.Option(7448, '--control-port', min=1, max=65535, help='New authority control port.'),
    yes: bool = typer.Option(False, '--yes', help='Confirm offline authority re-pin.'),
    json_output: bool = typer.Option(False, '--json', help='Emit JSON only.'),
) -> None:
    """Re-pin an existing full membership table while the daemon is stopped.

    Stop business and writes on all nodes first. Offline does not mean zero
    network: the sidecar still opens control/peer listeners and refreshes.
    The local daemon fence is not a network-wide write lock.
    This command does not transfer tables or receipts between machines. Preserve the full authority table and receipts
    before transfer; re-pin each node, then restart the new authority first.
    """
    from hyprial.daemon import TailnetMaintenance

    services = get_services()
    def operation():
        services.require_initialized_hyprial_home()
        try:
            with TailnetMaintenance(services._hyprial_home(), services._state_dir()) as maintenance:
                preview = maintenance.prepare(tailnet_id, authority, control_port=control_port)
                notice(f"Re-pin tailnet {tailnet_id} from {preview['previousAuthorityDeviceId']} "
                       f"to {authority}; affected devices {preview['deviceIds']}. "
                       'Existing leases will be cleared. All-node business/writes must stay stopped; '
                       'control networking remains active, and the local fence is not a global lock. '
                       'This command does not transfer the authority table or its receipts.')
                if not yes and (not services._stdin_isatty() or not confirm('Confirm offline re-pin?')):
                    raise services.CliError('CONFIRMATION_REQUIRED', 'Re-run with --yes after reviewing the affected devices.')
                result = maintenance.commit(confirmed=True)
                notice('Re-pin complete; backup retained. Restart the new authority first, then the other nodes.')
                return {'ok': True, **result}
        except services.CliError:
            raise
        except Exception as error:
            code = getattr(error, 'code', 'TAILNET_MAINTENANCE_FAILED')
            message = ('Membership changed; run the command again to review and confirm a new plan.'
                       if code == 'REVISION_CONFLICT' else
                       'Offline re-pin failed. Keep networking stopped and retry the same operation.')
            raise services.CliError(code, message) from error
    services._execute(operation, json_output=json_output)
