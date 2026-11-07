"""Item F: a refused import fails before the sidecar is launched, not after a long wait."""
import json

import pytest

from hyprial.daemon.impl.forwarding.membership.migration import (
    migrate_for_app, plan_migrations, receipt_path, start_before_transport,
)
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from tests.daemon.forwarding.membership.hardening.support import LOCAL_DEVICE, OWNER, LegacyHome, legacy_row

LISTEN = ('unixsock-stream//tmp/hr-migfix-zenoh.sock',)


@pytest.fixture
def node(tmp_path):
    node = LegacyHome(tmp_path)
    yield node
    node.close()


def test_refused_import_raises_before_any_sidecar_launch(node):
    node.seed_org('group-two', [legacy_row(OWNER, LOCAL_DEVICE), legacy_row(OWNER, 'alice-old')])
    with pytest.raises(ForwardingSidecarError) as caught:
        start_before_transport(node.app(), listen=LISTEN, connect=())
    assert caught.value.code == 'MIGRATION_AUTHORITY_MISSING'
    assert node.supervisor_starts == 0
    assert node.sidecar.calls == []


def test_healthy_import_still_starts_the_sidecar_then_migrates(node):
    space = node.seed_org('group-one', [legacy_row(OWNER, LOCAL_DEVICE)])
    app = node.app()
    start_before_transport(app, listen=LISTEN, connect=())
    assert node.supervisor_starts == 1 and app.zenoh_listen == LISTEN
    assert json.loads(receipt_path(node.home, space).read_text())['state'] == 'complete'
    assert [op for op, _ in node.sidecar.calls][:2] == ['membership-status', 'membership-init']


def test_plan_ignores_tailnets_the_sidecar_store_already_holds(node):
    space = node.seed_org('group-two', [legacy_row(OWNER, LOCAL_DEVICE), legacy_row(OWNER, 'alice-old')])
    store = node.home / 'tailnet' / 'membership.json'
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({'schemaVersion': 1, 'tailnets': {space: {}}, 'operations': {}}))
    assert plan_migrations(node.app()) == []
    assert node.events == []


def test_plan_is_executed_once_even_when_passed_explicitly(node):
    space = node.seed_org('group-one', [legacy_row(OWNER, LOCAL_DEVICE)])
    app = node.app()
    plans = plan_migrations(app)
    assert [plan['tailnetId'] for plan in plans] == [space]
    migrate_for_app(app, plans)
    migrate_for_app(app, plans)
    assert [op for op, _ in node.sidecar.calls].count('membership-init') == 1


# --- Round 2: a 0.5.5.1 `pending` receipt is advisory, never replayed verbatim ---

def sidecar_row(row: dict) -> dict:
    from hyprial.daemon.impl.forwarding.membership.migration import parse_legacy_member
    return {key: value for key, value in parse_legacy_member(row).items() if key != 'updatedAt'}


def legacy_pending_receipt(node, space, rows, *, authority_device, last_error=None):
    """Write a receipt exactly as 0.5.5.1 left it: params replayed verbatim on every start."""
    import hashlib
    from hyprial.daemon.impl.forwarding.membership.settings import durable_json_write
    members = [sidecar_row(row) for row in rows]
    authority = next(row for row in members if row['deviceId'] == authority_device)
    params = {'operationId': 'migration-' + hashlib.sha256(space.encode()).hexdigest(),
              'tailnetId': space,
              'authority': {key: authority[key] for key in ('owner', 'deviceId', 'masterPublic', 'serverPublic', 'address')}
              | {'controlPort': 7448},
              'migration': {'source': 'directory-0.5.5', 'sourceDigest': 'legacy', 'completed': True},
              'members': members}
    receipt = {'state': 'pending', 'params': params,
               'diagnostics': {'sourceDigest': 'legacy', 'accepted': [row['deviceId'] for row in members], 'rejected': []}}
    if last_error is not None:
        receipt['lastError'] = last_error
    durable_json_write(receipt_path(node.home, space), receipt)
    return receipt


@pytest.fixture
def renamed(tmp_path):
    node = LegacyHome(tmp_path, device='Dev000000000000000002.local')
    yield node
    node.close()


def reviewer_scenario(node):
    local = legacy_row(OWNER, node.device, updated_at='2026-10-20T05:52:31Z')
    stale = dict(local, deviceId='devusr-dev', updatedAt='2026-10-17T05:52:31Z')
    bob = legacy_row('bob', 'bob-laptop')
    internal = node.seed_org('group-internal', [local, stale, bob], members=('bob',))
    dead = node.seed_org('group-dead', [], state='deleted')
    legacy_pending_receipt(node, internal, [local, stale, bob], authority_device=node.device,
                           last_error={'code': 'MEMBER_CONFLICT', 'at': '2026-10-28T01:00:00+00:00'})
    # The deleted org's receipt was written while the org was still alive.
    legacy_pending_receipt(node, dead, [local], authority_device=node.device)
    return internal, dead


def test_pending_receipt_is_recomputed_not_replayed_verbatim(renamed):
    node = renamed
    internal, dead = reviewer_scenario(node)
    migrate_for_app(node.app())
    inits = [params for op, params in node.sidecar.calls if op == 'membership-init']
    assert [params['tailnetId'] for params in inits] == [internal]
    assert sorted(row['deviceId'] for row in inits[0]['members']) == sorted([node.device, 'bob-laptop'])
    receipt = json.loads(receipt_path(node.home, internal).read_text())
    assert receipt['state'] == 'complete' and receipt['params'] == inits[0]
    assert receipt['diagnostics']['droppedDeviceIds'] == ['devusr-dev']
    superseded = receipt['diagnostics']['supersededParams']
    assert superseded['reason'] == 'recomputed-on-startup'
    assert sorted(row['deviceId'] for row in superseded['params']['members']) == sorted([node.device, 'devusr-dev', 'bob-laptop'])
    assert receipt.get('lastError') is None  # cleared by the successful replay
    skipped = {fields['spaceId']: fields['reason'] for _l, event, fields in node.events
               if event == 'membership.migration.skipped'}
    assert skipped == {dead: 'org-deleted'}
    dead_receipt = json.loads(receipt_path(node.home, dead).read_text())
    assert dead_receipt['state'] == 'skipped' and dead_receipt['skipReason'] == 'org-deleted'


def test_recomputed_pending_receipt_keeps_last_error_until_a_reply_succeeds(renamed):
    node = renamed
    internal, _dead = reviewer_scenario(node)
    plan_migrations(node.app())  # no sidecar: only the local recompute happens
    receipt = json.loads(receipt_path(node.home, internal).read_text())
    assert receipt['state'] == 'pending'
    assert receipt['lastError'] == {'code': 'MEMBER_CONFLICT', 'at': '2026-10-28T01:00:00+00:00'}
    assert 'devusr-dev' not in [row['deviceId'] for row in receipt['params']['members']]
    assert receipt['diagnostics']['supersededParams']['reason'] == 'recomputed-on-startup'


def test_complete_receipts_are_authoritative_and_never_recomputed(renamed):
    node = renamed
    local = legacy_row(OWNER, node.device)
    stale = dict(local, deviceId='devusr-dev', updatedAt='2026-10-17T05:52:31Z')
    space = node.seed_org('group-internal', [local, stale])
    receipt = legacy_pending_receipt(node, space, [local, stale], authority_device=node.device)
    receipt['state'] = 'complete'
    receipt_path(node.home, space).write_text(json.dumps(receipt))
    node.sidecar.seed(space, authority=receipt['params']['authority'], members=receipt['params']['members'])
    migrate_for_app(node.app())
    assert json.loads(receipt_path(node.home, space).read_text()) == receipt
    assert not any(op == 'membership-init' for op, _ in node.sidecar.calls)
