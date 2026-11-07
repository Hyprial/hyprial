"""v6 replaces join-time/owner/liveliness ordering with the leased roster tuple."""
from itertools import combinations

import pytest

from hyprial.daemon.impl.forwarding.membership.projection import candidates


@pytest.mark.parametrize('keys', [('a', 'b', 'c', 'd'), ('same',) * 4])
def test_each_active_pair_has_exactly_one_dialer(keys):
    members = [{'owner': f'user-{3-index}', 'deviceId': f'device-{index}',
                'masterPublic': key, 'address': f'address-{index}', 'state': 'active'}
               for index, key in enumerate(keys)]
    status = {'tailnets': [{'memberReady': True, 'members': members}]}
    dials = {row['deviceId']: candidates(status, master_public=row['masterPublic'],
                                        device_id=row['deviceId']) for row in members}
    for left, right in combinations(members, 2):
        assert (right['deviceId'] in dials[left['deviceId']]) != (left['deviceId'] in dials[right['deviceId']])
        greater, lesser = sorted((left, right), key=lambda row: (row['masterPublic'], row['deviceId']), reverse=True)
        assert lesser['deviceId'] in dials[greater['deviceId']]


@pytest.mark.parametrize('ready,state', [(False, 'active'), (True, 'tombstone')])
def test_unleased_or_revoked_peer_is_not_a_fallback_candidate(ready, state):
    status = {'tailnets': [{'memberReady': ready, 'members': [
        {'owner': 'owner', 'deviceId': 'peer', 'masterPublic': 'a',
         'address': 'address', 'state': state}]}]}
    assert candidates(status, master_public='z', device_id='local') == {}


def test_join_time_owner_and_presence_do_not_reverse_order():
    status = {'tailnets': [{'memberReady': True, 'members': [
        {'owner': 'first-owner', 'deviceId': 'peer', 'masterPublic': 'z',
         'address': 'address', 'state': 'active', 'added_at': 'old', 'present': False}]}]}
    assert candidates(status, master_public='a', device_id='local') == {}
