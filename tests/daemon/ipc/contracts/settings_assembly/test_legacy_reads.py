"""Registry admission preserves existing maintenance reads and adjacent routes."""

import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.daemon.impl.upgrade import upgrade_step_status
from hyprial.kernel import DaemonRequestError


class App(_IpcDispatchMixin):
    _restore_done = threading.Event()
    _inbox = _harnesses = _presence = object()


@pytest.fixture
def app(tmp_path):
    result = App()
    result._restore_done = threading.Event()
    result._restore_done.set()
    result.state_dir = tmp_path
    result._autoupdate = SimpleNamespace(status=lambda: {
        'running': False, 'active': False, 'pending': False, 'nextRunAt': None,
        'lastTriggeredAt': None, 'lastTrigger': None, 'lastExitCode': None,
        'autoUpgradeEnabled': None, 'lastSkipReason': None, 'lastRun': None,
    })
    return result


@pytest.mark.parametrize('params', [{}, {'metadata': {'nested': [None, True, 42, 'x']}}, {'owner': 'ignored'}])
def test_registered_maintenance_reads_keep_legacy_params_and_result(app, monkeypatch, params):
    from hyprial.daemon.impl.upgrade.steps import mcp_entry

    monkeypatch.setattr(mcp_entry, 'legacy_processes', lambda: [])
    assert app.handle('autoupdate.status', params) == app._ipc_autoupdate_status()
    assert app.handle('upgrade.steps.status', params) == {
        **upgrade_step_status(app.state_dir), 'legacyProcesses': [],
    }


def test_neighboring_write_stays_on_legacy_handler(app):
    expected = {'legacy': True}
    app._ipc_autoupdate_trigger = lambda: expected
    assert app.handle('autoupdate.trigger', {}) is expected
    assert app.handle('upgrade.steps.run', {'apply': False}) == {
        'ok': False, 'code': 'INVALID_ARGUMENT', 'error': 'apply required',
    }


def test_restore_gate_still_precedes_registered_parameter_validation(app):
    app._restore_done.clear()
    with pytest.raises(DaemonRequestError) as error:
        app.handle('daemon.status', {'forged': True})
    assert error.value.code == 'DAEMON_RESTORING'


def test_registered_identity_reads_keep_legacy_ipc_params(app):
    app.hyprial_home = app.state_dir
    assert app.handle('identity.users.list', {'legacyMetadata': True}) == {
        'users': []
    }
    assert not (app.state_dir / 'users.sqlite3').exists()
