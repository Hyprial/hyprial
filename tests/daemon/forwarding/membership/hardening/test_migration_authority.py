"""Item B: authority selection is a local fact (pin, sole owner row, or opt-in self)."""
import json

import pytest

from hyprial.daemon.impl.forwarding.membership.migration import migrate_for_app, receipt_path, select_authority
from hyprial.daemon.impl.forwarding.membership.settings import tailnet_settings
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from tests.daemon.forwarding.membership.hardening.support import LOCAL_DEVICE, OWNER, LegacyHome, legacy_row
from tests.support.tailnet_membership import key, member

SPACE = 'space-1'


def owner_rows():
    return [member(OWNER, LOCAL_DEVICE, address='addr-local'), member(OWNER, 'alice-old', address='addr-old')]


def test_multi_device_owner_without_pin_names_candidates_and_remedy():
    with pytest.raises(ForwardingSidecarError) as caught:
        select_authority(owner_rows(), owner='user:' + OWNER, pin=None, tailnet_id=SPACE,
                         local_device_id=LOCAL_DEVICE, authority_default=None)
    error = caught.value
    assert error.code == 'MIGRATION_AUTHORITY_MISSING'
    assert LOCAL_DEVICE in str(error) and 'alice-old' in str(error)
    assert f'hyprial org pin-authority {SPACE} --device' in str(error)
    assert 'nodekey:' not in str(error) and 'addr-' not in str(error)


def test_authority_default_self_prefers_the_local_device():
    authority = select_authority(owner_rows(), owner=OWNER, pin=None, tailnet_id=SPACE,
                                 local_device_id=LOCAL_DEVICE, authority_default='self')
    assert authority['deviceId'] == LOCAL_DEVICE and authority['controlPort'] == 7448


def test_authority_default_self_does_not_pick_a_remote_device():
    with pytest.raises(ForwardingSidecarError) as caught:
        select_authority(owner_rows(), owner=OWNER, pin=None, tailnet_id=SPACE,
                         local_device_id='bob-laptop', authority_default='self')
    assert caught.value.code == 'MIGRATION_AUTHORITY_MISSING'


def test_sole_local_owner_row_is_the_authority():
    rows = [member(OWNER, LOCAL_DEVICE, address='addr'), member('bob', 'bob-laptop', address='addr-b')]
    authority = select_authority(rows, owner=OWNER, pin=None, tailnet_id=SPACE,
                                 local_device_id=LOCAL_DEVICE, authority_default=None)
    assert authority['deviceId'] == LOCAL_DEVICE


def test_sole_remote_owner_row_is_not_elected_without_a_pin():
    # Authority is a LOCAL fact: a member node must import a pin, never infer one from rows.
    rows = [member(OWNER, 'alice-only', address='addr'), member('bob', 'bob-laptop', address='addr-b')]
    with pytest.raises(ForwardingSidecarError) as caught:
        select_authority(rows, owner=OWNER, pin=None, tailnet_id=SPACE,
                         local_device_id='bob-laptop', authority_default=None)
    assert caught.value.code == 'MIGRATION_AUTHORITY_MISSING'
    assert 'alice-only' in str(caught.value) and 'bob-laptop' not in str(caught.value)
    assert f'hyprial org pin-authority {SPACE} --device' in str(caught.value)


def test_pin_is_used_as_is_without_requiring_a_matching_row():
    pin = {'owner': OWNER, 'deviceId': 'alice-pinned', 'masterPublic': key('m'), 'serverPublic': key('s'),
           'address': 'addr-pinned', 'controlPort': 7449}
    authority = select_authority(owner_rows(), owner=OWNER, pin=pin, tailnet_id=SPACE,
                                 local_device_id=LOCAL_DEVICE, authority_default=None)
    assert authority == pin


def test_settings_accept_only_self_as_authority_default(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'forwarding': {'tailnet': {'authorityDefault': 'self'}}}))
    assert tailnet_settings(tmp_path)['authorityDefault'] == 'self'
    (tmp_path / 'settings.json').write_text(json.dumps({'forwarding': {'tailnet': {}}}))
    assert tailnet_settings(tmp_path)['authorityDefault'] is None
    (tmp_path / 'settings.json').write_text(json.dumps({'forwarding': {'tailnet': {'authorityDefault': 'newest'}}}))
    with pytest.raises(ForwardingSidecarError) as caught:
        tailnet_settings(tmp_path)
    assert caught.value.code == 'BAD_REQUEST'


@pytest.fixture
def node(tmp_path):
    node = LegacyHome(tmp_path)
    yield node
    node.close()


def test_migration_refuses_multi_device_owner_without_pin_before_any_sidecar_write(node):
    node.seed_org('group-two', [legacy_row(OWNER, LOCAL_DEVICE), legacy_row(OWNER, 'alice-old')])
    with pytest.raises(ForwardingSidecarError) as caught:
        migrate_for_app(node.app())
    assert caught.value.code == 'MIGRATION_AUTHORITY_MISSING'
    assert [op for op, _ in node.sidecar.calls] == ['membership-status']


def test_migration_with_authority_default_self_imports_from_the_local_device(node):
    space = node.seed_org('group-two', [legacy_row(OWNER, LOCAL_DEVICE), legacy_row(OWNER, 'alice-old')])
    node.write_settings({'authorityDefault': 'self'})
    migrate_for_app(node.app())
    receipt = json.loads(receipt_path(node.home, space).read_text())
    assert receipt['state'] == 'complete'
    assert receipt['params']['authority']['deviceId'] == LOCAL_DEVICE
    assert sorted(row['deviceId'] for row in receipt['params']['members']) == sorted(['alice-old', LOCAL_DEVICE])
    assert node.settings()['forwarding']['tailnet']['authorities'][space]['deviceId'] == LOCAL_DEVICE


def test_member_node_refuses_without_a_pin_even_for_a_sole_remote_owner_row(tmp_path):
    bob = LegacyHome(tmp_path, owner='bob', device='bob-laptop')
    try:
        bob.seed_org('group-one', [legacy_row(OWNER, 'alice-only'), legacy_row('bob', 'bob-laptop')],
                     space_owner=OWNER, members=('bob',))
        with pytest.raises(ForwardingSidecarError) as caught:
            migrate_for_app(bob.app())
        assert caught.value.code == 'MIGRATION_AUTHORITY_MISSING'
        assert [op for op, _ in bob.sidecar.calls] == ['membership-status']
    finally:
        bob.close()
