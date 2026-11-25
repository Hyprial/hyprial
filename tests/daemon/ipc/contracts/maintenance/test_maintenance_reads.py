"""Maintenance contracts preserve existing read authority without executing upgrades."""
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.daemon.impl.request_catalog.maintenance import MAINTENANCE_BINDINGS
from hyprial.daemon.impl.request_catalog.maintenance.read_specs import autoupdate_status
from hyprial.daemon.impl.request_catalog.maintenance.steps import upgrade_steps_status
from hyprial.kernel import DaemonRequestError, validate_method_specs


def test_read_contracts_do_not_admit_upgrade_execution():
    specs = tuple(spec for spec, _ in MAINTENANCE_BINDINGS)
    validate_method_specs(specs)
    assert {s.name for s in specs} == {'autoupdate.status', 'upgrade.steps.status', 'daemon.status', 'log.page'}
    for spec in specs:
        assert not spec.mutates
        assert spec.retry == 'safe'
        assert spec.audience == ('local-operator',)
        assert spec.http_exposed


def test_scheduler_read_uses_existing_handler():
    row = {'running': False, 'active': False, 'pending': False, 'nextRunAt': None,
           'lastTriggeredAt': None, 'lastTrigger': None, 'lastExitCode': None,
           'autoUpgradeEnabled': None, 'lastSkipReason': None, 'lastRun': None}
    class App(_IpcDispatchMixin):
        _autoupdate = SimpleNamespace(status=lambda: dict(row))
    app = App()
    result = autoupdate_status(app, {})
    assert result == app._ipc_autoupdate_status()
    Draft202012Validator(MAINTENANCE_BINDINGS[0][0].result_schema).validate(result)


def test_missing_scheduler_is_not_an_empty_success():
    with pytest.raises(DaemonRequestError) as error:
        autoupdate_status(SimpleNamespace(), {})
    assert error.value.code == 'DAEMON_NOT_READY'


def test_steps_read_preserves_existing_projection_without_writes(tmp_path, monkeypatch):
    from hyprial.daemon.impl.upgrade import upgrade_step_status
    from hyprial.daemon.impl.upgrade.steps import mcp_entry
    monkeypatch.setattr(mcp_entry, 'legacy_processes', lambda: [])
    expected = {**upgrade_step_status(tmp_path), 'legacyProcesses': []}
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    result = upgrade_steps_status(SimpleNamespace(state_dir=tmp_path), {})
    assert result == expected
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before
    Draft202012Validator(MAINTENANCE_BINDINGS[1][0].result_schema).validate(result)


def test_examples_match_the_declared_contracts():
    from hyprial.daemon.impl.request_catalog.maintenance import MAINTENANCE_EXAMPLES
    from hyprial.kernel import validate_method_example_sets
    assert validate_method_example_sets(
        (spec for spec, _ in MAINTENANCE_BINDINGS), MAINTENANCE_EXAMPLES
    ) == tuple(sorted(MAINTENANCE_EXAMPLES, key=lambda item: item.method))
