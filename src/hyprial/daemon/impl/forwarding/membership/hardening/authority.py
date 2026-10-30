"""Authority selection for the one-time import is a tailcat-layer local fact.

Ruling (Allen, 2026-10-28): tailcat knows only tailnet devices; OrgFS rows may
be imported as data or shown as candidates, never used to decide.  So the
order is: the settings pin, else the single owner row, else the local device
when the operator opted in with ``forwarding.tailnet.authorityDefault="self"``,
else refuse with the candidate deviceIds and the exact remediation command.
"""
from __future__ import annotations

from ...policy import ForwardingSidecarError

AUTHORITY_KEYS = ('owner', 'deviceId', 'masterPublic', 'serverPublic', 'address')
DEFAULT_CONTROL_PORT = 7448


def remediation(tailnet_id: str, device_id: str = '<deviceId>') -> str:
    return f'hyprial org pin-authority {tailnet_id} --device {device_id} --yes'


def missing(tailnet_id: str, candidates: list[str], detail: str) -> ForwardingSidecarError:
    # Candidates are deviceIds only: no address or key material in diagnostics.
    listed = ', '.join(sorted(candidates)) or '(none)'
    return ForwardingSidecarError(
        f'{detail} Candidate owner devices: {listed}. Pin one with: {remediation(tailnet_id)}',
        code='MIGRATION_AUTHORITY_MISSING')


def pinned_authority(pin: dict) -> dict:
    if not isinstance(pin, dict) or any(not isinstance(pin.get(key), str) or not pin[key] for key in AUTHORITY_KEYS):
        raise ForwardingSidecarError('Invalid tailnet authority pin.', code='BAD_REQUEST')
    port = pin.get('controlPort', DEFAULT_CONTROL_PORT)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ForwardingSidecarError('Invalid tailnet authority pin.', code='BAD_REQUEST')
    return {key: pin[key] for key in AUTHORITY_KEYS} | {'controlPort': port}


def select_authority(members: list[dict], *, owner: str, pin: dict | None, tailnet_id: str = '',
                     local_device_id: str | None = None, authority_default: str | None = None) -> dict:
    if pin is not None:
        return pinned_authority(pin)
    bare_owner = owner.removeprefix('user:')
    rows = [row for row in members if row.get('owner') == bare_owner and row.get('address')]
    candidates = [row['deviceId'] for row in rows]
    local = next((row for row in rows if row['deviceId'] == local_device_id), None)
    if local is not None and (len(rows) == 1 or authority_default == 'self'):
        # The only local fact without a pin: this device is the owner's sole
        # device, or the operator opted this device in. A remote row, however
        # unambiguous it looks from here, is directory data, not a decision.
        selected = local
    elif not rows:
        raise missing(tailnet_id, candidates, 'No owner device row with an address was found.')
    elif local is None:
        raise missing(tailnet_id, candidates,
                      'Authority is a local fact and no pin is set; this member node imports a pin, it does not elect one.')
    else:
        raise missing(tailnet_id, candidates,
                      'Several owner devices and no authority pin; this node will not elect one.')
    return {key: selected[key] for key in AUTHORITY_KEYS} | {'controlPort': DEFAULT_CONTROL_PORT}
