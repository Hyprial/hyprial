"""Durable, confirmed OrgFS → Zenoh marker → tailnet revoke orchestration."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from hyprial.kernel import lock_exclusive, unlock
from .settings import durable_json_write as atomic_json_write
from ..policy import ForwardingSidecarError


def _identity_digest(row):
    values = [row.get(key) for key in ('owner', 'masterPublic', 'serverPublic')]
    if not all(isinstance(value, str) and value for value in values):
        return None
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


def _key_digests(row):
    return {hashlib.sha256(value.encode()).hexdigest()
            for key in ('masterPublic', 'serverPublic')
            if isinstance(value := row.get(key), str) and value}


class RemovalJournal:
    def __init__(self, home: Path, rpc, *, local_device: str, operator: str | None = None):
        self.root = Path(home) / 'tailnet' / 'removals'
        self.rpc = rpc
        self.local_device = local_device
        self.operator = operator or local_device

    def remove(self, tailnet_id: str, owner: str, *, confirmed: bool, upper,
               only_device: str | None = None, reason: str = 'operator-removal',
               operation_id: str | None = None, device_ids: list[str] | None = None) -> dict:
        if confirmed and (not isinstance(operation_id, str) or not operation_id
                          or not isinstance(device_ids, list)
                          or any(not isinstance(device, str) for device in device_ids)):
            self._stale_confirmation()
        owner = owner.removeprefix('user:')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(json.dumps([tailnet_id, owner, only_device]).encode()).hexdigest()
        path = self.root / (key + '.json')
        descriptor = os.open(self.root / (key + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            lock_exclusive(descriptor)
            return self._remove(path, tailnet_id, owner, confirmed, upper, only_device, reason,
                                operation_id, device_ids)
        finally:
            unlock(descriptor)
            os.close(descriptor)

    def _refresh_revision(self, operation):
        # Only an explicit REVISION_CONFLICT response proves the exact request
        # has no committed receipt (Go replays receipts before revision checks).
        if operation.get('revisionConflictProof') != 'authority-rejected-before-commit':
            raise ForwardingSidecarError('No proof of an uncommitted revoke.', code='OPERATION_CONFLICT')
        if operation['role'] != 'authority':
            self.rpc('roster-fetch', {'tailnetId': operation['tailnetId']})
        status = self.rpc('membership-status', {'tailnetId': operation['tailnetId']})
        table = next((row for row in status['tailnets'] if row['tailnetId'] == operation['tailnetId']), None)
        if table is None:
            raise ForwardingSidecarError('Tailnet not initialized.', code='MEMBERSHIP_NOT_INITIALIZED')
        if table['authorityDeviceId'] != operation['authorityDeviceId']:
            raise ForwardingSidecarError('Authority changed; resume on the pinned authority.', code='AUTHORITY_REQUIRED')
        targets = set(operation['deviceIds'])
        identities = operation.get('targetIdentityDigests', {})
        if any(not identities.get(device) for device in targets):
            raise ForwardingSidecarError('Original target identities cannot be proved.', code='OPERATION_CONFLICT')
        rows = {row['deviceId']: row for row in table['members']}
        previous_keys = set(operation.get('targetKeyDigests', []))
        if targets and not previous_keys:
            raise ForwardingSidecarError('Original target keys cannot be proved.', code='OPERATION_CONFLICT')
        for device, row in rows.items():
            digest = _identity_digest(row)
            if ((device in targets and digest != identities[device]) or
                    (device not in targets and row['state'] == 'active' and
                     (digest in identities.values() or _key_digests(row) & previous_keys))):
                raise ForwardingSidecarError('A replacement incarnation requires a separate operation.', code='MEMBER_CONFLICT')
        remaining = sorted(device for device in targets if device in rows and rows[device]['state'] == 'active')
        return {'oldRevision': operation['expectedRevision'], 'newRevision': table['revision'],
                'remainingDeviceIds': remaining, 'alreadyRemovedDeviceIds': sorted(targets - set(remaining)),
                'unselectedDeviceIds': sorted(device for device, row in rows.items()
                    if device not in targets and row['state'] == 'active' and row['owner'] == operation['owner'])}

    @staticmethod
    def _stale_confirmation():
        raise ForwardingSidecarError(
            'Removal confirmation is stale or incomplete; re-run preview and confirm its exact targets.',
            code='REMOVAL_CONFIRMATION_STALE')

    def _table(self, tailnet_id):
        status = self.rpc('membership-status', {'tailnetId': tailnet_id})
        table = next((row for row in status['tailnets'] if row['tailnetId'] == tailnet_id), None)
        if table is None:
            raise ForwardingSidecarError('Tailnet not initialized.', code='MEMBERSHIP_NOT_INITIALIZED')
        return table

    @staticmethod
    def _targets(table, owner, only_device):
        return sorted(row['deviceId'] for row in table['members']
                      if row['owner'] == owner and row['state'] == 'active'
                      and (only_device is None or row['deviceId'] == only_device))

    def _same_targets(self, operation, table, only_device):
        return (operation['authorityDeviceId'] == table['authorityDeviceId']
                and operation['deviceIds'] == self._targets(table, operation['owner'], only_device)
                and operation['targetIdentityDigests'] == {
                    row['deviceId']: _identity_digest(row) for row in table['members']
                    if row['deviceId'] in operation['deviceIds']})

    def _remove(self, path, tailnet_id, owner, confirmed, upper, only_device, reason,
                operation_id, device_ids):
        try:
            operation = json.loads(path.read_text())
        except FileNotFoundError:
            operation = None
        if confirmed:
            if (operation is None or operation['operationId'] != operation_id
                    or operation['deviceIds'] != device_ids):
                self._stale_confirmation()
            if operation['phase'] == 'complete':
                # Refuse stale confirmation before service-level completion
                # effects (for example recording self-leave) can run again.
                self._stale_confirmation()
        if operation and operation['phase'] in ('pending-confirmation', 'pending-authority', 'orgfs-pending'):
            if not self._same_targets(operation, self._table(tailnet_id), only_device):
                if confirmed or operation['phase'] == 'orgfs-pending':
                    self._stale_confirmation()
                operation = None  # A new preview is safe only before upper-layer work.
        if operation is None or operation['phase'] == 'complete':
            tailnet = self._table(tailnet_id)
            targets = self._targets(tailnet, owner, only_device)
            if operation is not None and not targets:
                return operation
            if tailnet['authorityDeviceId'] in targets:
                raise ForwardingSidecarError('Authority cannot be removed; use a transfer maintenance procedure.', code='LAST_AUTHORITY_REFUSED')
            if self.local_device in targets and only_device != self.local_device:
                raise ForwardingSidecarError('Local device requires an explicit self-leave.', code='SELF_REVOKE_REFUSED')
            operation = {'operationId': uuid.uuid4().hex, 'tailnetId': tailnet_id,
                         'owner': owner, 'deviceIds': targets, 'expectedRevision': tailnet['revision'],
                         'authorityDeviceId': tailnet['authorityDeviceId'], 'role': tailnet['role'],
                         'phase': 'pending-confirmation', 'reason': reason, 'confirmed': False,
                         'targetIdentityDigests': {row['deviceId']: _identity_digest(row)
                             for row in tailnet['members'] if row['deviceId'] in targets},
                         'targetKeyDigests': sorted({digest for row in tailnet['members']
                             if row['deviceId'] in targets for digest in _key_digests(row)})}
            atomic_json_write(path, operation)
        if operation['phase'] == 'revision-conflict':
            operation['revisionRefresh'] = self._refresh_revision(operation)
            operation['phase'] = 'revision-confirmation'
            atomic_json_write(path, operation)
            # Even confirmed=True must first return this new preview. No
            # automatic retry or target expansion follows a conflict.
            return operation
        if operation['phase'] == 'revision-confirmation':
            if not confirmed:
                return operation
            refresh = operation['revisionRefresh']
            operation.setdefault('revisionAudit', []).append(refresh | {
                'confirmedAtMs': int(time.time() * 1000), 'operator': self.operator,
                'operatorDeviceId': self.local_device})
            operation['expectedRevision'] = refresh['newRevision']
            operation['pendingDeviceIds'] = refresh['remainingDeviceIds']
            operation['phase'] = 'tailnet-pending'
            operation['confirmed'] = True
            operation.pop('revisionConflictProof', None)
            atomic_json_write(path, operation)
        if not confirmed and not operation['confirmed']:
            if operation['role'] != 'authority' and operation['deviceIds'] != [self.local_device]:
                operation['phase'] = 'pending-authority'
                atomic_json_write(path, operation)
            return operation
        operation['confirmed'] = True
        if operation['role'] != 'authority' and operation['deviceIds'] != [self.local_device]:
            operation['phase'] = 'pending-authority'
            atomic_json_write(path, operation)
            return operation
        if operation['phase'] in ('pending-confirmation', 'pending-authority'):
            # Persist confirmation before doing any destructive upper-layer work.
            operation['phase'] = 'orgfs-pending'
            atomic_json_write(path, operation)
        if operation['phase'] == 'orgfs-pending':
            upper()  # retryable idempotent mutation; failure prevents revoke
            operation['upperLayerReceipt'] = 'committed'
            operation['zenohDeny'] = 'not-implemented-0.5.5.1'
            operation['phase'] = 'tailnet-pending'
            atomic_json_write(path, operation)
        pending_targets = operation.get('pendingDeviceIds', operation['deviceIds'])
        if operation['phase'] == 'tailnet-pending' and not pending_targets:
            # No tailnet mutation exists to back up or revoke. The Go command
            # deliberately rejects empty deviceIds; do not forge its receipt.
            operation.update(phase='complete', tailnetUnchanged=True)
            atomic_json_write(path, operation)
        if operation['phase'] == 'tailnet-pending':
            params = {key: operation[key] for key in (
                'operationId', 'tailnetId', 'deviceIds', 'expectedRevision', 'reason')}
            params['confirmed'] = True
            params['deviceIds'] = pending_targets
            try:
                result = self.rpc('member-revoke', params)
            except ForwardingSidecarError as error:
                if error.code == 'REVISION_CONFLICT':
                    operation.update(phase='revision-conflict', confirmed=False,
                                     revisionConflictProof='authority-rejected-before-commit')
                    atomic_json_write(path, operation)
                raise
            operation['result'] = result
            operation['phase'] = 'complete'
            atomic_json_write(path, operation)
        return operation
