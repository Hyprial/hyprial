"""Golden responses for routine planning with the real routine loader."""

from collections.abc import Callable

from tests.shell.cli.workflows.test_cli_routine import DOC
from tests.support.golden.app import GoldenDaemon, golden_daemon as golden_daemon


def test_routine_plan_golden(golden_daemon: Callable[..., GoldenDaemon]) -> None:
    daemon = golden_daemon()
    golden = daemon.golden('request_methods', 'routine-plan')
    result = golden.call('routine.plan', {'yaml': DOC, 'label': 'routine.yaml'}, label='success')
    assert isinstance(result, dict)
    assert result['plan']['name'] == 'cli-routine'
    error = golden.call('routine.plan', {'yaml': 'name: missing-version', 'label': 'routine.yaml'},
                        label='schema-error')
    assert error.code == 'ROUTINE_SCHEMA_ERROR'
    golden.check()
