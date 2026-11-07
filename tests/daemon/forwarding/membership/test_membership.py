"""Tailcat v6: Python must not reconstruct admission from OrgFS."""
import threading

import pytest

from hyprial.daemon.impl.forwarding.controller import ForwardingSidecarController
from hyprial.daemon.impl.forwarding.policy import FORWARD_PROTOCOL_VERSION


def test_protocol_is_v6():
    assert FORWARD_PROTOCOL_VERSION == 6


def test_membership_rpc_is_correlated_and_preserves_error_code():
    controller = object.__new__(ForwardingSidecarController)
    controller._write_lock = threading.Lock()
    sent = []
    controller._send = sent.append
    def reply(match, *, deadline):
        request = sent[-1]
        assert request['v'] == 6
        assert set(request) == {'v', 'op', 'requestId', 'params'}
        assert not match({'requestId': 'other'})
        response = {'v': 6, 'event': 'error', 'requestId': request['requestId'],
                    'op': request['op'], 'code': 'AUTHORITY_UNREACHABLE',
                    'message': 'Authority is unreachable.'}
        assert match(response)
        return response
    controller._await_response = reply
    with pytest.raises(Exception) as raised:
        controller.membership_request('roster-fetch', {'tailnetId': 'space'})
    assert raised.value.code == 'AUTHORITY_UNREACHABLE'


@pytest.mark.parametrize('valid', [True, False])
def test_membership_transport_diagnostics_are_allowlisted(valid):
    from hyprial.daemon.impl.org.network.ipc import _translate
    controller = object.__new__(ForwardingSidecarController)
    controller._write_lock = threading.Lock()
    sent = []
    controller._send = sent.append
    detail = {'stage': 'derp', 'cause': 'proxy-rejected', 'proxy': 'configured'}
    if not valid:
        detail['cause'] = 'http://fake-user:fake-proxy-secret@proxy.invalid'
    controller._await_response = lambda *args, **kwargs: {
        'v': 6, 'event': 'error', 'requestId': sent[-1]['requestId'],
        'op': sent[-1]['op'], 'code': 'AUTHORITY_UNREACHABLE',
        'message': 'fake-proxy-secret', 'data': detail,
    }
    with pytest.raises(Exception) as raised:
        controller.membership_request('invite-redeem', {})
    error = raised.value
    assert 'fake-proxy-secret' not in str(error)
    if valid:
        assert error.details == detail
        assert 'proxy-rejected' in str(error)
        assert _translate(error).data == detail
    else:
        assert error.details == {}


@pytest.mark.parametrize('attempts', [
    [{'node': 'relay.invalid:443', 'cause': 'proxy-rejected'}],
    [{'node': '[::1]:443', 'cause': 'deadline-exceeded'}],
    [{'node': 'hung.invalid:443', 'cause': 'deadline-exceeded',
      'source': 'diagnostic-probe'}],
])
def test_per_node_diagnostics_are_preserved(attempts):
    from hyprial.daemon.impl.forwarding.membership.wire import (
        diagnostic_message, transport_diagnostic,
    )
    detail = {'stage': 'derp', 'cause': 'proxy-rejected', 'proxy': 'configured',
              'attempts': attempts}
    assert transport_diagnostic(detail) == detail
    assert attempts[0]['node'] in diagnostic_message(detail)
    assert attempts[0]['cause'] in diagnostic_message(detail)


@pytest.mark.parametrize('attempt', [
    {'node': 'http://user:fake-secret@relay.invalid:443', 'cause': 'proxy-rejected'},
    {'node': 'relay.invalid:443', 'cause': 'fake-secret'},
    {'node': 'relay.invalid:443', 'cause': 'proxy-rejected', 'source': 'fake-secret'},
    {'node': 'relay.invalid:443', 'cause': 'proxy-rejected', 'secret': 'fake-secret'},
    {'node': 'relay.invalid:99999', 'cause': 'proxy-rejected'},
    {'node': 'relay.invalid:443\nsecret', 'cause': 'proxy-rejected'},
    {'node': None, 'cause': 'proxy-rejected'},
    'fake-secret',
])
def test_unsafe_per_node_diagnostics_are_rejected(attempt):
    from hyprial.daemon.impl.forwarding.membership.wire import transport_diagnostic
    detail = {'stage': 'derp', 'cause': 'proxy-rejected', 'proxy': 'configured',
              'attempts': [attempt]}
    assert transport_diagnostic(detail) is None


def test_candidates_use_only_leased_active_roster_and_key_order():
    from hyprial.daemon.impl.forwarding.membership.projection import candidates
    rows = [
        {'owner': 'a', 'deviceId': 'a', 'masterPublic': 'a', 'address': 'addr-a', 'state': 'active'},
        {'owner': 'z', 'deviceId': 'z', 'masterPublic': 'z', 'address': 'addr-z', 'state': 'active'},
        {'owner': 'x', 'deviceId': 'x', 'masterPublic': '0', 'address': 'addr-x', 'state': 'tombstone'},
    ]
    status = {'tailnets': [{'memberReady': True, 'members': rows}]}
    assert candidates(status, master_public='m', device_id='me') == {'a': 'addr-a'}
    status['tailnets'][0]['memberReady'] = False
    assert candidates(status, master_public='m', device_id='me') == {}


def test_candidate_failure_does_not_build_orgfs():
    from types import SimpleNamespace
    from hyprial.daemon.impl.org.network.ipc import directory_peers_for_app
    def forbidden():
        pytest.fail('must not construct OrgFS to obtain tailnet candidates')
    app = SimpleNamespace(_forwarding_supervisor=None, _require_orgfs_runtime=forbidden)
    assert directory_peers_for_app(app) == {}


@pytest.mark.parametrize('key', ['master', 'server'])
def test_peer_owner_refuses_master_or_server_key_alias(key):
    from hyprial.daemon.impl.forwarding.membership.projection import owner_for_key
    status = {'tailnets': [{'memberReady': True, 'members': [{
        'owner': 'alice', 'deviceId': 'a', 'state': 'active', 'masterPublic': 'master',
        'serverPublic': 'server', 'clientPublicByServer': {'local-server': key}}]}]}
    assert owner_for_key(status, key, 'local-server') is None


def test_readiness_diagnostics_are_separate_expiring_and_secret_free(monkeypatch):
    from types import SimpleNamespace
    from hyprial.daemon.impl.forwarding.membership.projection import public_observation, readiness_for_app
    from hyprial.daemon.impl.forwarding.membership import projection
    status = {'readiness': {'sidecar': True, 'control': True, 'member': True},
        'tailnets': [{'tailnetId': 's', 'role': 'member', 'memberReady': True,
                     'leaseRemainingSeconds': 10, 'members': [{'address': 'secret', 'masterPublic': 'key'}]}]}
    observation = public_observation(status) | {'observedAtMs': 1000000, 'observedMonotonic': 100, 'sidecarPid': 42}
    assert 'secret' not in str(observation) and 'masterPublic' not in str(observation)
    app = SimpleNamespace(_forwarding_supervisor=SimpleNamespace(state='running', current_pid=42,
        last_membership_observation=observation), _transport=None, _orgfs_runtime=None)
    monkeypatch.setattr(projection.time, 'time', lambda: 1005)
    monkeypatch.setattr(projection.time, 'monotonic', lambda: 105)
    value = readiness_for_app(app)
    assert value['member'] and value['control'] and not value['zenoh'] and not value['orgfs']
    # Wall-clock rollback must not extend a 10-second observed lease.
    monkeypatch.setattr(projection.time, 'time', lambda: 1)
    monkeypatch.setattr(projection.time, 'monotonic', lambda: 111)
    assert readiness_for_app(app)['member'] is False
    app._forwarding_supervisor.current_pid = 43
    assert readiness_for_app(app)['sidecar'] is False
