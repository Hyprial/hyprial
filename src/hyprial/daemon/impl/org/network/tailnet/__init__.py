"""Org lifecycle operations over the sidecar's explicit membership API."""
from __future__ import annotations

import uuid

from hyprial.daemon.impl.forwarding.membership.migration import initialize_once
from hyprial.daemon.impl.forwarding.membership.projection import candidates
from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
from hyprial.daemon.impl.forwarding.membership.settings import tailnet_settings
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError


class TailnetLifecycle:
    def _tailnet_raw_call(self, op, params):
        if self._membership_request is None:
            raise ForwardingSidecarError('Tailnet sidecar unavailable.', code='MEMBERSHIP_NOT_INITIALIZED')
        return self._membership_request(op, params)

    def _tailnet_call(self, op, params):
        from ..service import OrgNetworkError
        try:
            return self._tailnet_raw_call(op, params)
        except ForwardingSidecarError as error:
            raise OrgNetworkError(error.code, str(error), error.details) from error

    def _ensure_new_tailnet(self, space):
        from ..service import OrgNetworkError
        try:
            self._initialize_new_tailnet(space)
        except ForwardingSidecarError as error:
            raise OrgNetworkError(error.code, str(error), error.details) from error

    def _initialize_new_tailnet(self, space):
        # An unknown filtered tailnet is MEMBERSHIP_NOT_INITIALIZED in v6.
        # Enumerate the store to distinguish first creation from a failed store.
        status = self._tailnet_call('membership-status', {})
        if any(item['tailnetId'] == space.space_id for item in status['tailnets']):
            return
        record = self._require_device_record()
        address = self._own_address()
        if not address:
            raise ForwardingSidecarError('Local address not ready.', code='AUTHORITY_UNREACHABLE')
        roster = self._owner_cell_roster(space.name.removeprefix('group-'))
        members = self._seed_members(roster, record, address)
        authority_member = next(
            member for member in members if member['deviceId'] == record.device_id
        )
        authority = {
            key: authority_member[key]
            for key in ('owner', 'deviceId', 'masterPublic', 'serverPublic', 'address')
        } | {'controlPort': tailnet_settings(self._home)['controlPort']}
        params = {'operationId': 'create-' + space.space_id, 'tailnetId': space.space_id,
                  'authority': authority, 'members': members,
                  'migration': {'source': 'new', 'sourceDigest': '', 'completed': True}}
        initialize_once(self._home, params, self._tailnet_raw_call, initialized=False)
        self._announce_tailnet(space.space_id)

    def _owner_cell_roster(self, org):
        callback = getattr(self, '_read_owner_cell_roster', None)
        return callback(org) if callback is not None else None

    @staticmethod
    def _seed_members(roster, record, address):
        if not isinstance(roster, dict):
            roster = {}
        members = []
        invalid_devices = []
        for entry in roster.values():
            if not isinstance(entry, dict):
                continue
            owner = str(entry.get('username') or '').removeprefix('user:')
            for device in entry.get('devices') or ():
                if not isinstance(device, dict) or not (device.get('serverPublic') or device.get('publicKey')):
                    continue
                device_id = str(device.get('deviceUid') or '')
                if owner == record.owner and not any(
                    item['deviceId'] == record.device_id for item in members
                ):
                    device_id = record.device_id
                master_public = str(device.get('masterPublic') or '')
                server_public = str(device.get('serverPublic') or device.get('publicKey') or '')
                if owner == record.owner and device_id == record.device_id:
                    master_public = record.client_public
                    server_public = record.server_public
                elif not master_public or not server_public or master_public == server_public:
                    invalid_devices.append(device_id or 'unknown')
                    continue
                members.append({
                    'owner': owner,
                    'deviceId': device_id,
                    'masterPublic': master_public,
                    'serverPublic': server_public,
                    'address': address if device_id == record.device_id else str(device.get('address') or ''),
                    'clientPublicByServer': {}, 'state': 'active', 'revision': 1,
                })
        if invalid_devices:
            raise ForwardingSidecarError(
                'Owner roster device is missing distinct authoritative client/server keys.',
                code='MEMBER_CONFLICT',
                details={'deviceIds': sorted(invalid_devices)},
            )
        if not members:
            members = [{
                'owner': record.owner, 'deviceId': record.device_id,
                'masterPublic': record.client_public, 'serverPublic': record.server_public,
                'address': address, 'clientPublicByServer': {}, 'state': 'active', 'revision': 1,
            }]
        return members

    def _redeem_tailnet(self, parsed):
        from hyprial.daemon.impl.forwarding.membership.migration import redeem_once
        from ..service import OrgNetworkError
        try:
            redeem_once(self._home, parsed.tailnet, self._tailnet_raw_call)
        except ForwardingSidecarError as error:
            raise OrgNetworkError(error.code, str(error), error.details) from error

    def _announce_tailnet(self, tailnet_id):
        return self._tailnet_call('member-announce', {
            'operationId': uuid.uuid4().hex, 'tailnetId': tailnet_id,
            'address': self._own_address() or '', 'clientPublicByServer': {}})

    def _tailnet_candidates(self, tailnet_id=None):
        record = self._require_device_record()
        status = self._tailnet_call('membership-status', {'tailnetId': tailnet_id} if tailnet_id else {})
        return candidates(status, master_public=record.client_public, device_id=record.device_id)

    def _removal(self, org, user, *, confirmed=False, self_leave=False, casdoor=False,
                 operation_id=None, device_ids=None, process_leave=False):
        from ..create.sub_org import effective_tailnet_id, sub_org_binding, sub_org_removal
        space = self._require_space(org)
        parent_org, _ = sub_org_binding(self._fs, self._home, org, space_id=space.space_id)
        tailnet_id = effective_tailnet_id(self._fs, self._home, org, space_id=space.space_id)
        journal = RemovalJournal(self._home, self._tailnet_raw_call,
                                 local_device=self._node_id, operator=self._owner)
        def upper():
            if self_leave:
                self._store.remove_device(org, self._node_id, owner=self._owner)
                self._store.put_leave_request(org, self._owner, {
                    'org': org, 'user': self._owner, 'deviceId': self._node_id,
                    'requestedAt': self._now().isoformat()})
            else:
                if not casdoor:
                    self._casdoor_remove(org, user)
                self._remove_orgfs_member(org, space.space_id, user)
        if parent_org is not None:
            if not self_leave:
                self.require_roster_write_authority(org, space.space_id)
            if process_leave:
                upper()
                return {'operationId': uuid.uuid4().hex, 'tailnetId': tailnet_id,
                        'owner': user.removeprefix('user:'), 'deviceIds': [],
                        'phase': 'complete', 'confirmed': True, 'tailnetUnchanged': True}
            return sub_org_removal(home=self._home, org=org, space_id=space.space_id,
                                   tailnet_id=tailnet_id, user=user, confirmed=confirmed,
                                   upper=upper, self_leave=self_leave, operation_id=operation_id,
                                   device_ids=device_ids)
        try:
            return journal.remove(space.space_id, user, confirmed=confirmed, upper=upper,
                                  only_device=self._node_id if self_leave else None,
                                  reason='casdoor-removal' if casdoor else 'operator-removal',
                                  operation_id=operation_id, device_ids=device_ids)
        except ForwardingSidecarError as error:
            from ..service import OrgNetworkError
            raise OrgNetworkError(error.code, str(error), error.details) from error

    def remove_from_casdoor(self, org, user):
        # Discovery is NOT operator confirmation, including previously-known users.
        return self._removal(org, user, confirmed=False, casdoor=True)
