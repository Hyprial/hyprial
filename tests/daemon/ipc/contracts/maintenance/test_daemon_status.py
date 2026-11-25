"""Daemon status projects isolated real ping; it is not model/network readiness."""

import pytest
from jsonschema import Draft202012Validator, ValidationError

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.request_catalog.maintenance.status import DAEMON_STATUS_SPEC, daemon_status
from hyprial.shell.impl.daemon_wiring import daemon_dependencies


@pytest.mark.parametrize('phase', ['serving', 'reconciled'])
def test_status_matches_isolated_daemon_ping(tmp_path, phase):
    app = DaemonApplication(state_dir=tmp_path, socket_path=tmp_path / 'daemon.sock',
                            owner='settings-status-owner', node_id='settings-status-node', **daemon_dependencies())
    try:
        app._restore_done.set()
        if phase == 'reconciled':
            app._readiness_first_round.set()
        else:
            app._readiness_first_round.clear()
        ping = app.handle('ping', {})
        result = daemon_status(app, {})
        assert result == {key: ping[key] for key in ('running', 'version', 'phase')}
        assert result['phase'] == phase
        Draft202012Validator(DAEMON_STATUS_SPEC.result_schema).validate(result)
    finally:
        app._close()
        app._close_home_lease()


def test_ping_failure_is_not_reported_as_stopped():
    failure = RuntimeError('ping unavailable')
    class App:
        def handle(self, method, params):
            assert (method, params) == ('ping', {})
            raise failure
    with pytest.raises(RuntimeError) as error:
        daemon_status(App(), {})
    assert error.value is failure


def test_missing_fields_cannot_validate_as_a_success():
    class App:
        def handle(self, method, params):
            return {'running': True, 'pid': 123, 'owner': 'private'}
    result = daemon_status(App(), {})
    assert set(result) == {'running', 'version', 'phase'}
    with pytest.raises(ValidationError):
        Draft202012Validator(DAEMON_STATUS_SPEC.result_schema).validate(result)
