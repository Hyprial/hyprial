import pytest
from tests.support.tailnet_membership import confirmation, confirm_removal

from hyprial.identity import InviteError, decode_invite


@pytest.fixture
def conflicted_removal(tmp_path):
    import copy
    from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
    from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
    def member(device):
        return {'owner': 'bob', 'deviceId': device, 'state': 'active',
                'masterPublic': 'master-' + device, 'serverPublic': 'server-' + device}
    table = {'tailnetId': 's', 'revision': 1, 'role': 'authority',
             'authorityDeviceId': 'local', 'members': [member('b1'), member('b2')]}
    requests, upper_calls = [], []
    def rpc(op, params):
        if op in ('membership-status', 'roster-fetch'):
            return {'tailnets': [copy.deepcopy(table)]}
        requests.append(copy.deepcopy(params))
        if params['expectedRevision'] != table['revision']:
            raise ForwardingSidecarError('stale', code='REVISION_CONFLICT')
        return {'revision': table['revision'] + 1, 'propagation': 'lease-bounded'}
    journal = RemovalJournal(tmp_path, rpc, local_device='local')
    def upper():
        upper_calls.append('committed')
    preview = journal.remove('s', 'bob', confirmed=False, upper=upper)
    table['revision'] = 2
    with pytest.raises(ForwardingSidecarError) as caught:
        journal.remove('s', 'bob', confirmed=True, upper=upper, **confirmation(preview))
    assert caught.value.code == 'REVISION_CONFLICT'
    return journal, table, requests, upper_calls, upper, preview, member


def test_conflict_reconfirmation_keeps_id_targets_and_upper_receipt(conflicted_removal):
    journal, table, requests, calls, upper, initial, member = conflicted_removal
    table['members'].append(member('new-unselected'))
    preview = journal.remove('s', 'bob', confirmed=False, upper=upper)
    assert preview['phase'] == 'revision-confirmation'
    assert preview['revisionRefresh']['newRevision'] == 2
    assert preview['revisionRefresh']['unselectedDeviceIds'] == ['new-unselected']
    assert len(requests) == 1
    result = journal.remove('s', 'bob', confirmed=True, upper=upper, **confirmation(preview))
    assert result['phase'] == 'complete'
    assert result['operationId'] == initial['operationId']
    assert result['deviceIds'] == requests[-1]['deviceIds'] == ['b1', 'b2']
    assert requests[-1]['expectedRevision'] == 2
    assert calls == ['committed']
    audit = result['revisionAudit'][-1]
    assert (audit['oldRevision'], audit['newRevision'], audit['operatorDeviceId']) == (1, 2, 'local')
    assert audit['confirmedAtMs'] > 0


@pytest.mark.parametrize('removed', [['b1'], ['b1', 'b2']])
def test_conflict_skips_targets_already_removed(conflicted_removal, removed):
    journal, table, requests, calls, upper, initial, _member = conflicted_removal
    for row in table['members']:
        if row['deviceId'] in removed:
            row['state'] = 'tombstone'
    preview = journal.remove('s', 'bob', confirmed=False, upper=upper)
    assert preview['revisionRefresh']['alreadyRemovedDeviceIds'] == removed
    result = journal.remove('s', 'bob', confirmed=True, upper=upper, **confirmation(preview))
    assert result['phase'] == 'complete' and result['deviceIds'] == initial['deviceIds']
    if len(removed) == 2:
        assert len(requests) == 1 and result['tailnetUnchanged']
    else:
        assert requests[-1]['deviceIds'] == ['b2']
    assert calls == ['committed']


@pytest.mark.parametrize('change_device_id', [False, True, 'server-rotation'])
def test_conflict_refuses_replacement_incarnation(conflicted_removal, change_device_id):
    from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
    journal, table, requests, calls, upper, _initial, _member = conflicted_removal
    if change_device_id:
        table['members'][0]['deviceId'] = 'replacement'
        if change_device_id == 'server-rotation':
            table['members'][0]['serverPublic'] = 'new-server'
    else:
        table['members'][0]['masterPublic'] = 'new-master'
    with pytest.raises(ForwardingSidecarError) as caught:
        journal.remove('s', 'bob', confirmed=False, upper=upper)
    assert caught.value.code == 'MEMBER_CONFLICT'
    assert len(requests) == 1 and calls == ['committed']


def test_confirmed_retry_cannot_bypass_new_conflict_preview(conflicted_removal):
    journal, _table, requests, calls, upper, initial, _member = conflicted_removal
    result = journal.remove('s', 'bob', confirmed=True, upper=upper, **confirmation(initial))
    assert result['phase'] == 'revision-confirmation'
    assert len(requests) == 1 and calls == ['committed']


@pytest.mark.parametrize('missing', [
    'targetIdentityDigests', 'targetKeyDigests', 'revisionConflictProof'])
def test_conflict_requires_complete_original_evidence(conflicted_removal, missing):
    import json
    from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
    journal, _table, requests, calls, upper, initial, _member = conflicted_removal
    path = next(journal.root.glob('*.json'))
    operation = json.loads(path.read_text())
    operation.pop(missing)
    path.write_text(json.dumps(operation))
    with pytest.raises(ForwardingSidecarError) as caught:
        journal.remove('s', 'bob', confirmed=True, upper=upper, **confirmation(initial))
    assert caught.value.code == 'OPERATION_CONFLICT'
    assert len(requests) == 1 and calls == ['committed']


def test_v1_invite_refused_with_reissue_code():
    with pytest.raises(InviteError) as error:
        decode_invite('hyprial-invite:v1:e30')
    assert error.value.code == 'INVITE_VERSION_UNSUPPORTED'


def test_unconfirmed_discovery_does_not_remove_anything(tmp_path):
    from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
    calls = []
    def rpc(op, params):
        assert op == 'membership-status'
        return {'tailnets': [{'tailnetId': 's', 'revision': 1, 'role': 'authority',
            'authorityDeviceId': 'local', 'members': [
                {'owner': 'bob', 'deviceId': 'b1', 'state': 'active'},
                {'owner': 'bob', 'deviceId': 'b2', 'state': 'active'}]}]}
    journal = RemovalJournal(tmp_path, rpc, local_device='local')
    result = journal.remove('s', 'bob', confirmed=False, upper=lambda: calls.append('orgfs'))
    assert result['phase'] == 'pending-confirmation'
    assert result['deviceIds'] == ['b1', 'b2']
    assert calls == []


def test_removal_retries_fixed_request_after_lost_reply(tmp_path):
    from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
    calls, revokes = [], []
    def rpc(op, params):
        if op == 'membership-status':
            return {'tailnets': [{'tailnetId': 's', 'revision': 2, 'role': 'authority',
                'authorityDeviceId': 'local', 'members': [
                    {'owner': 'bob', 'deviceId': 'b', 'state': 'active'}]}]}
        revokes.append(params)
        if len(revokes) == 1:
            raise TimeoutError('lost reply')
        return {'revision': 3, 'backupPath': '/private/backup', 'propagation': 'lease-bounded'}
    journal = RemovalJournal(tmp_path, rpc, local_device='local')
    preview = journal.remove('s', 'bob', confirmed=False, upper=lambda: calls.append('orgfs'))
    with pytest.raises(TimeoutError):
        journal.remove('s', 'bob', confirmed=True, upper=lambda: calls.append('orgfs'), **confirmation(preview))
    result = journal.remove('s', 'bob', confirmed=True, upper=lambda: calls.append('orgfs'), **confirmation(preview))
    assert result['phase'] == 'complete'
    assert calls == ['orgfs']
    assert revokes[0] == revokes[1]
    assert revokes[0]['deviceIds'] == ['b']


def test_orgfs_failure_prevents_revoke(tmp_path):
    from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
    calls = []
    def rpc(op, params):
        calls.append(op)
        return {'tailnets': [{'tailnetId': 's', 'revision': 1, 'role': 'authority',
            'authorityDeviceId': 'local', 'members': [
                {'owner': 'bob', 'deviceId': 'b', 'state': 'active'}]}]}
    journal = RemovalJournal(tmp_path, rpc, local_device='local')
    def fail():
        raise ValueError('orgfs failed')
    with pytest.raises(ValueError):
        confirm_removal(journal.remove, 's', 'bob', upper=fail)
    assert calls == ['membership-status', 'membership-status']


def test_empty_target_removal_completes_without_invalid_empty_revoke(tmp_path):
    from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
    upper = []
    def rpc(op, params):
        assert op == 'membership-status', 'empty member-revoke is BAD_REQUEST under v6'
        return {'tailnets': [{'tailnetId': 's', 'revision': 3, 'role': 'authority',
            'authorityDeviceId': 'local', 'members': []}]}
    result = confirm_removal(RemovalJournal(tmp_path, rpc, local_device='local').remove,
        's', 'bob', upper=lambda: upper.append('committed'))
    assert result['phase'] == 'complete' and result['tailnetUnchanged'] is True
    assert upper == ['committed']


def test_nonauthority_discovery_records_pending_authority_without_confirmation(tmp_path):
    from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
    def rpc(op, params):
        assert op == 'membership-status'
        return {'tailnets': [{'tailnetId': 's', 'revision': 3, 'role': 'member',
            'authorityDeviceId': 'authority', 'members': [
                {'owner': 'bob', 'deviceId': 'b', 'state': 'active'}]}]}
    def forbidden():
        pytest.fail('unconfirmed discovery mutated OrgFS')
    result = RemovalJournal(tmp_path, rpc, local_device='local').remove(
        's', 'bob', confirmed=False, upper=forbidden)
    assert result['phase'] == 'pending-authority'
    assert result['confirmed'] is False
