"""The single degraded-redemption record across tailnets, stale and retryable.

Review follow-up for 0.5.5.3.4 (#2319 x #2484): a stale (``retryable: false``)
record must never clobber another tailnet's record, restarts must converge on
the same record whatever the receipt glob order, a redemption completed at
startup clears its tailnet's record, a stale record costs the reconcile tick
no disk read, and an upgraded stale receipt keeps its stage-tagged text.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.forwarding.membership import migration
from hyprial.daemon.impl.forwarding.membership.settings import durable_json_write
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError


def seed(home, tailnet_id, *, stale_code=None):
    invite = {'tailnetId': tailnet_id, 'inviteId': 'invite-' + tailnet_id, 'authority': {}}
    receipt = {'state': 'pending',
               'params': {'tailnetId': tailnet_id, 'authority': {}},
               'redemption': {'operationId': 'redeem-' + invite['inviteId'], 'invite': invite},
               'diagnostics': {'source': 'invite-v2'}}
    if stale_code is not None:
        receipt['lastError'] = {'code': stale_code, 'at': '2026-11-01T00:00:00+00:00'}
    durable_json_write(migration.receipt_path(home, tailnet_id), receipt)


class Authority:
    def __init__(self):
        self.reachable: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, op, params):
        if op == 'membership-status':
            return {'tailnets': []}
        assert op == 'invite-redeem'
        tailnet_id = params['invite']['tailnetId']
        self.calls.append(tailnet_id)
        if tailnet_id not in self.reachable:
            raise ForwardingSidecarError('Tailnet membership command failed.', code='AUTHORITY_UNREACHABLE')
        return {}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, 'read_device_record', lambda home: object())
    return tmp_path


def ordered_receipts(monkeypatch, order):
    real = migration.read_receipts

    def read(home):
        receipts = real(home)
        return {key: receipts[key] for key in order if key in receipts}
    monkeypatch.setattr(migration, 'read_receipts', read)


def app_for(home, authority):
    return SimpleNamespace(hyprial_home=home,
                           _forwarding_supervisor=SimpleNamespace(membership_request=authority))


def due(home):
    record = migration.read_degraded_redemption(home)
    record['nextAttemptAtMs'] = 0
    migration.atomic_json_write(migration.degraded_redemption_path(home), record)


@pytest.mark.parametrize('order', [('stale', 'live'), ('live', 'stale')])
def test_stale_record_never_clobbers_a_retryable_one(home, monkeypatch, order):
    ordered_receipts(monkeypatch, order)
    authority = Authority()
    app = app_for(home, authority)
    seed(home, 'stale', stale_code='INVITE_EXPIRED')
    migration.migrate_for_app(app, plans=[])
    stale_record = migration.read_degraded_redemption(home)
    assert (stale_record['tailnetId'], stale_record['retryable']) == ('stale', False)
    seed(home, 'live')
    for attempt in (1, 2, 3):
        migration.migrate_for_app(app, plans=[])
        record = migration.read_degraded_redemption(home)
        assert (record['tailnetId'], record['code'], record['retryable']) == ('live', 'AUTHORITY_UNREACHABLE', True)
        assert record['attempts'] == attempt
    assert authority.calls == ['live', 'live', 'live']
    authority.reachable.add('live')
    due(home)
    migration.retry_degraded_redemption(app)
    record = migration.read_degraded_redemption(home)
    assert (record['tailnetId'], record['code'], record['retryable']) == ('stale', 'INVITE_EXPIRED', False)
    assert record['attempts'] == 1
    assert authority.calls == ['live'] * 4


@pytest.mark.parametrize('order', [('a', 'b'), ('b', 'a')])
def test_restarts_converge_on_one_stale_record_whatever_the_glob_order(home, monkeypatch, order):
    ordered_receipts(monkeypatch, order)
    authority = Authority()
    seed(home, 'a', stale_code='INVITE_EXPIRED')
    seed(home, 'b', stale_code='INVITE_CONSUMED')
    app = app_for(home, authority)
    migration.migrate_for_app(app, plans=[])
    first = migration.read_degraded_redemption(home)
    assert (first['tailnetId'], first['attempts']) == ('a', 1)
    for _ in range(3):
        migration.migrate_for_app(app, plans=[])
        assert migration.read_degraded_redemption(home) == first
    assert authority.calls == []


def test_startup_completion_clears_its_tailnets_record(home):
    authority = Authority()
    app = app_for(home, authority)
    seed(home, 'space')
    migration.migrate_for_app(app, plans=[])
    assert migration.read_degraded_redemption(home)['tailnetId'] == 'space'
    authority.reachable.add('space')
    migration.migrate_for_app(app, plans=[])
    assert migration.read_degraded_redemption(home) is None
    assert migration.read_receipts(home)['space']['state'] == 'complete'


def test_stale_record_costs_the_tick_no_disk_read(home, monkeypatch):
    seed(home, 'space', stale_code='INVITE_EXPIRED')
    app = app_for(home, Authority())
    migration.migrate_for_app(app, plans=[])
    assert migration.read_degraded_redemption(home)['retryable'] is False
    reads = []
    monkeypatch.setattr(migration, 'read_receipts', lambda home: reads.append(home) or {})
    for _ in range(100):
        migration.retry_degraded_redemption(app)
    assert reads == []


@pytest.mark.parametrize('code', ['INVITE_EXPIRED', 'INVITE_CONSUMED'])
def test_upgraded_stale_receipt_keeps_the_stage_tagged_reason(home, code):
    seed(home, 'space', stale_code=code)
    migration.migrate_for_app(app_for(home, Authority()), plans=[])
    record = migration.read_degraded_redemption(home)
    assert record['code'] == code and 'stage=invite-redemption' in record['error']
    assert code in record['error'] or record['error'] == migration.EXPIRED_REDEMPTION_TEXT
    if code == 'INVITE_EXPIRED':
        assert record['error'] == migration.EXPIRED_REDEMPTION_TEXT
