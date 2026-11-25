"""Grant simulation requests across the actual daemon socket and CLI."""

import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from hyprial.kernel import daemon_request, ipc_errors
from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.runtime import entry as cli
from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app
from tests.integration.service_connect.test_service_connect_integration import _IdleRuntime


@pytest.fixture()
def server(app):
    app.socket_path = Path('/tmp') / f'grant-sim-{uuid4().hex}.sock'
    app._runtime = _IdleRuntime()
    app._maintenance_generation = 1
    app._start_server()
    thread = threading.Thread(target=app._serve, daemon=True)
    thread.start()
    try:
        yield app
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        thread.join(2)
        app.socket_path.unlink(missing_ok=True)
        assert not thread.is_alive()


@pytest.fixture()
def online(server, monkeypatch):
    monkeypatch.setattr(support, '_service_socket_path', lambda: server.socket_path)
    monkeypatch.setenv('HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS', '0')
    return server


@pytest.mark.parametrize(('verb', 'extra', 'message'), [
    ('check', ['--capability', 'tool-surface', '--resource', 'shell'],
     'principal uri must be a non-empty, unpadded string'),
    ('visibility', ['--target', 'user:owner'], 'caller uri must be a non-empty, unpadded string'),
])
def test_invalid_caller_keeps_uncoded_cli_failure(online, verb, extra, message):
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', verb, *extra,
                                         '--caller', ' ', '--json'])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {'ok': False, 'error': message}


def test_check_scope_error_keeps_old_cli_code(online):
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', 'check',
        '--capability', 'unknown', '--resource', 'shell', '--caller', 'user:owner', '--scope', 'bad', '--json'])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {'ok': False, 'code': 'enforcement_scope_invalid',
                                         'error': "unknown capability 'unknown'"}


def test_visibility_scope_error_keeps_old_cli_code(online):
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', 'visibility',
        '--caller', 'user:owner', '--see-actors', 'not-json', '--target', 'user:other', '--json'])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['code'] == 'visibility_scope_invalid'


def test_operator_ignores_unused_bad_scope_and_revision(online):
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', 'check',
        '--capability', 'tool-surface', '--resource', 'shell', '--scope', 'not-json',
        '--revision', '0', '--hosted', '--unverified', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['allowed'] is True


def test_restore_gate_precedes_simulation(server):
    server._restore_done.clear()
    try:
        with pytest.raises(ipc_errors.DaemonRestoringError):
            daemon_request('agent.grant.visibility', {'scenario': {'caller': None, 'hosted': False,
                'unverified': False, 'seeActors': None, 'sendTo': None, 'targets': []}},
                socket_path=server.socket_path, timeout=10.0, restore_wait=0)
    finally:
        server._restore_done.set()


def test_published_examples_match_real_daemon(server):
    from hyprial.daemon.impl.request_catalog.agent.grant_simulation.examples import EXAMPLES
    from hyprial.kernel import RequestPortError, example_to_json

    for example_set in EXAMPLES:
        for case in example_set.cases:
            example = example_to_json(case)
            if 'result' in example:
                result = daemon_request(example_set.method, example['params'],
                    socket_path=server.socket_path, timeout=10.0, restore_wait=0)
                assert result == example['result']
            else:
                with pytest.raises(RequestPortError) as error:
                    daemon_request(example_set.method, example['params'],
                        socket_path=server.socket_path, timeout=10.0, restore_wait=0)
                assert error.value.code == example['error']['code']
                assert str(error.value) == example['error']['message']


def test_visibility_without_caller_ignores_invalid_grants(online):
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', 'visibility',
        '--see-actors', 'bad', '--send-to', 'bad', '--hosted', '--unverified', '--target', 'user:other', '--json'])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body['caller'] is None
    assert body['hosted'] is True and body['verified'] is False
    assert body['visible'] == ['user:other']


def test_offline_simulation_reports_daemon_unavailable(monkeypatch):
    monkeypatch.setattr(support, '_service_socket_path', lambda: Path('/tmp') / f'missing-{uuid4().hex}.sock')
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', 'visibility', '--json'])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['code'] == 'DAEMON_UNAVAILABLE'


@pytest.mark.parametrize(('name', 'args'), [
    ('check', ['check', '--capability', 'tool-surface', '--resource', 'shell', '--caller', 'user:visitor', '--hosted']),
    ('visibility', ['visibility', '--caller', 'user:visitor', '--target', 'user:other']),
    ('constructor', ['check', '--capability', 'tool-surface', '--resource', 'shell', '--caller', ' ']),
    ('scope', ['visibility', '--caller', 'user:owner', '--see-actors', 'bad', '--target', 'user:other']),
])
def test_text_output_matches_old_cli(online, name, args):
    baseline = json.loads(Path(__file__).with_name('grant_text_baseline.json').read_text(encoding='utf-8'))
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', *args])
    assert {'stdout': result.stdout, 'stderr': result.stderr, 'exit_code': result.exit_code} == baseline[name]
