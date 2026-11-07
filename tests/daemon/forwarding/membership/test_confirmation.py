from types import SimpleNamespace

import pytest


def test_noninteractive_remove_requires_yes_before_confirmed_call():
    from hyprial.shell.impl.cli.commands.org.removal import confirmed_removal
    calls = []
    class Error(Exception):
        def __init__(self, code, message):
            self.code = code
    def request(method, params):
        calls.append(params)
        return {'operationId': 'preview-id', 'deviceIds': ['one', 'two'], 'tailnetId': 's', 'authorityDeviceId': 'auth',
                'phase': 'pending-confirmation'}
    services = SimpleNamespace(
        runtime=SimpleNamespace(request=request),
        _stdin_isatty=lambda: False,
        CliError=Error,
    )
    with pytest.raises(Error) as error:
        confirmed_removal(services, 'org.remove', {'org': 'x', 'user': 'bob'}, yes=False)
    assert error.value.code == 'CONFIRMATION_REQUIRED'
    assert calls == [{'org': 'x', 'user': 'bob', 'confirmed': False}]


@pytest.mark.parametrize('yes', [False, True])
def test_revision_refresh_cli_shows_changes_before_reconfirmation(monkeypatch, yes):
    from hyprial.shell.impl.cli.commands.org import removal as module
    calls, notices = [], []
    class Error(Exception):
        def __init__(self, code, message):
            self.code = code
    def request(method, params):
        calls.append(params['confirmed'])
        if params['confirmed']:
            assert params['operationId'] == 'frozen-id'
            assert params['deviceIds'] == ['b1', 'b2']
        return {'operationId': 'frozen-id', 'phase': 'complete' if params['confirmed'] else 'revision-confirmation',
                'deviceIds': ['b1', 'b2'], 'revisionRefresh': {
                    'oldRevision': 1, 'newRevision': 4, 'remainingDeviceIds': ['b2'],
                    'alreadyRemovedDeviceIds': ['b1'], 'unselectedDeviceIds': ['b3']}}
    monkeypatch.setattr(module, 'notice', notices.append)
    services = SimpleNamespace(
        runtime=SimpleNamespace(request=request),
        _stdin_isatty=lambda: False,
        CliError=Error,
    )
    if yes:
        assert module.confirmed_removal(services, 'org.remove', {'user': 'bob'}, yes=True)['phase'] == 'complete'
    else:
        with pytest.raises(Error) as caught:
            module.confirmed_removal(services, 'org.remove', {'user': 'bob'}, yes=False)
        assert caught.value.code == 'CONFIRMATION_REQUIRED'
    assert calls == ([False, True] if yes else [False])
    assert '1 → 4' in notices[1] and "already removed ['b1']" in notices[1]
    assert "new devices NOT selected ['b3']" in notices[1]


@pytest.mark.parametrize('yes', [False, True])
def test_offline_repin_cli_prints_preview_and_requires_confirmation(monkeypatch, tmp_path, yes):
    import json
    from typer.testing import CliRunner
    from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
    from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
    import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
    monkeypatch.setenv('HYPRIAL_HOME', str(tmp_path))
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(_cli_get_services(), 'require_initialized_hyprial_home', lambda: None)
    calls = []
    class Maintenance:
        def __init__(self, home, state_dir):
            assert home == tmp_path and state_dir == tmp_path / 'state'
        def __enter__(self):
            return self
        def __exit__(self, *_error):
            calls.append('close')
        def prepare(self, tailnet_id, authority, *, control_port):
            assert (tailnet_id, authority, control_port) == ('space', 'new', 7448)
            calls.append('preview')
            return {'previousAuthorityDeviceId': 'old', 'deviceIds': ['old', 'new']}
        def commit(self, *, confirmed):
            assert confirmed is True
            calls.append('commit')
            return {'phase': 'complete', 'backupPath': '/fixture/backup', 'restartRequired': True}
    monkeypatch.setattr('hyprial.daemon.TailnetMaintenance', Maintenance)
    args = ['org', 'repin', 'space', '--authority', 'new', '--json'] + (['--yes'] if yes else [])
    result = CliRunner().invoke(_cli_root_app, args)
    payload = json.loads(result.stdout)
    assert 'affected devices' in result.stderr
    assert calls == (['preview', 'commit', 'close'] if yes else ['preview', 'close'])
    assert result.exit_code == (0 if yes else 1)
    if not yes:
        assert payload['code'] == 'CONFIRMATION_REQUIRED'
    else:
        assert payload['restartRequired'] is True


def test_offline_repin_refuses_when_ownership_fence_is_busy(monkeypatch, tmp_path):
    import json
    from typer.testing import CliRunner
    from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
    from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
    import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

    monkeypatch.setenv('HYPRIAL_HOME', str(tmp_path))
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(_cli_get_services(), 'require_initialized_hyprial_home', lambda: None)
    calls = []

    class Maintenance:
        def __init__(self, home, state_dir):
            del home, state_dir
        def __enter__(self):
            error = RuntimeError('daemon still owns the state')
            error.code = 'NETWORK_NOT_QUIESCENT'
            raise error
        def __exit__(self, *_error):
            calls.append('close')
        def commit(self, *, confirmed):
            calls.append('commit')

    monkeypatch.setattr('hyprial.daemon.TailnetMaintenance', Maintenance)
    result = CliRunner().invoke(
        _cli_root_app,
        ['org', 'repin', 'space', '--authority', 'new', '--yes', '--json'],
    )
    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload['code'] == 'NETWORK_NOT_QUIESCENT'
    assert calls == []
