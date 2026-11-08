"""Device selection with real OrgFS/journal and an explicit sidecar double.

SQLiteGroups proves durable group writes across client reconstruction. It is
an isolated Casdoor protocol adapter, not acceptance of a deployed Casdoor.
"""
# ruff: noqa: F811
import json
import sqlite3
from contextlib import closing

import pytest

from hyprial.daemon.impl.org.network.service import OrgNetworkError
from tests.daemon.org.network.test_org_lifecycle import (
    BOB, OWNER, _acl_member_users, _member_users, _service, home, runtime,
)
from tests.support.tailnet_membership import confirmation, member

__all__ = ['home', 'runtime']
ORG = 'group-devices'
USER = 'user:' + BOB
GROUP = 'hyprial/' + ORG


class SQLiteGroups:
    def __init__(self, path):
        self.path = path
        self.deletes = []
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS members (group_id, user_id, PRIMARY KEY (group_id, user_id))')

    def member_state(self, group, user):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            row = connection.execute('SELECT 1 FROM members WHERE group_id=? AND user_id=?', (group, user)).fetchone()
        return 'member' if row else 'not-member'

    def require_member(self, group, user):
        assert self.member_state(group, user) == 'member'

    def add_member(self, group, user):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return bool(connection.execute('INSERT OR IGNORE INTO members VALUES (?, ?)', (group, user)).rowcount)

    def remove_member(self, group, user):
        self.deletes.append(('DELETE', group, user))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return bool(connection.execute('DELETE FROM members WHERE group_id=? AND user_id=?', (group, user)).rowcount)


def setup(runtime, home, devices=('b1', 'b2')):
    groups = SQLiteGroups(home / 'groups.sqlite')
    groups.add_member(GROUP, 'hyprial/' + OWNER)
    service = _service(runtime, home, casdoor=groups)
    created = service.create(ORG)
    service.invite(ORG, USER)
    table = runtime.membership_sidecar.tailnets[created['spaceId']]
    for device in devices:
        table['members'][device] = member(BOB, device)
    return service, table, groups


def assert_member(runtime, home, present=True):
    assert (USER in _member_users(runtime, ORG)) is present
    assert (USER in _acl_member_users(runtime, ORG)) is present
    assert SQLiteGroups(home / 'groups.sqlite').member_state(GROUP, 'hyprial/' + BOB) == (
        'member' if present else 'not-member')


def test_selected_device_preserves_durable_group_directory_and_acl(runtime, home):
    service, table, _groups = setup(runtime, home)
    preview = service.remove(ORG, USER, device_ids=['b1'])
    assert preview['deviceIds'] == ['b1']
    assert preview['membershipAction'] == 'preserved'
    assert preview['remainingDeviceIds'] == ['b2']
    assert_member(runtime, home)
    service = _service(runtime, home, casdoor=SQLiteGroups(home / 'groups.sqlite'))
    result = service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    assert result['phase'] == 'complete'
    assert result['casdoorGroup'] == 'retained in ' + GROUP
    assert result['membershipAction'] == 'preserved'
    assert table['members']['b1']['state'] == 'tombstone'
    assert table['members']['b2']['state'] == 'active'
    assert_member(runtime, home)
    journal = json.loads(next((home / 'tailnet/removals').glob('*.json')).read_text())
    assert journal['deviceIds'] == ['b1']
    assert journal['selectedDeviceIds'] == ['b1']


@pytest.mark.parametrize('devices,selection,code', [
    (('b1', 'b2'), ['absent'], 'DEVICE_NOT_FOUND'),
    (('b1', 'b2'), ['b1', 'absent'], 'DEVICE_NOT_FOUND'),
    (('b1',), ['b1'], 'LAST_DEVICE_REFUSED'),
    (('b1', 'b2'), ['b1', 'b2'], 'LAST_DEVICE_REFUSED'),
    (('b1', 'b2'), [], 'INVALID_ARGUMENT'),
    (('b1', 'b2'), 'b1', 'INVALID_ARGUMENT'),
    (('b1', 'b2'), [''], 'INVALID_ARGUMENT'),
    (('b1', 'b2'), [1], 'INVALID_ARGUMENT'),
])
def test_invalid_selection_has_no_membership_or_journal_mutation(runtime, home, devices, selection, code):
    service, table, _groups = setup(runtime, home, devices)
    before = json.dumps(table, sort_keys=True)
    with pytest.raises(OrgNetworkError) as refused:
        service.remove(ORG, USER, device_ids=selection)
    assert refused.value.code == code
    print(json.dumps({'selection': selection, 'code': refused.value.code, 'message': str(refused.value)}))
    if code == 'LAST_DEVICE_REFUSED':
        assert 'last active device' in str(refused.value)
    assert json.dumps(table, sort_keys=True) == before
    assert_member(runtime, home)
    assert not list((home / 'tailnet/removals').glob('*.json'))


def test_selected_device_must_be_active_and_owned_by_user(runtime, home):
    service, table, _groups = setup(runtime, home)
    table['members']['b1']['state'] = 'tombstone'
    for device in ('b1', 'node-a'):
        with pytest.raises(OrgNetworkError) as refused:
            service.remove(ORG, USER, device_ids=[device])
        assert refused.value.code == 'DEVICE_NOT_FOUND'
    assert_member(runtime, home)


def test_last_device_is_rechecked_at_confirmation(runtime, home):
    service, table, _groups = setup(runtime, home)
    preview = service.remove(ORG, USER, device_ids=['b1'])
    table['members']['b2']['state'] = 'tombstone'
    table['revision'] += 1
    with pytest.raises(OrgNetworkError) as refused:
        service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    assert refused.value.code == 'LAST_DEVICE_REFUSED'
    assert table['members']['b1']['state'] == 'active'
    assert_member(runtime, home)
    assert not any(op == 'member-revoke' for op, _ in runtime.membership_sidecar.calls)


def test_selection_change_supersedes_confirmation(runtime, home):
    service, table, _groups = setup(runtime, home)
    first = service.remove(ORG, USER, device_ids=['b1'])
    second = service.remove(ORG, USER, device_ids=['b2'])
    assert first['operationId'] != second['operationId']
    with pytest.raises(OrgNetworkError) as refused:
        service.remove(ORG, USER, confirmed=True, **confirmation(first))
    assert refused.value.code == 'REMOVAL_CONFIRMATION_STALE'
    assert table['members']['b1']['state'] == table['members']['b2']['state'] == 'active'
    assert_member(runtime, home)


def test_default_still_removes_all_devices_and_memberships(runtime, home):
    service, table, _groups = setup(runtime, home)
    preview = service.remove(ORG, USER)
    assert preview['deviceIds'] == ['b1', 'b2']
    assert 'selectedDeviceIds' not in preview
    assert 'membershipAction' not in preview
    result = service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    assert result['casdoorGroup'] == 'removed from ' + GROUP
    assert 'membershipAction' not in result
    assert table['members']['b1']['state'] == table['members']['b2']['state'] == 'tombstone'
    assert_member(runtime, home, False)


def test_multiple_devices_are_sorted_and_deduplicated(runtime, home):
    service, table, _groups = setup(runtime, home, ('b1', 'b2', 'b3'))
    preview = service.remove(ORG, USER, device_ids=['b2', 'b1', 'b2'])
    assert preview['deviceIds'] == ['b1', 'b2']
    result = service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    assert result['remainingDeviceIds'] == ['b3']
    assert table['members']['b3']['state'] == 'active'
    assert_member(runtime, home)


def test_retry_after_unavailable_revoke_preserves_selection(runtime, home):
    service, table, groups = setup(runtime, home)
    preview = service.remove(ORG, USER, device_ids=['b1'])
    runtime.membership_sidecar.failures['member-revoke'] = 'AUTHORITY_UNREACHABLE'
    with pytest.raises(OrgNetworkError):
        service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    with pytest.raises(OrgNetworkError) as refused:
        service.remove(ORG, USER)
    assert refused.value.code == 'REMOVAL_CONFIRMATION_STALE'
    del runtime.membership_sidecar.failures['member-revoke']
    retried = _service(runtime, home, casdoor=groups).remove(
        ORG, USER, confirmed=True, **confirmation(preview))
    assert retried['deviceIds'] == ['b1']
    assert table['members']['b2']['state'] == 'active'
    assert_member(runtime, home)


def test_preview_recovers_a_committed_revoke_whose_reply_was_lost(runtime, home):
    from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
    service, table, groups = setup(runtime, home)
    preview = service.remove(ORG, USER, device_ids=['b1'])
    original = service._membership_request

    def lose_reply(op, params):
        result = original(op, params)
        if op == 'member-revoke':
            raise ForwardingSidecarError('reply lost after commit', code='AUTHORITY_UNREACHABLE')
        return result

    service._membership_request = lose_reply
    with pytest.raises(OrgNetworkError) as lost:
        service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    assert lost.value.code == 'AUTHORITY_UNREACHABLE'
    assert table['members']['b1']['state'] == 'tombstone'
    resumed = _service(runtime, home, casdoor=groups).remove(ORG, USER, device_ids=['b1'])
    assert resumed['phase'] == 'complete'
    assert resumed['operationId'] == preview['operationId']
    assert resumed['deviceIds'] == ['b1']
    assert len(runtime.membership_sidecar.backups) == 1
    assert groups.deletes == []
    assert table['members']['b2']['state'] == 'active'
    assert_member(runtime, home)


def test_revision_conflict_reconfirmation_never_removes_memberships(runtime, home):
    service, table, _groups = setup(runtime, home)
    preview = service.remove(ORG, USER, device_ids=['b1'])
    table['revision'] += 1
    with pytest.raises(OrgNetworkError) as refused:
        service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    assert refused.value.code == 'REVISION_CONFLICT'
    refreshed = service.remove(ORG, USER, device_ids=['b1'])
    assert refreshed['phase'] == 'revision-confirmation'
    done = service.remove(ORG, USER, confirmed=True, **confirmation(refreshed))
    assert done['deviceIds'] == ['b1']
    assert done['upperLayerReceipt'] == 'preserved'
    assert_member(runtime, home)


@pytest.mark.parametrize('yes', [False, True])
def test_cli_selected_removal_requires_yes_and_keeps_memberships(runtime, home, monkeypatch, yes):
    from typer.testing import CliRunner
    from tests.shell.cli.org.test_cli_org import _cli_root_app, _cli_support
    service, table, _groups = setup(runtime, home)
    calls = []

    def request(method, params, **_kwargs):
        assert method == 'org.remove'
        calls.append(params)
        return service.remove(params['org'], params['user'], confirmed=params['confirmed'],
                              device_ids=params.get('deviceIds'), operation_id=params.get('operationId'))

    monkeypatch.setattr(_cli_support, '_daemon_request', request)
    args = ['org', 'remove', ORG, USER, '--device', 'b1', '--json']
    result = CliRunner().invoke(_cli_root_app, args + (['--yes'] if yes else []))
    payload = json.loads(result.stdout)
    if yes:
        assert result.exit_code == 0, result.output
        assert payload['deviceIds'] == ['b1']
        assert payload['membershipAction'] == 'preserved'
        assert calls[0] == {'org': ORG, 'user': USER, 'deviceIds': ['b1'], 'confirmed': False}
        assert calls[1]['deviceIds'] == ['b1']
        assert calls[1]['operationId'] == payload['operationId']
        assert table['members']['b1']['state'] == 'tombstone'
    else:
        assert result.exit_code == 1, result.output
        assert payload['code'] == 'CONFIRMATION_REQUIRED'
        assert calls == []
        assert table['members']['b1']['state'] == 'active'
    assert_member(runtime, home)


def test_help_explains_selected_and_default_removal():
    from typer.testing import CliRunner
    from tests.shell.cli.org.test_cli_org import _cli_root_app
    result = CliRunner().invoke(_cli_root_app, ['org', 'remove', '--help'], color=False,
                                env={'COLUMNS': '180', 'TERM': 'dumb'})
    assert result.exit_code == 0
    plain = ' '.join(result.stdout.split())
    for text in ('--device', 'repeatable', 'retain user memberships', '--yes', 'last active device', 'Without this option'):
        assert text in plain


@pytest.mark.parametrize('selected', [True, False])
def test_membership_action_trace_positive_and_negative_controls(runtime, home, monkeypatch, selected):
    service, _table, groups = setup(runtime, home)
    writes = []
    original = service._remove_orgfs_member

    def recording_remove(org, space_id, user):
        writes.append((org, space_id, user))
        return original(org, space_id, user)

    monkeypatch.setattr(service, '_remove_orgfs_member', recording_remove)
    before = groups.member_state(GROUP, 'hyprial/' + BOB)
    preview = service.remove(ORG, USER, **({'device_ids': ['b1']} if selected else {}))
    assert groups.deletes == writes == []
    result = service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    after = SQLiteGroups(home / 'groups.sqlite').member_state(GROUP, 'hyprial/' + BOB)
    if selected:
        assert groups.deletes == []
        assert writes == []
        assert before == after == 'member'
        assert_member(runtime, home)
    else:
        assert groups.deletes == [('DELETE', GROUP, 'hyprial/' + BOB)]
        assert writes == [(ORG, result['spaceId'], USER)]
        assert before == 'member' and after == 'not-member'
        assert_member(runtime, home, False)
    print(json.dumps({'selected': selected, 'targets': result['deviceIds'],
                      'casdoorDeleteCalls': groups.deletes, 'orgfsRemoveCalls': writes,
                      'groupBefore': before, 'groupAfter': after}))


def test_ipc_preview_and_confirmation_select_only_requested_devices(runtime, home):
    from hyprial.daemon.impl.org.network.ipc import ipc_org_remove
    from tests.daemon.org.network.test_org_lifecycle import _IpcApp
    _service0, table, _groups = setup(runtime, home)
    app = _IpcApp(runtime, home, owner=OWNER)
    preview = ipc_org_remove(app, {'org': ORG, 'user': USER, 'deviceIds': ['b1']})
    assert preview['deviceIds'] == ['b1']
    done = ipc_org_remove(app, {'org': ORG, 'user': USER, 'confirmed': True,
                                'operationId': preview['operationId'], 'deviceIds': preview['deviceIds']})
    assert done['phase'] == 'complete'
    assert table['members']['b1']['state'] == 'tombstone'
    assert table['members']['b2']['state'] == 'active'
    assert_member(runtime, home)


def test_orgfs_and_journal_reopen_preserve_user_memberships(runtime, home):
    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
    service, _table, _groups = setup(runtime, home)
    preview = service.remove(ORG, USER, device_ids=['b1'])
    done = service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    sidecar = runtime.membership_sidecar
    runtime.close()
    reopened = OrgFsRuntime(home / 'state', node_id='node-a', author='user:' + OWNER)
    reopened.membership_sidecar = sidecar
    try:
        assert_member(reopened, home)
        service = _service(reopened, home, casdoor=SQLiteGroups(home / 'groups.sqlite'))
        with pytest.raises(OrgNetworkError) as refused:
            service.remove(ORG, USER, confirmed=True, **confirmation(done))
        assert refused.value.code == 'REMOVAL_CONFIRMATION_STALE'
        full = service.remove(ORG, USER)
        assert full['deviceIds'] == ['b2']
        service.remove(ORG, USER, confirmed=True, **confirmation(full))
        assert_member(reopened, home, False)
    finally:
        reopened.close()
