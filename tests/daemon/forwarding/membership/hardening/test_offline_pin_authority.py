"""Item C: operator pin for not-yet-migrated tailnets works without a daemon."""
import json

import pytest

from hyprial.daemon.impl.forwarding.membership.hardening.offline import OfflineMembership
from hyprial.daemon.impl.forwarding.membership.migration import migrate_for_app, receipt_path
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from tests.daemon.forwarding.membership.hardening.support import LOCAL_DEVICE, OWNER, LegacyHome, legacy_row
from tests.support.tailnet_membership import key


@pytest.fixture
def node(tmp_path):
    node = LegacyHome(tmp_path)
    yield node
    node.close()


def two_device_org(node, org='group-two'):
    space = node.seed_org(org, [legacy_row(OWNER, LOCAL_DEVICE), legacy_row(OWNER, 'alice-old'),
                                legacy_row('bob', 'bob-laptop')], members=('bob',))
    node.close()
    return space


def test_pin_authority_resolves_the_row_offline_and_writes_the_settings_pin(node):
    space = two_device_org(node)
    with OfflineMembership(node.home, node.state_dir) as tool:
        preview = tool.pin_authority(space, 'alice-old', confirmed=False)
        assert preview['candidates'] == sorted(['alice-old', LOCAL_DEVICE])
        assert preview['pinned'] is False and preview['deviceId'] == 'alice-old'
        assert 'nodekey:' not in json.dumps(preview) and 'address-of' not in json.dumps(preview)
        assert node.settings() == {}
        result = tool.pin_authority(space, 'alice-old', confirmed=True)
    assert result['pinned'] is True and result['restartRequired'] is True
    pin = node.settings()['forwarding']['tailnet']['authorities'][space]
    assert pin == {'owner': OWNER, 'deviceId': 'alice-old', 'masterPublic': key('client-alice-old'),
                   'serverPublic': key('server-alice-old'), 'address': 'address-of-alice-old', 'controlPort': 7448}


def test_pin_then_migration_uses_the_pinned_device(node):
    space = two_device_org(node)
    with OfflineMembership(node.home, node.state_dir) as tool:
        tool.pin_authority(space, 'alice-old', confirmed=True)
    node.reopen()
    try:
        migrate_for_app(node.app())
    except ForwardingSidecarError:
        # The double has no remote authority to fetch a roster from; init already ran.
        assert [op for op, _ in node.sidecar.calls][-1] == 'roster-fetch'
    assert [op for op, _ in node.sidecar.calls][1] == 'membership-init'
    receipt = json.loads(receipt_path(node.home, space).read_text())
    assert receipt['params']['authority']['deviceId'] == 'alice-old'
    assert receipt['params']['members'] == []  # this node is not the authority: pin only


def test_pin_authority_refuses_unknown_device_and_initialized_tables(node):
    space = two_device_org(node)
    with OfflineMembership(node.home, node.state_dir) as tool:
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.pin_authority(space, 'bob-laptop', confirmed=True)
        assert caught.value.code == 'MEMBER_UNKNOWN'
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.pin_authority('no-such-space', LOCAL_DEVICE, confirmed=True)
        assert caught.value.code == 'SPACE_UNKNOWN'
    store = node.home / 'tailnet' / 'membership.json'
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({'schemaVersion': 1, 'tailnets': {space: {}}, 'operations': {}}))
    with OfflineMembership(node.home, node.state_dir) as tool:
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.pin_authority(space, LOCAL_DEVICE, confirmed=True)
    assert caught.value.code == 'MEMBERSHIP_ALREADY_INITIALIZED'
    assert 'org repin' in str(caught.value)
    assert node.settings() == {}


def test_pin_authority_never_replaces_a_different_pin(node):
    space = two_device_org(node)
    with OfflineMembership(node.home, node.state_dir) as tool:
        tool.pin_authority(space, 'alice-old', confirmed=True)
        tool.pin_authority(space, 'alice-old', confirmed=True)  # idempotent
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.pin_authority(space, LOCAL_DEVICE, confirmed=True)
    assert caught.value.code == 'AUTHORITY_PIN_MISMATCH'


def test_migration_status_lists_every_org_space(node):
    two = node.seed_org('group-two', [legacy_row(OWNER, LOCAL_DEVICE), legacy_row(OWNER, 'alice-old')])
    dead = node.seed_org('group-dead', [], state='deleted')
    done = node.seed_org('group-done', [legacy_row(OWNER, LOCAL_DEVICE)])
    node.write_settings({'authorityDefault': 'self'})
    migrate_for_app(node.app())
    node.close()
    with OfflineMembership(node.home, node.state_dir) as tool:
        rows = {row['spaceId']: row for row in tool.migration_status()}
    assert set(rows) == {two, dead, done}
    assert rows[dead]['skipReason'] == 'org-deleted' and rows[dead]['receipt'] is None
    assert rows[done]['receipt']['state'] == 'complete' and rows[done]['pinnedDeviceId'] == LOCAL_DEVICE
    assert rows[done]['rows'] == {'accepted': [LOCAL_DEVICE], 'rejected': [], 'dropped': []}
    assert rows[two]['pinnedDeviceId'] == LOCAL_DEVICE and rows[two]['receipt']['state'] == 'complete'
    assert rows[two]['lastError'] is None
    assert 'nodekey:' not in json.dumps(rows) and 'address-of' not in json.dumps(rows)
