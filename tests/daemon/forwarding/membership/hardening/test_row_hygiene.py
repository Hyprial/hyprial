"""Item D: rows are validated and de-duplicated the way the sidecar would refuse them."""
import json

import pytest

from hyprial.daemon.impl.forwarding.membership.hardening.rows import dedupe_rows, validate_row
from hyprial.daemon.impl.forwarding.membership.migration import (
    migrate_for_app, offline_rows, parse_legacy_member, receipt_path,
)
from tests.daemon.forwarding.membership.hardening.support import OWNER, LegacyHome, legacy_row
from tests.support.tailnet_membership import key, member


def test_validate_row_mirrors_sidecar_sender_rules():
    good = member(OWNER, 'd1', address='addr')
    assert validate_row(good) is None
    assert validate_row(good | {'owner': ''}) == 'MISSING_OWNER'
    assert validate_row(good | {'deviceId': ''}) == 'MISSING_DEVICE_ID'
    assert validate_row(good | {'masterPublic': 'nodekey:xyz'}) == 'INVALID_KEY'
    assert validate_row(good | {'serverPublic': good['masterPublic'].upper()}) == 'INVALID_KEY'
    assert validate_row(good | {'serverPublic': good['masterPublic']}) == 'KEYS_EQUAL'
    assert validate_row(good | {'address': ''}) == 'MISSING_ADDRESS'


def test_dedupe_keeps_the_newest_row_sharing_any_key_and_treats_bad_updated_at_as_oldest():
    renamed = member(OWNER, 'Dev000000000000000002.local', address='same') | {'updatedAt': '2026-10-20T10:00:00Z'}
    stale = dict(renamed, deviceId='devusr-dev', updatedAt='2026-10-17T10:00:00Z')
    corrupt = member(OWNER, 'corrupt', address='c') | {'updatedAt': '2026-10-197T005:52:31Z'}
    shares_server = member(OWNER, 'shares-server', address='s') | {
        'serverPublic': corrupt['serverPublic'], 'updatedAt': '2026-10-01T00:00:00Z'}
    kept, dropped = dedupe_rows([stale, renamed, corrupt, shares_server, member('bob', 'bob-laptop', address='b')])
    assert [row['deviceId'] for row in kept] == ['Dev000000000000000002.local', 'shares-server', 'bob-laptop']
    assert dropped == [
        {'deviceId': 'devusr-dev', 'keptDeviceId': 'Dev000000000000000002.local', 'reason': 'DUPLICATE_KEY'},
        {'deviceId': 'corrupt', 'keptDeviceId': 'shares-server', 'reason': 'DUPLICATE_KEY'},
    ]


def test_legacy_parser_tolerates_a_corrupted_updated_at():
    row = legacy_row(OWNER, 'd1', updated_at='2026-10-197T005:52:31Z')
    parsed = parse_legacy_member(row)
    assert parsed['deviceId'] == 'd1' and parsed['updatedAt'] is None
    assert parse_legacy_member(legacy_row(OWNER, 'd2'))['updatedAt'] == '2026-10-20T00:00:00Z'


@pytest.fixture
def node(tmp_path):
    node = LegacyHome(tmp_path, device='Dev000000000000000002.local')
    yield node
    node.close()


def production_rows(node):
    local = legacy_row(OWNER, node.device, updated_at='2026-10-20T05:52:31Z')
    stale = dict(local, deviceId='devusr-dev', updatedAt='2026-10-17T05:52:31Z')
    corrupt = legacy_row('bob', 'bob-corrupt', updated_at='2026-10-197T005:52:31Z')
    bad_key = legacy_row('bob', 'bob-badkey', server=key('client-bob-badkey'))  # equals its client key
    return [local, stale, corrupt, bad_key, legacy_row('bob', 'bob-laptop')]


def test_offline_rows_reports_validated_deduplicated_rows_without_updated_at(node):
    space_id = node.seed_org('group-internal', production_rows(node), members=('bob',))
    space = next(space for space in node.fs.spaces() if space.space_id == space_id)
    rows, diagnostics = offline_rows(node.fs, space)
    assert sorted(row['deviceId'] for row in rows) == sorted(['bob-corrupt', 'bob-laptop', node.device])
    assert all('updatedAt' not in row for row in rows)
    assert diagnostics['droppedDeviceIds'] == ['devusr-dev']
    assert diagnostics['dropped'] == [{'deviceId': 'devusr-dev', 'keptDeviceId': node.device, 'reason': 'DUPLICATE_KEY'}]
    assert [item['code'] for item in diagnostics['rejected']] == ['KEYS_EQUAL']


def test_renamed_host_duplicate_no_longer_makes_the_owner_ambiguous(node):
    space = node.seed_org('group-internal', production_rows(node), members=('bob',))
    migrate_for_app(node.app())
    receipt = json.loads(receipt_path(node.home, space).read_text())
    assert receipt['state'] == 'complete'
    assert receipt['params']['authority']['deviceId'] == node.device
    init = next(params for op, params in node.sidecar.calls if op == 'membership-init')
    assert init == receipt['params']  # the receipt stores the request actually sent
    assert sorted(row['deviceId'] for row in init['members']) == sorted(['bob-corrupt', 'bob-laptop', node.device])
    assert receipt['diagnostics']['droppedDeviceIds'] == ['devusr-dev']
    assert 'bob-corrupt' not in [item.get('path', '') for item in receipt['diagnostics']['rejected']]
