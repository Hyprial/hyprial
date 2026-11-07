"""Review regressions: real OrgFS/journal, explicit membership RPC fixture."""
import json
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.shell.impl.cli.commands.org import removal as cli
from tests.daemon.org.network.test_org_lifecycle import (
    BOB, _acl_member_users, _member_users, _service, home, runtime,
)
from tests.support.tailnet_membership import member

# Reuse the real-runtime fixtures, not a replacement storage implementation.
__all__ = ['home', 'runtime']
ORG = 'group-review'
USER = 'user:' + BOB


def setup_member(runtime, home):
    service = _service(runtime, home)
    created = service.create(ORG)
    service.invite(ORG, USER)
    table = runtime.membership_sidecar.tailnets[created['spaceId']]
    table['members']['b1'] = member(BOB, 'b1')
    return service, table


@pytest.mark.parametrize('casdoor_discovery', [False, True])
def test_acl_retry_after_directory_commit(runtime, home, monkeypatch, casdoor_discovery):
    service, _table = setup_member(runtime, home)
    if casdoor_discovery:
        preview = service.remove_from_casdoor(ORG, USER)
        assert preview['reason'] == 'casdoor-removal'
    preview = service.remove(ORG, USER)
    binding = {'operation_id': preview['operationId'], 'device_ids': preview['deviceIds']}
    original = service._remove_acl_member
    attempts = []

    def fail_acl_once(org, user):
        attempts.append(user)
        if len(attempts) == 1:
            raise OrgFsError('storage-unavailable', {'message': 'injected ACL write failure'})
        return original(org, user)

    monkeypatch.setattr(service, '_remove_acl_member', fail_acl_once)
    with pytest.raises(OrgFsError, match='storage-unavailable'):
        service.remove(ORG, USER, confirmed=True, **binding)
    assert USER not in _member_users(runtime, ORG)
    assert USER in _acl_member_users(runtime, ORG)
    assert not any(op == 'member-revoke' for op, _ in runtime.membership_sidecar.calls)
    pending = json.loads(next((home / 'tailnet/removals').glob('*.json')).read_text())
    assert pending['phase'] == 'orgfs-pending'
    assert 'upperLayerReceipt' not in pending
    result = _service(runtime, home).remove(ORG, USER, confirmed=True, **binding)
    assert USER not in _acl_member_users(runtime, ORG)
    assert result['phase'] == 'complete'
    assert result['upperLayerReceipt'] == 'committed'
    assert sum(op == 'member-revoke' for op, _ in runtime.membership_sidecar.calls) == 1


@pytest.mark.parametrize('yes', [False, True])
def test_confirmation_cannot_remove_reenrolled_device(runtime, home, monkeypatch, yes):
    _service0, table = setup_member(runtime, home)
    previews = []

    def interleave():
        other = _service(runtime, home).remove(
            ORG, USER, confirmed=True, operation_id=previews[0]['operationId'],
            device_ids=previews[0]['deviceIds'])
        assert other['phase'] == 'complete' and other['deviceIds'] == ['b1']
        _service(runtime, home).invite(ORG, USER)
        table['members']['b2'] = member(BOB, 'b2')
        table['revision'] += 1

    def request(method, params):
        assert method == 'org.remove'
        bindings = {key: params[wire] for key, wire in (
            ('operation_id', 'operationId'), ('device_ids', 'deviceIds')) if wire in params}
        result = _service(runtime, home).remove(
            params['org'], params['user'], confirmed=params['confirmed'], **bindings)
        if not params['confirmed']:
            previews.append(result)
            if yes:
                interleave()
        return result

    def confirm(_prompt):
        interleave()
        return True

    monkeypatch.setattr(cli, 'confirm', confirm)
    services = SimpleNamespace(
        runtime=SimpleNamespace(request=request),
        _stdin_isatty=lambda: True,
        CliError=RuntimeError,
    )
    try:
        result = cli.confirmed_removal(services, 'org.remove', {'org': ORG, 'user': USER}, yes=yes)
    except OrgNetworkError as error:
        assert error.code == 'REMOVAL_CONFIRMATION_STALE'
    else:
        assert result['deviceIds'] == previews[0]['deviceIds'] == ['b1']
        assert result['operationId'] == previews[0]['operationId']
    assert table['members']['b2']['state'] == 'active'
    assert USER in _member_users(runtime, ORG)
    assert USER in _acl_member_users(runtime, ORG)
    assert sum(op == 'member-revoke' for op, _ in runtime.membership_sidecar.calls) == 1


@pytest.mark.parametrize('binding', [
    {}, {'operationId': 'unknown', 'deviceIds': ['b1']},
    {'deviceIds': ['b1']}, {'operationId': 'preview'},
    {'operationId': 'preview', 'deviceIds': ['b2']},
    {'operationId': 'preview', 'deviceIds': 'b1'},
])
def test_ipc_refuses_unbound_confirmation_without_mutation(runtime, home, binding):
    from hyprial.daemon.impl.org.network.ipc import ipc_org_remove
    from hyprial.kernel import DaemonRequestError
    from tests.daemon.org.network.test_org_lifecycle import _IpcApp, OWNER
    _service0, table = setup_member(runtime, home)
    app = _IpcApp(runtime, home, owner=OWNER)
    params = {'org': ORG, 'user': USER}
    preview = ipc_org_remove(app, params)
    if binding.get('operationId') == 'preview':
        binding = {**binding, 'operationId': preview['operationId']}
    before = json.dumps(table, sort_keys=True)
    journal = next((home / 'tailnet/removals').glob('*.json'))
    journal_before = journal.read_bytes()
    with pytest.raises(DaemonRequestError) as error:
        ipc_org_remove(app, {**params, **binding, 'confirmed': True})
    assert error.value.code == 'REMOVAL_CONFIRMATION_STALE'
    assert 're-run preview' in str(error.value)
    assert json.dumps(table, sort_keys=True) == before
    assert journal.read_bytes() == journal_before
    assert USER in _member_users(runtime, ORG)
    assert USER in _acl_member_users(runtime, ORG)
    assert not any(op == 'member-revoke' for op, _ in runtime.membership_sidecar.calls)


@pytest.mark.parametrize('change', ['add', 'replace', 'identity'])
def test_changed_roster_requires_new_preview_before_upper_work(runtime, home, change):
    from tests.support.tailnet_membership import confirmation
    service, table = setup_member(runtime, home)
    preview = service.remove(ORG, USER)
    if change == 'replace':
        table['members']['b1']['state'] = 'tombstone'
    if change == 'identity':
        table['members']['b1']['masterPublic'] = member(BOB, 'replacement')['masterPublic']
    else:
        table['members']['b2'] = member(BOB, 'b2')
    table['revision'] += 1
    with pytest.raises(OrgNetworkError) as error:
        service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    assert error.value.code == 'REMOVAL_CONFIRMATION_STALE'
    assert USER in _member_users(runtime, ORG)
    assert USER in _acl_member_users(runtime, ORG)
    assert not any(op == 'member-revoke' for op, _ in runtime.membership_sidecar.calls)
    fresh = service.remove(ORG, USER)
    assert fresh['operationId'] != preview['operationId']
    with pytest.raises(OrgNetworkError) as error:
        service.remove(ORG, USER, confirmed=True, **confirmation(preview))
    assert error.value.code == 'REMOVAL_CONFIRMATION_STALE'
    done = service.remove(ORG, USER, confirmed=True, **confirmation(fresh))
    assert done['phase'] == 'complete'
    assert done['operationId'] == fresh['operationId']
    assert done['deviceIds'] == fresh['deviceIds']


def test_confirmation_without_preview_cannot_create_operation(runtime, home):
    service, _table = setup_member(runtime, home)
    with pytest.raises(OrgNetworkError) as error:
        service.remove(ORG, USER, confirmed=True, operation_id='unknown', device_ids=['b1'])
    assert error.value.code == 'REMOVAL_CONFIRMATION_STALE'
    assert not list((home / 'tailnet/removals').glob('*.json'))
    assert USER in _member_users(runtime, ORG)
    assert USER in _acl_member_users(runtime, ORG)


def test_completed_leave_confirmation_cannot_hide_rejoined_org(runtime, home):
    from hyprial.daemon.impl.org.network.state import clear_left_org, read_left_orgs
    from tests.daemon.org.network.test_org_lifecycle import _as_bob
    from tests.support.tailnet_membership import confirmation
    _owner, table = setup_member(runtime, home)
    bob = _service(runtime, home, owner=BOB, node_id='b1')
    with _as_bob(runtime):
        preview = bob.leave(ORG)
        done = bob.leave(ORG, confirmed=True, **confirmation(preview))
        assert done['phase'] == 'complete'
        assert read_left_orgs(home) == [ORG]
        # Model valid re-enrollment of the local device after the other terminal
        # completed its leave. The old terminal still holds its first preview.
        clear_left_org(home, ORG)
        table['members']['b1'] = member(BOB, 'b1')
        table['revision'] += 1
        try:
            bob.leave(ORG, confirmed=True, **confirmation(preview))
        except OrgNetworkError as error:
            assert error.code == 'REMOVAL_CONFIRMATION_STALE'
    assert read_left_orgs(home) == []
    assert table['members']['b1']['state'] == 'active'


def test_cli_completed_preview_returns_receipt_without_confirmation():
    calls = []
    receipt = {'phase': 'complete', 'operationId': 'finished', 'deviceIds': ['b1']}

    def request(method, params):
        calls.append(params)
        return receipt

    services = SimpleNamespace(
        runtime=SimpleNamespace(request=request),
        _stdin_isatty=lambda: False,
        CliError=RuntimeError,
    )
    assert cli.confirmed_removal(services, 'org.leave', {'org': ORG}, yes=True) == receipt
    assert calls == [{'org': ORG, 'confirmed': False}]
