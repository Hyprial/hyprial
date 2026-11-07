"""A pending invite redemption must degrade startup, not abort it.

Red-first for brief-degraded-start (2026-11-03): when the resume of a pending
redemption cannot reach the invite authority, the daemon must still come up
serving, record the exact reason for doctor/status, and finish the redemption
later on a bounded-backoff retry without a restart. Every other membership
resume failure stays fatal.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from tests.daemon.forwarding.membership.hardening.support import (
    LOCAL_DEVICE,
    OWNER,
    LegacyHome,
    _Supervisor,
    legacy_row,
)

LISTEN = ('unixsock-stream//tmp/hr-degraded-redemption.sock',)


class _QuietRuntime:
    def on_timer(self, _observed_at_ms: int | None = None) -> None:
        return None

    def stop(self) -> None:
        return None

INVITE = {'version': 2, 'tailnetId': 'space', 'inviteId': 'i1', 'owner': OWNER,
          'authority': {'owner': OWNER, 'deviceId': 'hq', 'masterPublic': 'm',
                        'serverPublic': 's', 'address': 'addr-hq', 'controlPort': 7448},
          'token': 'secret', 'expiresAtMs': 9999999999999}


def degraded_events(node: LegacyHome, name: str) -> list[dict[str, Any]]:
    return [fields for level, event, fields in node.events if event == name]


def seed_pending_redemption(node: LegacyHome, space: str, invite: dict) -> None:
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path
    from hyprial.kernel import atomic_json_write
    receipt = {'state': 'pending',
               'params': {'tailnetId': space, 'authority': invite['authority']},
               'redemption': {'operationId': 'redeem-' + invite['inviteId'], 'invite': invite},
               'diagnostics': {'source': 'invite-v2'}}
    atomic_json_write(receipt_path(node.home, space), receipt)


def seed_pending_import(node: LegacyHome, space: str) -> None:
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path
    from hyprial.kernel import atomic_json_write
    receipt = {'state': 'pending',
               'params': {'operationId': 'migration-x', 'tailnetId': space,
                          'authority': INVITE['authority'],
                          'migration': {'source': 'directory-0.5.5', 'sourceDigest': 'd', 'completed': True},
                          'members': []}}
    atomic_json_write(receipt_path(node.home, space), receipt)


def force_retry_due(node: LegacyHome) -> None:
    from hyprial.daemon.impl.forwarding.membership.migration import (
        degraded_redemption_path, read_degraded_redemption,
    )
    from hyprial.kernel import atomic_json_write
    record = read_degraded_redemption(node.home)
    assert record is not None
    record['nextAttemptAtMs'] = 0
    atomic_json_write(degraded_redemption_path(node.home), record)


class _RetryApp:
    def __init__(self, node: LegacyHome):
        self.hyprial_home = node.home
        self._forwarding_supervisor = _Supervisor(node.sidecar)
        self.logs: list[tuple[str, str, str, dict[str, Any]]] = []

    def _log(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self.logs.append((level, domain, event, fields))

    def retry(self) -> None:
        from hyprial.daemon.impl.application.netendpoints.forwarding import _ForwardingOpsMixin
        _ForwardingOpsMixin._retry_degraded_redemption_throttled(self)

    def projection(self) -> dict[str, Any] | None:
        from hyprial.daemon.impl.application.netendpoints.forwarding import _ForwardingOpsMixin
        return _ForwardingOpsMixin._membership_redemption_projection(self)

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for _level, _domain, name, fields in self.logs if name == event]


@pytest.fixture
def node(tmp_path):
    home = LegacyHome(tmp_path)
    yield home
    home.close()


def test_pending_redemption_resume_failure_does_not_abort_start(node):
    from hyprial.daemon.impl.forwarding.membership.migration import (
        read_degraded_redemption, receipt_path, start_before_transport,
    )
    node.sidecar.failures['invite-redeem'] = 'AUTHORITY_UNREACHABLE'
    seed_pending_redemption(node, 'space', INVITE)
    app = node.app()
    start_before_transport(app, listen=LISTEN, connect=())
    assert node.supervisor_starts == 1
    record = read_degraded_redemption(node.home)
    assert record is not None
    assert record['tailnetId'] == 'space'
    assert record['code'] == 'AUTHORITY_UNREACHABLE'
    assert record['error'] == 'Fake membership operation refused.'
    assert record['attempts'] == 1
    receipt = json.loads(receipt_path(node.home, 'space').read_text())
    assert receipt['state'] == 'pending'
    assert receipt['lastError']['code'] == 'AUTHORITY_UNREACHABLE'
    degraded = degraded_events(node, 'membership.redemption.degraded')
    assert degraded and degraded[0]['code'] == 'AUTHORITY_UNREACHABLE'
    assert degraded[0]['tailnetId'] == 'space'


def test_repeat_start_keeps_serving_and_counts_attempts(node):
    from hyprial.daemon.impl.forwarding.membership.migration import (
        read_degraded_redemption, start_before_transport,
    )
    node.sidecar.failures['invite-redeem'] = 'AUTHORITY_UNREACHABLE'
    seed_pending_redemption(node, 'space', INVITE)
    start_before_transport(node.app(), listen=LISTEN, connect=())
    start_before_transport(node.app(), listen=LISTEN, connect=())
    assert node.supervisor_starts == 2
    assert read_degraded_redemption(node.home)['attempts'] == 2


def test_non_redemption_resume_failure_stays_fatal(node):
    from hyprial.daemon.impl.forwarding.membership.migration import migrate_for_app
    space = node.seed_org('group-one', [legacy_row(OWNER, LOCAL_DEVICE)])
    node.sidecar.seed(space)
    node.sidecar.failures['membership-init'] = 'MEMBER_CONFLICT'
    seed_pending_import(node, space)
    with pytest.raises(ForwardingSidecarError) as caught:
        migrate_for_app(node.app())
    assert caught.value.code == 'MEMBER_CONFLICT'
    from hyprial.daemon.impl.forwarding.membership.migration import read_degraded_redemption
    assert read_degraded_redemption(node.home) is None


def test_retry_completes_redemption_once_authority_is_reachable(node):
    from hyprial.daemon.impl.forwarding.membership.migration import (
        migrate_for_app, read_degraded_redemption, receipt_path,
    )
    node.sidecar.seed('space')
    created = node.sidecar.membership_request('invite-create', {
        'operationId': 'create-1', 'tailnetId': 'space', 'owner': OWNER,
        'expiresAtMs': node.sidecar.now_ms + 60000})
    invite = created['invite']
    seed_pending_redemption(node, 'space', invite)
    node.sidecar.failures['invite-redeem'] = 'AUTHORITY_UNREACHABLE'
    migrate_for_app(node.app())
    assert read_degraded_redemption(node.home) is not None
    del node.sidecar.failures['invite-redeem']
    force_retry_due(node)
    app = _RetryApp(node)
    app.retry()
    receipt = json.loads(receipt_path(node.home, 'space').read_text())
    assert receipt['state'] == 'complete'
    assert read_degraded_redemption(node.home) is None
    completed = app.named('membership.redemption.completed')
    assert completed and completed[0]['tailnetId'] == 'space'


def test_retry_holds_the_bounded_backoff_window(node):
    from hyprial.daemon.impl.forwarding.membership.migration import migrate_for_app
    node.sidecar.seed('space')
    created = node.sidecar.membership_request('invite-create', {
        'operationId': 'create-1', 'tailnetId': 'space', 'owner': OWNER,
        'expiresAtMs': node.sidecar.now_ms + 60000})
    seed_pending_redemption(node, 'space', created['invite'])
    node.sidecar.failures['invite-redeem'] = 'AUTHORITY_UNREACHABLE'
    migrate_for_app(node.app())
    before = len(node.sidecar.calls)
    _RetryApp(node).retry()
    assert len(node.sidecar.calls) == before
    force_retry_due(node)
    del node.sidecar.failures['invite-redeem']
    _RetryApp(node).retry()
    assert len(node.sidecar.calls) > before


def test_retry_backoff_is_bounded_and_growing():
    from hyprial.daemon.impl.forwarding.membership.migration import redemption_retry_delay_ms
    assert redemption_retry_delay_ms(1) == 30_000
    assert redemption_retry_delay_ms(2) == 60_000
    assert redemption_retry_delay_ms(10) == 600_000
    assert redemption_retry_delay_ms(1000) == 600_000
    delays = [redemption_retry_delay_ms(n) for n in range(1, 12)]
    assert delays == sorted(delays)


def test_retry_failure_records_next_attempt_and_exact_reason(node):
    from hyprial.daemon.impl.forwarding.membership.migration import (
        migrate_for_app, read_degraded_redemption,
    )
    node.sidecar.seed('space')
    created = node.sidecar.membership_request('invite-create', {
        'operationId': 'create-1', 'tailnetId': 'space', 'owner': OWNER,
        'expiresAtMs': node.sidecar.now_ms + 60000})
    seed_pending_redemption(node, 'space', created['invite'])
    node.sidecar.failures['invite-redeem'] = 'AUTHORITY_UNREACHABLE'
    migrate_for_app(node.app())
    force_retry_due(node)
    app = _RetryApp(node)
    app.retry()
    record = read_degraded_redemption(node.home)
    assert record['attempts'] == 2
    assert record['code'] == 'AUTHORITY_UNREACHABLE'
    assert record['nextAttemptAtMs'] > 0
    failed = app.named('membership.redemption.retry-failed')
    assert failed and failed[0]['code'] == 'AUTHORITY_UNREACHABLE'


def test_unexpected_retry_error_still_arms_the_backoff(node, monkeypatch):
    from hyprial.daemon.impl.forwarding.membership import migration
    node.sidecar.seed('space')
    created = node.sidecar.membership_request('invite-create', {
        'operationId': 'create-1', 'tailnetId': 'space', 'owner': OWNER,
        'expiresAtMs': node.sidecar.now_ms + 60000})
    seed_pending_redemption(node, 'space', created['invite'])
    node.sidecar.failures['invite-redeem'] = 'AUTHORITY_UNREACHABLE'
    migration.migrate_for_app(node.app())
    force_retry_due(node)

    def explode(*args, **kwargs):
        raise RuntimeError('sidecar transport torn down mid-request')

    monkeypatch.setattr(migration, 'initialize_once', explode)
    app = _RetryApp(node)
    app.retry()
    record = migration.read_degraded_redemption(node.home)
    assert record['attempts'] == 2
    assert record['code'] == 'RuntimeError'
    assert record['nextAttemptAtMs'] > node.sidecar.now_ms
    # a second tick inside the window must not retry again
    app.retry()
    assert migration.read_degraded_redemption(node.home)['attempts'] == 2


def test_ps_projection_carries_the_degraded_redemption(node):
    from hyprial.daemon.impl.forwarding.membership.migration import (
        record_degraded_redemption,
    )
    app = _RetryApp(node)
    assert app.projection() is None
    record_degraded_redemption(node.home, tailnet_id='space', invite_id='i1',
                               code='AUTHORITY_UNREACHABLE',
                               error='Tailnet membership command failed.')
    projection = app.projection()
    assert projection is not None
    assert projection['tailnetId'] == 'space'
    assert projection['code'] == 'AUTHORITY_UNREACHABLE'
    assert projection['error'] == 'Tailnet membership command failed.'


def test_degraded_record_lives_outside_the_receipt_glob(node):
    from hyprial.daemon.impl.forwarding.membership.migration import (
        degraded_redemption_path, read_receipts, receipt_path, record_degraded_redemption,
    )
    record = record_degraded_redemption(node.home, tailnet_id='space', invite_id=None,
                                        code='AUTHORITY_UNREACHABLE', error='x')
    assert record['tailnetId'] == 'space'
    assert degraded_redemption_path(node.home).parent != receipt_path(node.home, 'space').parent
    assert 'space' not in read_receipts(node.home)


def test_ps_daemon_block_carries_the_degraded_redemption() -> None:
    from hyprial.daemon.impl.forwarding.membership.migration import (
        record_degraded_redemption,
    )
    from tests.daemon.application.reconcile.test_daemon_reconcile_resilience import (
        _app, _ipc_ps, _wire,
    )
    from tests.support.runtime._tmp import hyprial_test_tmp_root

    root = Path(tempfile.mkdtemp(prefix="hyprial-dr-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    _wire(app, root)
    record_degraded_redemption(app.hyprial_home, tailnet_id='space-1', invite_id='i1',
                               code='AUTHORITY_UNREACHABLE',
                               error='Tailnet membership command failed.')
    app._runtime = _QuietRuntime()
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        response = _ipc_ps(app.socket_path)
        degraded = response["result"]["daemon"]["degradedRedemption"]
        assert degraded["tailnetId"] == "space-1"
        assert degraded["code"] == "AUTHORITY_UNREACHABLE"
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        app.agents.close()
        shutil.rmtree(root, ignore_errors=True)


class _BrokenSupervisor:
    """A sidecar owner whose RPC fails with a plain (non-sidecar) exception."""

    def __init__(self) -> None:
        self.calls = 0

    def membership_request(self, op: str, params: dict) -> dict:
        self.calls += 1
        raise RuntimeError('decode failed')


def test_unexpected_retry_failure_rearms_the_bounded_backoff(node):
    from hyprial.daemon.impl.forwarding.membership.migration import (
        migrate_for_app, read_degraded_redemption,
    )
    node.sidecar.seed('space')
    created = node.sidecar.membership_request('invite-create', {
        'operationId': 'create-1', 'tailnetId': 'space', 'owner': OWNER,
        'expiresAtMs': node.sidecar.now_ms + 60000})
    seed_pending_redemption(node, 'space', created['invite'])
    node.sidecar.failures['invite-redeem'] = 'AUTHORITY_UNREACHABLE'
    migrate_for_app(node.app())
    force_retry_due(node)
    broken = _BrokenSupervisor()
    app = _RetryApp(node)
    app._forwarding_supervisor = broken
    app.retry()
    record = read_degraded_redemption(node.home)
    assert record['attempts'] == 2
    assert record['code'] == 'RuntimeError'
    assert record['error'] == 'decode failed'
    assert record['nextAttemptAtMs'] > 0
    failed = app.named('membership.redemption.retry-failed')
    assert failed and failed[0]['errorType'] == 'RuntimeError'
    assert failed[0]['attempts'] == 2
    assert failed[0]['nextRetryAtMs'] == record['nextAttemptAtMs']
    app.retry()
    assert broken.calls == 1
    assert read_degraded_redemption(node.home)['attempts'] == 2


def test_retry_stops_once_the_invite_is_permanently_refused(node):
    from hyprial.daemon.impl.forwarding.membership.migration import (
        migrate_for_app, read_degraded_redemption, receipt_path,
    )
    node.sidecar.seed('space')
    created = node.sidecar.membership_request('invite-create', {
        'operationId': 'create-1', 'tailnetId': 'space', 'owner': OWNER,
        'expiresAtMs': node.sidecar.now_ms + 60000})
    seed_pending_redemption(node, 'space', created['invite'])
    node.sidecar.failures['invite-redeem'] = 'AUTHORITY_UNREACHABLE'
    migrate_for_app(node.app())
    force_retry_due(node)
    node.sidecar.failures['invite-redeem'] = 'INVITE_EXPIRED'
    app = _RetryApp(node)
    app.retry()
    record = read_degraded_redemption(node.home)
    assert (record['code'], record['attempts']) == ('INVITE_EXPIRED', 2)
    assert record['retryable'] is False and record['nextAttemptAtMs'] is None
    assert app.named('membership.redemption.retry-failed')[0]['retryable'] is False
    redeems = sum(1 for call in node.sidecar.calls if call[0] == 'invite-redeem')
    del node.sidecar.failures['invite-redeem']
    app.retry()
    migrate_for_app(node.app())
    assert sum(1 for call in node.sidecar.calls if call[0] == 'invite-redeem') == redeems
    assert read_degraded_redemption(node.home) == record
    assert json.loads(receipt_path(node.home, 'space').read_text())['lastError']['code'] == 'INVITE_EXPIRED'
    held = degraded_events(node, 'membership.redemption.degraded')[-1]
    assert (held['code'], held['retryable'], held['replayed']) == ('INVITE_EXPIRED', False, False)
