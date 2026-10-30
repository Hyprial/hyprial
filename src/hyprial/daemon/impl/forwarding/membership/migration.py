"""One-time OFFLINE directory import. Never a runtime admission fallback."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
import json
import uuid
from pathlib import Path
from typing import Any

from hyprial.identity import (
    DirectoryDevice, address_path, directory_owner_principal, org_from_space_name,
    protected_directory_doc_id, read_device_record,
)

from ..policy import ForwardingSidecarError
from .hardening.authority import select_authority
from .hardening.rows import dedupe_rows, parse_updated_at, validate_row
from .hardening.spaces import SKIP_NO_ROWS, SKIP_SPACE_MISSING, log_skip, space_skip_reason
from .hardening.store import store_tailnets
from .settings import durable_json_write as atomic_json_write, pin_authority, tailnet_settings


def start_before_transport(app: Any, *, listen: tuple[str, ...], connect: tuple[str, ...]) -> None:
    if connect:
        raise ValueError('Managed networking accepts sidecar slots only, not explicit Zenoh connect endpoints.')
    if any(not endpoint.startswith('unixsock-stream/') for endpoint in listen):
        raise ValueError('sidecar-managed networking requires private Unix listeners')
    app.zenoh_listen = listen
    # Decide everything the import needs from local files first. A refusal
    # (MIGRATION_AUTHORITY_MISSING) then costs no sidecar launch, no control
    # handshake and no 30 s membership round-trip before the start fails.
    plans = plan_migrations(app)
    app._start_forwarding_supervisor()
    migrate_for_app(app, plans)


def parse_legacy_member(value: dict, *, check_address=None) -> dict:
    # The compatibility parser is deliberately local to this one-time import.
    raw = dict(value)
    raw['clientPublicByServer'] = {}
    # A corrupted updatedAt (seen in production: "2026-10-197T005:52:31…") is
    # not a reason to lose the device; it only ranks the row as the oldest.
    updated_at = raw.get('updatedAt') if parse_updated_at(raw.get('updatedAt')) is not None else None
    if updated_at is None:
        raw['updatedAt'] = '1970-01-01T00:00:00Z'
    device = DirectoryDevice.from_record(raw)
    if check_address is not None and not check_address(device.address, device.server_public):
        raise ValueError('address/server mismatch')
    # Go verifies the embedded CBOR server key as part of the atomic init.
    return {'owner': device.owner.removeprefix('user:'), 'deviceId': device.device_id,
            'masterPublic': device.client_public, 'serverPublic': device.server_public,
            'clientPublicByServer': {}, 'address': device.address,
            'state': 'active', 'revision': 1, 'updatedAt': updated_at}


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
    if receipt and receipt['state'] == 'skipped':
        return  # plan_migrations rewrites it to pending first when the space becomes a candidate
    if receipt is None:
        receipt = {'state': 'pending', 'params': params, 'diagnostics': diagnostics or {}}
        atomic_json_write(path, receipt)
    # Lost replies replay the exact original normalized request, not new rows.
    pin_authority(home, params['tailnetId'], receipt['params']['authority'])
    try:
        if 'redemption' in receipt:
            rpc('invite-redeem', receipt['redemption'])
        else:
            rpc('membership-init', receipt['params'])
    except ForwardingSidecarError as error:
        # The sidecar's code is the only diagnostic the operator gets; keep it.
        receipt['lastError'] = {'code': error.code, 'at': datetime.now(UTC).isoformat(timespec='seconds')}
        atomic_json_write(path, receipt)
        raise
    receipt.pop('lastError', None)
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
                code = validate_row(row)
                if code is not None:
                    rejected.append({'path': path, 'code': code})
                    continue
                accepted.append(row)
            except Exception as error:
                rejected.append({'path': path, 'code': getattr(error, 'code', 'INVALID_ROW')})
    # The sidecar refuses two active devices sharing any key (MEMBER_CONFLICT);
    # a renamed host leaves exactly that behind. Drop the older row here.
    kept, dropped = dedupe_rows(accepted)
    members = [{key: value for key, value in row.items() if key != 'updatedAt'} for row in kept]
    digest = hashlib.sha256(json.dumps(sorted(sources), separators=(',', ':')).encode()).hexdigest()
    return members, {'sourceDigest': digest, 'accepted': [row['deviceId'] for row in members],
                     'rejected': rejected, 'dropped': dropped,
                     'droppedDeviceIds': [item['deviceId'] for item in dropped]}


def plan_migrations(app: Any) -> list[dict]:
    """Decide every pending import from local files only; no sidecar involved.

    Raises MIGRATION_AUTHORITY_MISSING before any sidecar launch so a refused
    import fails the start phase in seconds, not after the control handshake.
    Only a ``complete`` receipt is authoritative. A ``pending`` (or ``skipped``)
    directory-import receipt is advisory: its request is recomputed from the
    current local files every start (skip rules, authority rule, row hygiene)
    and rewritten, the superseded params kept under ``diagnostics.supersededParams``.
    """
    record = read_device_record(app.hyprial_home)
    if record is None:
        return []
    settings = tailnet_settings(app.hyprial_home)
    initialized = store_tailnets(app.hyprial_home)
    fs = app._require_orgfs_runtime().facade
    plans = []
    advisory = {tailnet_id: receipt for tailnet_id, receipt in read_receipts(app.hyprial_home).items()
                if receipt.get('state') != 'complete' and 'redemption' not in receipt}
    for space in fs.spaces():
        if org_from_space_name(space.name) is None:
            continue
        tailnet_id = space.space_id
        receipt = advisory.pop(tailnet_id, None)
        if tailnet_id in initialized or (receipt is None and receipt_path(app.hyprial_home, tailnet_id).exists()):
            # An existing sidecar table is authoritative (a pending receipt is
            # then replayed against it); a complete receipt resumes on its own.
            continue
        reason = space_skip_reason(fs, space, app.hyprial_home, local_owner=record.owner)
        members, diagnostics = ([], {}) if reason else offline_rows(fs, space)
        if reason is None and not members:
            reason = SKIP_NO_ROWS
        if reason is not None:
            log_skip(app, space, reason)
            if receipt is not None:
                skip_receipt(app.hyprial_home, tailnet_id, receipt, reason)
            continue
        authority = select_authority(members, owner=space.owner, tailnet_id=tailnet_id,
                                     pin=settings['authorities'].get(tailnet_id),
                                     local_device_id=record.device_id,
                                     authority_default=settings['authorityDefault'])
        params = {'operationId': 'migration-' + hashlib.sha256(tailnet_id.encode()).hexdigest(),
                  'tailnetId': tailnet_id, 'authority': authority,
                  'migration': {'source': 'directory-0.5.5', 'sourceDigest': diagnostics['sourceDigest'], 'completed': True},
                  'members': members if record.device_id == authority['deviceId'] else []}
        if receipt is not None:
            recompute_receipt(app.hyprial_home, tailnet_id, receipt, params, diagnostics)
        plans.append({'tailnetId': tailnet_id, 'params': params, 'diagnostics': diagnostics})
    for tailnet_id, receipt in advisory.items():
        # The org space is gone from the local store: nothing left to import.
        if receipt.get('state') != 'skipped':
            skip_receipt(app.hyprial_home, tailnet_id, receipt, SKIP_SPACE_MISSING)
            log = getattr(app, '_log', None)
            if log is not None:
                log('info', 'zenoh', 'membership.migration.skipped', spaceId=tailnet_id, org=None, reason=SKIP_SPACE_MISSING)
    return plans


def read_receipts(home: Path) -> dict[str, dict]:
    receipts = {}
    for path in (Path(home) / 'tailnet' / 'migration').glob('*.json'):
        receipt = json.loads(path.read_text())
        receipts[receipt['params']['tailnetId']] = receipt
    return receipts


def recompute_receipt(home: Path, tailnet_id: str, receipt: dict, params: dict, diagnostics: dict) -> None:
    """Rewrite an advisory receipt to the recomputed request; keep lastError and the old params."""
    if receipt.get('state') == 'pending' and receipt['params'] == params:
        return
    diagnostics = dict(diagnostics)
    if receipt['params'] != params:
        diagnostics['supersededParams'] = {'params': receipt['params'], 'reason': 'recomputed-on-startup',
                                           'previousState': receipt.get('state'),
                                           'at': datetime.now(UTC).isoformat(timespec='seconds')}
    receipt = {key: value for key, value in receipt.items() if key not in ('skipReason', 'skippedAt')}
    receipt.update(state='pending', params=params, diagnostics=diagnostics)
    atomic_json_write(receipt_path(home, tailnet_id), receipt)


def skip_receipt(home: Path, tailnet_id: str, receipt: dict, reason: str) -> None:
    receipt.update(state='skipped', skipReason=reason, skippedAt=datetime.now(UTC).isoformat(timespec='seconds'))
    atomic_json_write(receipt_path(home, tailnet_id), receipt)


def migrate_for_app(app: Any, plans: list[dict] | None = None) -> None:
    supervisor = getattr(app, '_forwarding_supervisor', None)
    if supervisor is None:
        return
    rpc = supervisor.membership_request
    statuses = {item['tailnetId']: item for item in rpc('membership-status', {})['tailnets']}
    record = read_device_record(app.hyprial_home)
    if record is None:
        return
    if plans is None:
        plans = plan_migrations(app)
    # Resume authoritative receipts first: complete ones (store must exist),
    # invite redemptions, and pending imports whose table the sidecar already
    # holds (a lost reply; the sidecar replays by operationId). Pending
    # directory imports without a table were recomputed into `plans`.
    for tailnet_id, receipt in read_receipts(app.hyprial_home).items():
        if receipt.get('state') == 'skipped':
            continue
        if receipt.get('state') == 'complete' or 'redemption' in receipt or tailnet_id in statuses:
            initialize_once(app.hyprial_home, receipt['params'], rpc, initialized=tailnet_id in statuses)
    for plan in plans:
        tailnet_id = plan['tailnetId']
        receipt = read_receipts(app.hyprial_home).get(tailnet_id)
        if tailnet_id in statuses or (receipt is not None and receipt.get('state') == 'complete'):
            continue
        initialize_once(app.hyprial_home, plan['params'], rpc, initialized=False, diagnostics=plan['diagnostics'])
        rpc('roster-fetch', {'tailnetId': tailnet_id})
        rpc('member-announce', {'operationId': 'announce-' + uuid.uuid4().hex,
                              'tailnetId': tailnet_id,
                              'address': address_path(app.hyprial_home).read_text().strip(),
                              'clientPublicByServer': {}})
