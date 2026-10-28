"""Explicit operator confirmation for persistent tailnet removal operations."""
from hyprial.shell.impl.cli.output import confirm, notice


def confirmed_removal(services, method, params, *, yes: bool):
    preview = services._daemon_request(method, {**params, 'confirmed': False})
    if (not isinstance(preview, dict) or not isinstance(preview.get('operationId'), str)
            or not preview['operationId'] or not isinstance(preview.get('deviceIds'), list)
            or any(not isinstance(device, str) for device in preview['deviceIds'])):
        raise services.CliError('INVALID_RESPONSE', 'Removal preview must include operationId and deviceIds.')
    if preview.get('phase') == 'complete':
        return preview  # A fresh preview found no new targets; no confirmation is needed.
    binding = {'operationId': preview['operationId'], 'deviceIds': list(preview['deviceIds'])}
    notice(f"Cut off owner {params.get('user', 'self')}; devices {preview.get('deviceIds', [])}; "
           f"tailnet {preview.get('tailnetId')}; authority {preview.get('authorityDeviceId')}. "
           'Affected business connections will close; global propagation is lease-bounded (300 seconds).')
    refresh = preview.get('revisionRefresh')
    if isinstance(refresh, dict):
        notice(f"Revision changed {refresh.get('oldRevision')} → {refresh.get('newRevision')}; "
               f"still targeted {refresh.get('remainingDeviceIds', [])}; "
               f"already removed {refresh.get('alreadyRemovedDeviceIds', [])}; "
               f"new devices NOT selected {refresh.get('unselectedDeviceIds', [])}. "
               'Confirm this revised preview; committed OrgFS changes will not repeat.')
    if not yes:
        if not services._stdin_isatty() or not confirm('Confirm this removal?'):
            raise services.CliError('CONFIRMATION_REQUIRED', 'Re-run with --yes to confirm this exact removal.')
    result = services._daemon_request(method, {**params, **binding, 'confirmed': True})
    if not isinstance(result, dict):
        raise services.CliError('INVALID_RESPONSE', 'Removal must return an object.')
    if result.get('phase') != 'complete':
        code = 'AUTHORITY_REQUIRED' if result.get('phase') == 'pending-authority' else 'AUTHORITY_UNREACHABLE'
        raise services.CliError(code, 'Removal is pending; execute/retry on the pinned authority.')
    return result
