"""Bound worker grant simulations preserve the relay's identity fence."""

import json

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.runtime import entry as cli
from tests.daemon.workers.test_worker_relay import frame, isolated as isolated, request


@pytest.mark.parametrize(('verb', 'extra', 'method'), [
    ('check', ['--capability', 'tool-surface', '--resource', 'shell'], 'agent.grant.check'),
    ('visibility', ['--target', 'user:other'], 'agent.grant.visibility'),
])
def test_worker_cli_simulates_through_bound_relay(isolated, monkeypatch, verb, extra, method):
    _, _, relay, _ = isolated
    seen = []
    actual = support._daemon_request

    def forwarded(name, params=None, **kwargs):
        seen.append(name)
        return actual(name, params, **kwargs)

    monkeypatch.setattr(support, '_service_socket_path', lambda: relay.socket_path)
    monkeypatch.setattr(support, '_daemon_request', forwarded)
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', verb, *extra, '--json'])
    assert result.exit_code == 0, result.output
    assert seen == [method]
    assert json.loads(result.stdout)['ok'] is True


@pytest.mark.parametrize(('key', 'value', 'code'), [
    ('resourcePaths', ['/host/secret'], 'INVALID_ARGUMENT'),
    ('actor', 'agent:test:isolated:bob', 'CALLER_NOT_AUTHORIZED'),
    ('sessionRef', 'forged-session', 'CALLER_NOT_AUTHORIZED'),
])
def test_worker_refuses_extra_or_forged_metadata(isolated, key, value, code):
    _, app, relay, binding = isolated
    scenario = {'caller': None, 'hosted': False, 'unverified': False,
                'seeActors': None, 'sendTo': None, 'targets': []}
    inner = frame('agent.grant.visibility', {'scenario': scenario, key: value})
    assert request(relay.socket_path, inner)['error']['code'] == code
    assert request(app.socket_path, frame('worker.channel.request', binding.envelope(inner)))['error']['code'] == code


@pytest.mark.parametrize('change', ['epoch', 'session'])
def test_worker_rejects_stale_binding(isolated, change):
    _, app, relay, _ = isolated
    if change == 'epoch':
        app.epoch = 'replacement-daemon'
    else:
        app._harnesses.refs[('codex', 'alice')] = 'new-session'
    scenario = {'caller': None, 'hosted': False, 'unverified': False,
                'seeActors': None, 'sendTo': None, 'targets': []}
    response = request(relay.socket_path, frame('agent.grant.visibility', {'scenario': scenario}))
    assert response['error']['code'] == 'STALE_SESSION'


def test_worker_schema_error_is_sanitized(isolated):
    _, _, relay, _ = isolated
    scenario = {'caller': ' ', 'hosted': False, 'unverified': False,
                'seeActors': None, 'sendTo': None, 'targets': []}
    response = request(relay.socket_path, frame('agent.grant.visibility', {'scenario': scenario}))
    assert response['error'] == {'code': 'VISIBILITY_ERROR', 'message': 'Worker channel request failed'}


@pytest.mark.parametrize(('verb', 'extra', 'expected'), [
    ('check', ['--capability', 'tool-surface', '--resource', 'shell', '--caller', ' '],
     {'ok': False, 'error': 'Worker channel request failed'}),
    ('visibility', ['--caller', ' ', '--target', 'user:other'],
     {'ok': False, 'error': 'Worker channel request failed'}),
    ('check', ['--capability', 'unknown', '--resource', 'shell', '--caller', 'user:owner', '--scope', 'bad'],
     {'ok': False, 'code': 'enforcement_scope_invalid', 'error': 'Worker channel request failed'}),
    ('visibility', ['--caller', 'user:owner', '--see-actors', 'bad', '--target', 'user:other'],
     {'ok': False, 'code': 'visibility_scope_invalid', 'error': 'Worker channel request failed'}),
])
def test_worker_cli_preserves_error_shape_and_sanitizes_message(isolated, monkeypatch, verb, extra, expected):
    _, _, relay, _ = isolated
    monkeypatch.setattr(support, '_service_socket_path', lambda: relay.socket_path)
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', verb, *extra, '--json'])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == expected


def test_worker_binding_does_not_override_simulated_caller(isolated, monkeypatch):
    _, _, relay, binding = isolated
    monkeypatch.setattr(support, '_service_socket_path', lambda: relay.socket_path)
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', 'check',
        '--capability', 'tool-surface', '--resource', 'shell', '--caller', 'user:visitor', '--hosted', '--json'])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body['allowed'] is False
    assert body['caller'] == 'user:visitor'
    assert body['caller'] != binding.actor
