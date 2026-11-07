"""Degraded-record follow-ups from the 0.5.5.3.4 review: no retryable record may
outlive a permanently refused receipt, and pre-``retryable`` records are read honestly."""

from __future__ import annotations

import json
from types import SimpleNamespace

from hyprial.daemon.impl.forwarding.membership import migration


def _receipt(home, tailnet_id: str, invite_id: str, code: str | None) -> None:
    path = migration.receipt_path(home, tailnet_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    invite = {'tailnetId': tailnet_id, 'inviteId': invite_id, 'authority': {}, 'token': 'secret'}
    receipt = {
        'state': 'pending',
        'params': {'tailnetId': tailnet_id, 'authority': {}},
        'redemption': {'operationId': 'redeem-' + invite_id, 'invite': invite},
        'diagnostics': {'source': 'invite-v2'},
    }
    if code is not None:
        receipt['lastError'] = {'code': code, 'at': '2026-11-07T00:00:00+00:00'}
    path.write_text(json.dumps(receipt))


def _app(home, calls: list[str]):
    def rpc(op, params):
        calls.append(op)
        raise AssertionError('a stale invite must not be replayed')

    return SimpleNamespace(hyprial_home=home, _forwarding_supervisor=SimpleNamespace(membership_request=rpc))


def test_a_retryable_record_over_a_stale_receipt_turns_stale_after_one_tick(tmp_path, monkeypatch):
    """A transient degrade, then an explicit join of the same inviteId refused permanently."""
    migration.record_degraded_redemption(tmp_path, tailnet_id='space', invite_id='old',
                                         code='AUTHORITY_UNREACHABLE', error='unreachable')
    record = migration.read_degraded_redemption(tmp_path)
    assert record['retryable'] is True
    record['nextAttemptAtMs'] = 0
    migration.degraded_redemption_path(tmp_path).write_text(json.dumps(record))
    _receipt(tmp_path, 'space', 'old', 'INVITE_EXPIRED')
    calls: list[str] = []
    app = _app(tmp_path, calls)

    migration.retry_degraded_redemption(app)
    held = migration.read_degraded_redemption(tmp_path)
    assert held['code'] == 'INVITE_EXPIRED' and held['retryable'] is False
    assert held['nextAttemptAtMs'] is None

    reads = []
    original = migration.read_receipts
    monkeypatch.setattr(migration, 'read_receipts', lambda home: reads.append(1) or original(home))
    for _ in range(20):
        migration.retry_degraded_redemption(app)
    assert reads == [] and calls == []


def test_a_start_rewrites_a_retryable_record_whose_receipt_is_stale(tmp_path):
    migration.record_degraded_redemption(tmp_path, tailnet_id='space', invite_id='old',
                                         code='AUTHORITY_UNREACHABLE', error='unreachable')
    _receipt(tmp_path, 'space', 'old', 'INVITE_EXPIRED')
    calls: list[str] = []
    migration.hold_stale_redemptions(_app(tmp_path, calls), migration.read_receipts(tmp_path))
    record = migration.read_degraded_redemption(tmp_path)
    assert record['retryable'] is False and record['code'] == 'INVITE_EXPIRED'
    assert calls == []


def test_a_record_written_before_retryable_existed_is_read_by_its_code(tmp_path, monkeypatch):
    path = migration.degraded_redemption_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'tailnetId': 'space', 'inviteId': 'old', 'code': 'INVITE_EXPIRED',
                                'error': 'expired', 'attempts': 3, 'nextAttemptAtMs': 0}))
    record = migration.read_degraded_redemption(tmp_path)
    assert record['retryable'] is False and record['nextAttemptAtMs'] is None

    reads = []
    monkeypatch.setattr(migration, 'read_receipts', lambda home: reads.append(1) or {})
    migration.retry_degraded_redemption(_app(tmp_path, []))
    assert reads == []

    path.write_text(json.dumps({'tailnetId': 'space', 'inviteId': 'old', 'code': 'AUTHORITY_UNREACHABLE',
                                'error': 'unreachable', 'attempts': 1, 'nextAttemptAtMs': 5}))
    legacy_transient = migration.read_degraded_redemption(tmp_path)
    assert legacy_transient['retryable'] is True and legacy_transient['nextAttemptAtMs'] == 5
