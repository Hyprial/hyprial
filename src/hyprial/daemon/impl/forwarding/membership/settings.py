"""Private tailnet settings and the single transitional upper-layer policy."""
from __future__ import annotations

import json
import os
from pathlib import Path

from hyprial.kernel import atomic_json_write
from ..policy import ForwardingSidecarError


def durable_json_write(path: Path, value: dict) -> None:
    """Persist the file and its rename before a remote destructive operation."""
    atomic_json_write(path, value)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _settings_record(home: Path) -> dict:
    try:
        record = json.loads((Path(home) / 'settings.json').read_text())
    except FileNotFoundError:
        record = {}
    except (OSError, ValueError) as error:
        raise ForwardingSidecarError('Invalid tailnet settings.', code='BAD_REQUEST') from error
    if not isinstance(record, dict) or not isinstance(record.get('forwarding', {}), dict):
        raise ForwardingSidecarError('Invalid tailnet settings.', code='BAD_REQUEST')
    return record


def tailnet_settings(home: Path) -> dict:
    section = _settings_record(home).get('forwarding', {}).get('tailnet', {})
    if not isinstance(section, dict):
        raise ForwardingSidecarError('Invalid tailnet settings.', code='BAD_REQUEST')
    store = section.get('membershipStore', str(Path(home) / 'tailnet' / 'membership.json'))
    port = section.get('controlPort', 7448)
    trust = section.get('trustMembersForUpperLayers', True)
    pins = section.get('authorities', {})
    # Opt-in only: with several owner rows and no pin, "self" lets the local
    # device claim authority. Off by default so an upgrade never elects one.
    default = section.get('authorityDefault')
    if (not isinstance(store, str) or not Path(store).is_absolute()
            or type(port) is not int or not 1 <= port <= 65535
            or type(trust) is not bool or not isinstance(pins, dict)
            or default not in (None, 'self')):
        raise ForwardingSidecarError('Invalid tailnet settings.', code='BAD_REQUEST')
    return {'membershipStore': store, 'controlPort': port,
            'trustMembersForUpperLayers': trust, 'authorities': pins,
            'authorityDefault': default}


def pin_authority(home: Path, tailnet_id: str, authority: dict) -> None:
    tailnet_settings(home)  # Validate the nested shape before updating it.
    path = Path(home) / 'settings.json'
    record = _settings_record(home)
    pins = record.setdefault('forwarding', {}).setdefault('tailnet', {}).setdefault('authorities', {})
    if tailnet_id in pins and pins[tailnet_id] != authority:
        raise ForwardingSidecarError('Explicit re-pin required.', code='AUTHORITY_PIN_MISMATCH')
    pins[tailnet_id] = authority
    durable_json_write(path, record)


def private_inbound(home: Path) -> str:
    directory = Path(home) / 'tailnet'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    from ..policy import validate_unix_exposure_target
    return validate_unix_exposure_target('unix:' + str(directory / 'zenoh.sock'))


def trust_upper_layers(app) -> bool:
    # Sidecar presence alone is insufficient: no public TCP listener may share
    # the session while the transitional directory gates are bypassed.
    listeners = getattr(app, 'zenoh_listen', ())
    return (bool(getattr(app, '_forwarding_environment', {})) and bool(listeners)
            and all(endpoint.startswith('unixsock-stream/') for endpoint in listeners)
            and tailnet_settings(app.hyprial_home)['trustMembersForUpperLayers'])
