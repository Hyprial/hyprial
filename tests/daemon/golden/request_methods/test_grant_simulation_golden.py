"""Golden responses for both real grant simulation handlers."""

from collections.abc import Callable

from tests.support.golden.app import GoldenDaemon, golden_daemon as golden_daemon


def test_grant_simulations_golden(golden_daemon: Callable[..., GoldenDaemon]) -> None:
    daemon = golden_daemon()
    golden = daemon.golden('request_methods', 'grant-simulations')
    common = {'caller': None, 'hosted': False, 'unverified': False}
    check = {**common, 'capability': 'tool-surface', 'resource': 'shell',
             'scope': [], 'revision': 1, 'write': False}
    visibility = {**common, 'seeActors': None, 'sendTo': None, 'targets': ['user:owner']}
    result = golden.call('agent.grant.check', {'scenario': check}, label='operator-check')
    assert result['allowed'] is True
    result = golden.call('agent.grant.visibility', {'scenario': visibility}, label='operator-visibility')
    assert result['visible'] == ['user:owner']
    error = golden.call('agent.grant.check', {'scenario': {**check, 'caller': ' '}}, label='bad-caller-check')
    assert error.code == 'ENFORCEMENT_ERROR'
    error = golden.call('agent.grant.visibility', {'scenario': {**visibility, 'caller': ' '}},
                        label='bad-caller-visibility')
    assert error.code == 'VISIBILITY_ERROR'
    golden.check()
