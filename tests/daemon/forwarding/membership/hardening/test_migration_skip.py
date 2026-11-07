"""Item A: deleted, empty and left org spaces are skipped with one event, never fatal."""
import json

import pytest

from hyprial.daemon.impl.forwarding.membership.migration import migrate_for_app, receipt_path
from hyprial.daemon.impl.org.network.state import left_orgs_path
from tests.daemon.forwarding.membership.hardening.support import LOCAL_DEVICE, OWNER, LegacyHome, legacy_row


@pytest.fixture
def node(tmp_path):
    node = LegacyHome(tmp_path)
    yield node
    node.close()


def skipped(node):
    return {fields['spaceId']: fields for level, event, fields in node.events
            if event == 'membership.migration.skipped'}


def test_deleted_org_with_ambiguous_owner_rows_is_skipped_not_fatal(node):
    dead = node.seed_org('group-dead', [legacy_row(OWNER, LOCAL_DEVICE), legacy_row(OWNER, 'alice-old')],
                         state='deleted')
    live = node.seed_org('group-live', [legacy_row(OWNER, LOCAL_DEVICE)])
    migrate_for_app(node.app())
    events = skipped(node)
    assert events[dead]['reason'] == 'org-deleted' and events[dead]['org'] == 'group-dead'
    assert dead not in node.sidecar.tailnets and not receipt_path(node.home, dead).exists()
    assert json.loads(receipt_path(node.home, live).read_text())['state'] == 'complete'
    assert live in node.sidecar.tailnets


def test_probe_org_with_zero_rows_is_skipped(node):
    probe = node.seed_org('group-probe-e2e', [])
    migrate_for_app(node.app())
    assert skipped(node)[probe]['reason'] == 'no-device-rows'
    assert probe not in node.sidecar.tailnets
    assert list(node.events).count(('info', 'membership.migration.skipped', skipped(node)[probe])) == 1


def test_left_org_is_skipped(node):
    gone = node.seed_org('group-gone', [legacy_row(OWNER, LOCAL_DEVICE)])
    left_orgs_path(node.home).parent.mkdir(parents=True, exist_ok=True)
    left_orgs_path(node.home).write_text(json.dumps(['group-gone']))
    migrate_for_app(node.app())
    assert skipped(node)[gone]['reason'] == 'left'
    assert gone not in node.sidecar.tailnets


def test_skipped_space_leaves_no_receipt_and_is_reconsidered_next_start(node):
    probe = node.seed_org('group-later', [])
    migrate_for_app(node.app())
    assert not receipt_path(node.home, probe).exists()
    node.events.clear()
    migrate_for_app(node.app())
    assert skipped(node)[probe]['reason'] == 'no-device-rows'
