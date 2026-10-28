"""One-time OFFLINE directory import. Never a runtime admission fallback."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from hyprial.identity import (
    DirectoryDevice, address_path, directory_owner_principal, org_from_space_name,
    protected_directory_doc_id, read_device_record,
)

from ..policy import ForwardingSidecarError
from .settings import durable_json_write as atomic_json_write, pin_authority, tailnet_settings


def start_before_transport(app: Any, *, listen: tuple[str, ...], connect: tuple[str, ...]) -> None:
    if connect:
        raise ValueError('Managed networking accepts sidecar slots only, not explicit Zenoh connect endpoints.')
    if any(not endpoint.startswith('unixsock-stream/') for endpoint in listen):
        raise ValueError('sidecar-managed networking requires private Unix listeners')
    app.zenoh_listen = listen
    app._start_forwarding_supervisor()
    migrate_for_app(app)


def parse_legacy_member(value: dict, *, check_address=None) -> dict:
    # The compatibility parser is deliberately local to this one-time import.
    raw = dict(value)
    raw['clientPublicByServer'] = {}
    device = DirectoryDevice.from_record(raw)
    if check_address is not None and not check_address(device.address, device.server_public):
        raise ValueError('address/server mismatch')
    # Go verifies the embedded CBOR server key as part of the atomic init.
    return {'owner': device.owner.removeprefix('user:'), 'deviceId': device.device_id,
            'masterPublic': device.client_public, 'serverPublic': device.server_public,
            'clientPublicByServer': {}, 'address': device.address,
            'state': 'active', 'revision': 1}


def select_authority(members: list[dict], *, owner: str, pin: dict | None) -> dict:
    selected = [row for row in members if row['owner'] == owner.removeprefix('user:')]
    if pin is not None:
        selected = [row for row in selected if all(row.get(key) == pin.get(key)
                    for key in ('owner', 'deviceId', 'masterPublic', 'serverPublic', 'address'))]
    if len(selected) != 1 or not selected[0].get('address'):
        raise ForwardingSidecarError('Supply an unambiguous owner authority pin/address.', code='MIGRATION_AUTHORITY_MISSING')
    return {key: selected[0][key] for key in ('owner', 'deviceId', 'masterPublic', 'serverPublic', 'address')} | {
        'controlPort': pin.get('controlPort', 7448) if pin else 7448}


def receipt_path(home: Path, tailnet_id: str) -> Path:
    digest = hashlib.sha256(tailnet_id.encode()).hexdigest()
    return Path(home) / 'tailnet' / 'migration' / (digest + '.json')


def initialize_once(home: Path, params: dict, rpc, *, initialized: bool, diagnostics=None) -> None:
    path = receipt_path(home, params['tailnetId'])
    try:
        receipt = json.loads(path.read_text())
    except FileNotFoundError:
        receipt = None
    if receipt and receipt['state'] == 'complete':
        if not initialized:
            raise ForwardingSidecarError('Membership store is missing after completed migration; restore its backup.', code='MEMBERSHIP_NOT_INITIALIZED')
        return
    if receipt is None:
        receipt = {'state': 'pending', 'params': params, 'diagnostics': diagnostics or {}}
        atomic_json_write(path, receipt)
    # Lost replies replay the exact original normalized request, not new rows.
    pin_authority(home, params['tailnetId'], receipt['params']['authority'])
    if 'redemption' in receipt:
        rpc('invite-redeem', receipt['redemption'])
    else:
        rpc('membership-init', receipt['params'])
    receipt['state'] = 'complete'
    atomic_json_write(path, receipt)


def redeem_once(home: Path, invite: dict, rpc) -> None:
    tailnet_id = invite['tailnetId']
    params = {'tailnetId': tailnet_id, 'authority': invite['authority']}
    # Filtered status refuses unknown tailnets; initial redemption enumerates.
    status = rpc('membership-status', {})
    initialized = any(row['tailnetId'] == tailnet_id for row in status['tailnets'])
    path = receipt_path(home, tailnet_id)
    try:
        previous = json.loads(path.read_text())
    except FileNotFoundError:
        previous = None
    new_invite = (previous is None or (previous['state'] == 'complete'
        and previous.get('redemption', {}).get('invite', {}).get('inviteId') != invite['inviteId']))
    if previous is not None and previous['state'] == 'complete' and not initialized:
        raise ForwardingSidecarError('Restore the missing membership store before rejoining.', code='MEMBERSHIP_NOT_INITIALIZED')
    if new_invite:
        pin_authority(home, tailnet_id, invite['authority'])
        atomic_json_write(path, {'state': 'pending', 'params': params,
            'redemption': {'operationId': 'redeem-' + invite['inviteId'], 'invite': invite},
            'diagnostics': {'source': 'invite-v2'}})
    elif previous['params']['authority'] != invite['authority']:
        raise ForwardingSidecarError('Explicit re-pin required.', code='AUTHORITY_PIN_MISMATCH')
    initialize_once(home, params, rpc, initialized=initialized)


def offline_rows(fs, space) -> tuple[list[dict], dict]:
    accepted, rejected, sources = [], [], []
    current = {member.user.removeprefix('user:') for member in fs.members(space.space_id)}
    try:
        owners = fs.listdir(space.space_id, 'directory/devices')
    except Exception as error:
        if getattr(error, 'code', None) == 'unknown-doc':
            owners = []
        else:
            raise
    for owner in owners:
        if owner.kind != 'dir':
            continue
        try:
            principal = directory_owner_principal(owner.name).removeprefix('user:')
            entries = fs.listdir(space.space_id, f'id:{owner.node_id}')
        except ValueError:
            rejected.append({'owner': owner.name, 'code': 'INVALID_OWNER'})
            continue
        for entry in entries:
            if entry.kind != 'doc' or not entry.name.endswith('.json'):
                continue
            path = f'directory/devices/{owner.name}/{entry.name}'
            try:
                if entry.doc_id != protected_directory_doc_id(space.space_id, path, space_owner=space.owner):
                    raise ValueError('document identity mismatch')
                text, _ = fs.read_text(space.space_id, path)
                sources.append((path, text))
                row = parse_legacy_member(json.loads(text))
                if row['owner'] != principal or row['deviceId'] != entry.name[:-5] or principal not in current:
                    raise ValueError('owner/device mismatch or removed member')
                accepted.append(row)
            except Exception as error:
                rejected.append({'path': path, 'code': getattr(error, 'code', 'INVALID_ROW')})
    digest = hashlib.sha256(json.dumps(sorted(sources), separators=(',', ':')).encode()).hexdigest()
    return accepted, {'sourceDigest': digest, 'accepted': [row['deviceId'] for row in accepted], 'rejected': rejected}


def migrate_for_app(app: Any) -> None:
    supervisor = getattr(app, '_forwarding_supervisor', None)
    if supervisor is None:
        return
    rpc = supervisor.membership_request
    statuses = {item['tailnetId']: item for item in rpc('membership-status', {})['tailnets']}
    settings = tailnet_settings(app.hyprial_home)
    record = read_device_record(app.hyprial_home)
    if record is None:
        return
    # Resume receipts before opening even the local directory facade.
    receipt_root = Path(app.hyprial_home) / 'tailnet' / 'migration'
    for path in receipt_root.glob('*.json'):
        receipt = json.loads(path.read_text())
        params = receipt['params']
        initialize_once(app.hyprial_home, params, rpc, initialized=params['tailnetId'] in statuses)
    fs = app._require_orgfs_runtime().facade
    for space in fs.spaces():
        if org_from_space_name(space.name) is None:
            continue
        tailnet_id = space.space_id
        if receipt_path(app.hyprial_home, tailnet_id).exists():
            continue
        if tailnet_id in statuses:
            # Existing sidecar table is authoritative; no directory reread.
            continue
        members, diagnostics = offline_rows(fs, space)
        authority = select_authority(members, owner=space.owner,
                                     pin=settings['authorities'].get(tailnet_id))
        params = {'operationId': 'migration-' + hashlib.sha256(tailnet_id.encode()).hexdigest(),
                  'tailnetId': tailnet_id, 'authority': authority,
                  'migration': {'source': 'directory-0.5.5', 'sourceDigest': diagnostics['sourceDigest'], 'completed': True},
                  'members': members if record.device_id == authority['deviceId'] else []}
        initialize_once(app.hyprial_home, params, rpc, initialized=False, diagnostics=diagnostics)
        rpc('roster-fetch', {'tailnetId': tailnet_id})
        rpc('member-announce', {'operationId': 'announce-' + uuid.uuid4().hex,
                              'tailnetId': tailnet_id,
                              'address': address_path(app.hyprial_home).read_text().strip(),
                              'clientPublicByServer': {}})
