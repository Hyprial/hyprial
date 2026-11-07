import pytest

from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError


def row(device='one', owner='alice'):
    return {'deviceId': device, 'owner': owner, 'serverPublic': 'nodekey:' + 'a'*64,
            'clientPublic': 'nodekey:' + 'b'*64, 'address': 'address',
            'keyGeneration': 1, 'updatedAt': '2026-10-27T00:00:00Z'}


def test_legacy_missing_derived_map_is_migration_only():
    from hyprial.daemon.impl.forwarding.membership.migration import parse_legacy_member
    from hyprial.identity import DirectoryDevice
    with pytest.raises(ValueError):
        DirectoryDevice.from_record(row())
    value = parse_legacy_member(row(), check_address=lambda *_: True)
    assert value['masterPublic'] == row()['clientPublic']
    assert value['clientPublicByServer'] == {}


def test_ambiguous_authority_requires_explicit_pin():
    from hyprial.daemon.impl.forwarding.membership.migration import select_authority
    rows = [dict(row(), masterPublic='m'), dict(row('two'), masterPublic='n')]
    with pytest.raises(ForwardingSidecarError) as exc:
        select_authority(rows, owner='alice', pin=None)
    assert exc.value.code == 'MIGRATION_AUTHORITY_MISSING'


def test_migration_receipt_prevents_reimport_after_store_loss(tmp_path):
    from hyprial.daemon.impl.forwarding.membership.migration import initialize_once
    calls = []
    def rpc(op, params):
        calls.append((op, params))
        return {'tailnetId': 'space', 'revision': 1, 'initialized': True}
    params = {'operationId': 'init', 'tailnetId': 'space', 'authority': {},
              'migration': {'source': 'directory-0.5.5', 'sourceDigest': 'digest', 'completed': True},
              'members': []}
    initialize_once(tmp_path, params, rpc, initialized=False)
    with pytest.raises(ForwardingSidecarError) as exc:
        initialize_once(tmp_path, params, rpc, initialized=False)
    assert exc.value.code == 'MEMBERSHIP_NOT_INITIALIZED'
    assert len(calls) == 1


def test_redemption_lost_reply_replays_exact_request(tmp_path):
    import json
    from hyprial.daemon.impl.forwarding.membership.migration import (
        redeem_once, initialize_once, receipt_path,
    )
    calls = []
    def rpc(op, params):
        if op == 'membership-status':
            return {'tailnets': []}
        calls.append((op, params))
        raise ForwardingSidecarError('lost response', code='AUTHORITY_UNREACHABLE')
    invite = {'tailnetId': 'space', 'inviteId': 'i1', 'authority': {}, 'token': 'secret'}
    with pytest.raises(ForwardingSidecarError):
        redeem_once(tmp_path, invite, rpc)
    receipt = json.loads(receipt_path(tmp_path, 'space').read_text())
    assert receipt['state'] == 'pending'
    initialize_once(tmp_path, receipt['params'], lambda op, params: calls.append((op, params)), initialized=False)
    assert calls[0] == calls[1]
    assert calls[1][0] == 'invite-redeem'
    assert json.loads(receipt_path(tmp_path, 'space').read_text())['state'] == 'complete'


def test_new_invite_after_completed_membership_redeems_again(tmp_path):
    from hyprial.daemon.impl.forwarding.membership.migration import redeem_once
    redemptions = []
    def rpc(op, params):
        if op == 'membership-status':
            return {'tailnets': [{'tailnetId': 'space'}] if redemptions else []}
        assert op == 'invite-redeem'
        redemptions.append(params)
        return {}
    invite = {'tailnetId': 'space', 'inviteId': 'one', 'authority': {}}
    redeem_once(tmp_path, invite, rpc)
    redeem_once(tmp_path, invite, rpc)
    assert len(redemptions) == 1
    redeem_once(tmp_path, invite | {'inviteId': 'two'}, rpc)
    assert [item['invite']['inviteId'] for item in redemptions] == ['one', 'two']


def pending_redemption(home, invite):
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path
    from hyprial.daemon.impl.forwarding.membership.settings import durable_json_write
    receipt = {'state': 'pending',
               'params': {'tailnetId': invite['tailnetId'], 'authority': invite['authority']},
               'redemption': {'operationId': 'redeem-' + invite['inviteId'], 'invite': invite},
               'diagnostics': {'source': 'invite-v2'}}
    durable_json_write(receipt_path(home, invite['tailnetId']), receipt)
    return receipt


def test_startup_stale_redemption_recovers_with_new_invite(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from hyprial.daemon.impl.forwarding.membership import migration

    monkeypatch.setenv('HYPRIAL_HOME', str(tmp_path))
    monkeypatch.setattr(migration, 'read_device_record', lambda home: object())
    old_invite = {'tailnetId': 'space', 'inviteId': 'old', 'authority': {}, 'token': 'old-secret'}
    new_invite = old_invite | {'inviteId': 'new', 'token': 'new-secret'}
    pending_redemption(tmp_path, old_invite)
    calls = []
    initialized = False

    def rpc(op, params):
        nonlocal initialized
        if op == 'membership-status':
            return {'tailnets': [{'tailnetId': 'space'}] if initialized else []}
        assert op == 'invite-redeem'
        calls.append(params)
        if params['invite']['inviteId'] == 'old':
            raise ForwardingSidecarError('Tailnet membership command failed.', code='INVITE_EXPIRED')
        assert params['invite'] == new_invite
        initialized = True
        return {}

    app = SimpleNamespace(hyprial_home=tmp_path,
                          _forwarding_supervisor=SimpleNamespace(membership_request=rpc))
    migration.migrate_for_app(app, plans=[])
    assert len(calls) == 1
    receipt = json.loads(migration.receipt_path(tmp_path, 'space').read_text())
    assert receipt['state'] == 'pending'
    assert receipt['lastError']['code'] == 'INVITE_EXPIRED'
    record = migration.read_degraded_redemption(tmp_path)
    assert record['code'] == 'INVITE_EXPIRED' and record['inviteId'] == 'old'
    assert 'stage=invite-redemption' in record['error'] and 'old-secret' not in record['error']
    assert record['retryable'] is False and record['nextAttemptAtMs'] is None
    print('startup_degraded: code=INVITE_EXPIRED; state=pending; redemption_calls=1')

    for attempt in (1, 2):
        migration.migrate_for_app(app, plans=[])
        migration.retry_degraded_redemption(app)
    assert len(calls) == 1
    assert migration.read_degraded_redemption(tmp_path) == record
    print('stale_invite_not_replayed: startups=2; retries=2; redemption_calls=1')

    migration.redeem_once(tmp_path, new_invite, rpc)
    written = json.loads(migration.receipt_path(tmp_path, 'space').read_text())['redemption']['invite']['inviteId']
    print(f'inviteId_written={written}')
    assert written == 'new'
    assert migration.read_degraded_redemption(tmp_path) is None
    for startup in (1, 2):
        before = len(calls)
        migration.migrate_for_app(app, plans=[])
        migration.retry_degraded_redemption(app)
        assert len(calls) - before == 0
        print(f'post_rejoin_startup_{startup}: redemption_calls_before={before}; after={len(calls)}; delta=0')
    assert [call['invite']['inviteId'] for call in calls] == ['old', 'new']
    receipt = json.loads(migration.receipt_path(tmp_path, 'space').read_text())
    assert receipt['state'] == 'complete'
    assert receipt['redemption'] == {'operationId': 'redeem-new', 'invite': new_invite}
    assert 'lastError' not in receipt
    print('fresh invite recovery: old -> new; receipt=complete; next startup does not redeem')


def test_failed_fresh_invite_after_stale_one_retries_with_backoff(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from hyprial.daemon.impl.forwarding.membership import migration

    monkeypatch.setattr(migration, 'read_device_record', lambda home: object())
    old_invite = {'tailnetId': 'space', 'inviteId': 'old', 'authority': {}}
    fresh = old_invite | {'inviteId': 'fresh'}
    pending_redemption(tmp_path, old_invite)
    calls = []
    reachable = False

    def rpc(op, params):
        if op == 'membership-status':
            return {'tailnets': []}
        calls.append(params['invite']['inviteId'])
        if params['invite']['inviteId'] == 'old':
            raise ForwardingSidecarError('expired', code='INVITE_EXPIRED')
        if not reachable:
            raise ForwardingSidecarError('unreachable', code='AUTHORITY_UNREACHABLE')
        return {}

    app = SimpleNamespace(hyprial_home=tmp_path,
                          _forwarding_supervisor=SimpleNamespace(membership_request=rpc))
    migration.migrate_for_app(app, plans=[])
    with pytest.raises(ForwardingSidecarError) as caught:
        migration.redeem_once(tmp_path, fresh, rpc)
    assert caught.value.code == 'AUTHORITY_UNREACHABLE'
    assert migration.read_degraded_redemption(tmp_path) is None
    migration.migrate_for_app(app, plans=[])
    record = migration.read_degraded_redemption(tmp_path)
    assert (record['inviteId'], record['code'], record['retryable']) == ('fresh', 'AUTHORITY_UNREACHABLE', True)
    assert record['nextAttemptAtMs'] > 0
    reachable = True
    record['nextAttemptAtMs'] = 0
    migration.atomic_json_write(migration.degraded_redemption_path(tmp_path), record)
    migration.retry_degraded_redemption(app)
    assert calls == ['old', 'fresh', 'fresh', 'fresh']
    assert json.loads(migration.receipt_path(tmp_path, 'space').read_text())['state'] == 'complete'
    assert migration.read_degraded_redemption(tmp_path) is None


def test_expired_recorded_redemption_names_stage_without_exposing_token(tmp_path):
    from hyprial.daemon.impl.forwarding.membership.migration import initialize_once
    receipt = pending_redemption(tmp_path, {'tailnetId': 'space', 'inviteId': 'old',
                                          'authority': {}, 'token': 'old-secret'})

    def rpc(op, params):
        raise ForwardingSidecarError('Tailnet membership command failed.', code='INVITE_EXPIRED',
                                     details={'operation': op})

    with pytest.raises(ForwardingSidecarError) as caught:
        initialize_once(tmp_path, receipt['params'], rpc, initialized=False)
    assert caught.value.code == 'INVITE_EXPIRED'
    assert 'stage=invite-redemption' in str(caught.value)
    assert 'recorded invite' in str(caught.value)
    assert 'fresh invite' in str(caught.value)
    assert caught.value.details == {'operation': 'invite-redeem'}
    assert 'old-secret' not in str(caught.value)


def test_same_pending_invite_keeps_exact_request_until_reply(tmp_path):
    import json
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path, redeem_once
    invite = {'tailnetId': 'space', 'inviteId': 'same', 'authority': {}, 'token': 'saved-secret'}
    receipt = pending_redemption(tmp_path, invite)
    receipt['redemption']['operationId'] = 'saved-operation'
    path = receipt_path(tmp_path, 'space')
    path.write_text(json.dumps(receipt))
    calls = []

    def rpc(op, params):
        if op == 'membership-status':
            return {'tailnets': [{'tailnetId': 'space'}]}
        calls.append((op, params))
        return {}

    redeem_once(tmp_path, invite | {'token': 'changed-token'}, rpc)
    assert calls == [('invite-redeem', receipt['redemption'])]
    assert json.loads(path.read_text())['state'] == 'complete'


def test_failed_replacement_persists_only_fresh_request_for_restart(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from hyprial.daemon.impl.forwarding.membership import migration
    monkeypatch.setenv('HYPRIAL_HOME', str(tmp_path))
    monkeypatch.setattr(migration, 'read_device_record', lambda home: object())
    invite = {'tailnetId': 'space', 'inviteId': 'old', 'authority': {}}
    pending_redemption(tmp_path, invite)
    fresh = invite | {'inviteId': 'fresh'}
    calls = []

    def lost_reply(op, params):
        if op == 'membership-status':
            return {'tailnets': []}
        calls.append((op, params))
        assert params['invite'] == fresh
        raise ForwardingSidecarError('lost reply', code='AUTHORITY_UNREACHABLE')

    with pytest.raises(ForwardingSidecarError) as caught:
        migration.redeem_once(tmp_path, fresh, lost_reply)
    assert caught.value.code == 'AUTHORITY_UNREACHABLE'
    receipt = json.loads(migration.receipt_path(tmp_path, 'space').read_text())
    assert receipt['state'] == 'pending' and receipt['redemption']['invite'] == fresh

    def recovered(op, params):
        if op == 'membership-status':
            return {'tailnets': [{'tailnetId': 'space'}]}
        calls.append((op, params))
        return {}

    app = SimpleNamespace(hyprial_home=tmp_path,
                          _forwarding_supervisor=SimpleNamespace(membership_request=recovered))
    migration.migrate_for_app(app, plans=[])
    assert calls == [('invite-redeem', receipt['redemption'])] * 2
    assert json.loads(migration.receipt_path(tmp_path, 'space').read_text())['state'] == 'complete'


def test_new_invite_does_not_reinitialize_completed_missing_store(tmp_path):
    import json
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path, redeem_once
    invite = {'tailnetId': 'space', 'inviteId': 'old', 'authority': {}}
    receipt = pending_redemption(tmp_path, invite)
    receipt['state'] = 'complete'
    path = receipt_path(tmp_path, 'space')
    path.write_text(json.dumps(receipt))
    before = path.read_bytes()

    def rpc(op, params):
        assert op == 'membership-status'
        return {'tailnets': []}

    with pytest.raises(ForwardingSidecarError) as caught:
        redeem_once(tmp_path, invite | {'inviteId': 'new'}, rpc)
    assert caught.value.code == 'MEMBERSHIP_NOT_INITIALIZED'
    assert path.read_bytes() == before
    print('negative control: complete + missing store -> MEMBERSHIP_NOT_INITIALIZED')


@pytest.mark.parametrize('state', ['pending', 'complete', 'skipped'])
@pytest.mark.parametrize('same_invite', [False, True])
def test_replacement_invite_preserves_recorded_authority(tmp_path, state, same_invite):
    import json
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path, redeem_once
    invite = {'tailnetId': 'space', 'inviteId': 'old', 'authority': {'deviceId': 'authority-a'}}
    receipt = pending_redemption(tmp_path, invite)
    receipt['state'] = state
    path = receipt_path(tmp_path, 'space')
    path.write_text(json.dumps(receipt))
    before = path.read_bytes()

    def rpc(op, params):
        assert op == 'membership-status'
        return {'tailnets': [{'tailnetId': 'space'}]}

    replacement = invite | {'inviteId': 'old' if same_invite else 'new',
                            'authority': {'deviceId': 'authority-b'}}
    with pytest.raises(ForwardingSidecarError) as caught:
        redeem_once(tmp_path, replacement, rpc)
    assert caught.value.code == 'AUTHORITY_PIN_MISMATCH'
    assert path.read_bytes() == before
    print(f'negative control: {state}, same_invite={same_invite}, changed authority -> AUTHORITY_PIN_MISMATCH')


def test_unknown_offline_directory_is_not_empty_and_can_retry(tmp_path, monkeypatch):
    from hyprial.daemon.impl.forwarding.membership.migration import migrate_for_app, receipt_path
    from tests.daemon.forwarding.membership.hardening.support import LOCAL_DEVICE, OWNER, LegacyHome, legacy_row

    class UnknownDirectoryError(Exception):
        code = 'unknown-doc'

    node = LegacyHome(tmp_path)
    try:
        space = node.seed_org('group-retry-directory', [legacy_row(OWNER, LOCAL_DEVICE)])
        listdir = node.fs.listdir
        error = UnknownDirectoryError('Device directory is not available locally.')

        def unavailable(space_id, path):
            if space_id == space and path == 'directory/devices':
                raise error
            return listdir(space_id, path)

        with monkeypatch.context() as patch:
            patch.setattr(node.fs, 'listdir', unavailable)
            with pytest.raises(UnknownDirectoryError) as caught:
                migrate_for_app(node.app())
            assert caught.value is error
        assert not receipt_path(node.home, space).exists()
        assert space not in node.sidecar.tailnets
        assert not node.events
        print('offline_unknown_directory: code=unknown-doc; receipt=absent; skipped_events=0')

        migrate_for_app(node.app())
        assert space in node.sidecar.tailnets
        assert receipt_path(node.home, space).exists()
        print('offline_directory_retry: readable; membership_initialized=true')
    finally:
        node.close()


def repinned_invite(home, *, state='complete', invite_id='fresh', has_pin=True):
    from hyprial.daemon.impl.forwarding.membership.settings import durable_json_write
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path
    old = {'tailnetId': 'space', 'inviteId': 'old', 'authority': {'deviceId': 'authority-a'}}
    fresh = old | {'inviteId': invite_id, 'authority': {'deviceId': 'authority-b'}}
    receipt = pending_redemption(home, old)
    receipt['state'] = state
    durable_json_write(receipt_path(home, 'space'), receipt)
    if has_pin:
        durable_json_write(home / 'settings.json', {
            'forwarding': {'tailnet': {'authorities': {'space': fresh['authority']}}},
        })
    return fresh


def test_completed_repin_accepts_fresh_invite_from_current_pin(tmp_path):
    import json
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path, redeem_once
    fresh = repinned_invite(tmp_path)
    calls = []

    def rpc(op, params):
        if op == 'membership-status':
            return {'tailnets': [{'tailnetId': 'space'}]}
        assert op == 'invite-redeem'
        calls.append(params)
        return {}

    redeem_once(tmp_path, fresh, rpc)
    assert calls == [{'operationId': 'redeem-fresh', 'invite': fresh}]
    saved = json.loads(receipt_path(tmp_path, 'space').read_text())
    assert saved['state'] == 'complete' and saved['params']['authority'] == fresh['authority']
    print('completed_repinned_rejoin: current_pin=authority-b; inviteId_written=fresh; state=complete')


@pytest.mark.parametrize('initialized,state,invite_id,has_pin,code', [
    (False, 'complete', 'fresh', True, 'MEMBERSHIP_NOT_INITIALIZED'),
    (True, 'pending', 'fresh', True, 'AUTHORITY_PIN_MISMATCH'),
    (True, 'complete', 'old', True, 'AUTHORITY_PIN_MISMATCH'),
    (True, 'complete', 'fresh', False, 'AUTHORITY_PIN_MISMATCH'),
], ids=['store-missing', 'receipt-pending', 'same-invite-id', 'pin-missing'])
def test_explicit_repin_rejoin_requires_each_condition(tmp_path, initialized, state, invite_id, has_pin, code):
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path, redeem_once
    fresh = repinned_invite(tmp_path, state=state, invite_id=invite_id, has_pin=has_pin)
    path = receipt_path(tmp_path, 'space')
    before = path.read_bytes()
    settings = tmp_path / 'settings.json'
    pin_before = settings.read_bytes() if settings.exists() else None
    calls = []

    def rpc(op, params):
        calls.append(op)
        return {'tailnets': [{'tailnetId': 'space'}] if initialized else []}

    with pytest.raises(ForwardingSidecarError) as caught:
        redeem_once(tmp_path, fresh, rpc)
    assert caught.value.code == code
    assert calls == ['membership-status']
    assert path.read_bytes() == before
    assert (settings.read_bytes() if settings.exists() else None) == pin_before
    print(f'repin_fence: initialized={initialized}; state={state}; inviteId={invite_id}; has_pin={has_pin}; code={code}')
