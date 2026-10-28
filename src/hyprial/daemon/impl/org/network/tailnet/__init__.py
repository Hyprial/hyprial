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
            raise OrgNetworkError(error.code, str(error)) from error

    def _ensure_new_tailnet(self, space):
        from ..service import OrgNetworkError
        try:
            self._initialize_new_tailnet(space)
        except ForwardingSidecarError as error:
            raise OrgNetworkError(error.code, str(error)) from error

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
        authority = {'owner': record.owner, 'deviceId': record.device_id,
                     'masterPublic': record.client_public, 'serverPublic': record.server_public,
                     'address': address, 'controlPort': tailnet_settings(self._home)['controlPort']}
        member = {key: authority[key] for key in authority if key != 'controlPort'} | {
            'clientPublicByServer': {}, 'state': 'active', 'revision': 1}
        params = {'operationId': 'create-' + space.space_id, 'tailnetId': space.space_id,
                  'authority': authority, 'members': [member],
                  'migration': {'source': 'new', 'sourceDigest': '', 'completed': True}}
        initialize_once(self._home, params, self._tailnet_raw_call, initialized=False)
        self._announce_tailnet(space.space_id)

    def _redeem_tailnet(self, parsed):
        from hyprial.daemon.impl.forwarding.membership.migration import redeem_once
        from ..service import OrgNetworkError
        try:
            redeem_once(self._home, parsed.tailnet, self._tailnet_raw_call)
        except ForwardingSidecarError as error:
            raise OrgNetworkError(error.code, str(error)) from error

    def _announce_tailnet(self, tailnet_id):
        return self._tailnet_call('member-announce', {
            'operationId': uuid.uuid4().hex, 'tailnetId': tailnet_id,
            'address': self._own_address() or '', 'clientPublicByServer': {}})

    def _tailnet_candidates(self, tailnet_id=None):
        record = self._require_device_record()
        status = self._tailnet_call('membership-status', {'tailnetId': tailnet_id} if tailnet_id else {})
        return candidates(status, master_public=record.client_public, device_id=record.device_id)

    def _removal(self, org, user, *, confirmed=False, self_leave=False, casdoor=False,
                 operation_id=None, device_ids=None):
        space = self._require_space(org)
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
        try:
            return journal.remove(space.space_id, user, confirmed=confirmed, upper=upper,
                                  only_device=self._node_id if self_leave else None,
                                  reason='casdoor-removal' if casdoor else 'operator-removal',
                                  operation_id=operation_id, device_ids=device_ids)
        except ForwardingSidecarError as error:
            from ..service import OrgNetworkError
            raise OrgNetworkError(error.code, str(error)) from error

    def remove_from_casdoor(self, org, user):
        # Discovery is NOT operator confirmation, including previously-known users.
        return self._removal(org, user, confirmed=False, casdoor=True)
