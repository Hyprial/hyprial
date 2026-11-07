"""`daemon.start.failed` names the sidecar error code; the message stays secret-free."""
from __future__ import annotations

import pytest

from hyprial.daemon.impl.application.startup import _StartupMixin
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from hyprial.kernel import DaemonStartupPhase


class _Recorder(_StartupMixin):
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _log_trace(self, level: str, event: str, **fields) -> None:
        self.events.append((level, event, fields))


def _failed(recorder: _Recorder) -> dict:
    return next(fields for _level, event, fields in recorder.events if event == 'daemon.start.failed')


def test_start_failed_carries_the_sidecar_error_code():
    recorder = _Recorder()

    def refuse():
        raise ForwardingSidecarError('Tailnet membership command failed.', code='MEMBER_CONFLICT')

    with pytest.raises(ForwardingSidecarError):
        recorder._startup_step(refuse, DaemonStartupPhase.ACTOR_RUNTIME_FORWARDING_RECONFIGURE)
    fields = _failed(recorder)
    assert fields['phase'] == 'actor-runtime-forwarding-reconfigure'
    assert fields['code'] == 'MEMBER_CONFLICT'
    assert fields['errorType'] == 'ForwardingSidecarError'
    assert 'nodekey:' not in fields['error']


def test_start_failed_without_a_coded_error_reports_code_none():
    recorder = _Recorder()

    def boom():
        raise RuntimeError('plain failure')

    with pytest.raises(RuntimeError):
        recorder._startup_step(boom, DaemonStartupPhase.ACTOR_RUNTIME_FORWARDING_RECONFIGURE)
    assert _failed(recorder)['code'] is None


def _fail(recorder: _Recorder) -> None:
    def boom():
        raise ForwardingSidecarError('Tailnet membership command failed.', code='MEMBER_CONFLICT')
    with pytest.raises(ForwardingSidecarError):
        recorder._startup_step(boom, DaemonStartupPhase.ACTOR_RUNTIME_FORWARDING_RECONFIGURE)


def test_start_failed_counts_consecutive_attempts_with_a_growing_backoff(tmp_path):
    from hyprial.daemon.impl.application.start_backoff import reset_start_failures

    first = _Recorder()
    first.state_dir = tmp_path
    _fail(first)
    assert (_failed(first)['attempt'], _failed(first)['backoffMs']) == (1, 5000)
    second = _Recorder()
    second.state_dir = tmp_path
    _fail(second)
    assert (_failed(second)['attempt'], _failed(second)['backoffMs']) == (2, 10000)
    # A nested phase failing inside its parent phase is one attempt, not two.
    _fail(second)
    attempts = [fields['attempt'] for _l, event, fields in second.events if event == 'daemon.start.failed']
    assert attempts == [2, 2]
    reset_start_failures(tmp_path)
    third = _Recorder()
    third.state_dir = tmp_path
    _fail(third)
    assert _failed(third)['attempt'] == 1


def test_backoff_is_capped_at_one_minute(tmp_path):
    from hyprial.daemon.impl.application.start_backoff import record_start_failure
    for _ in range(10):
        fields = record_start_failure(tmp_path)
    assert fields == {'attempt': 10, 'backoffMs': 60000}


def test_start_failed_without_a_state_dir_reports_unknown_attempt():
    recorder = _Recorder()
    _fail(recorder)
    assert (_failed(recorder)['attempt'], _failed(recorder)['backoffMs']) == (None, None)


def test_failed_starts_persist_an_operator_readable_counter(tmp_path):
    import json
    from hyprial.daemon.impl.application.start_backoff import failures_path, read_start_failures

    first = _Recorder()
    first.state_dir = tmp_path
    _fail(first)
    second = _Recorder()
    second.state_dir = tmp_path
    _fail(second)
    path = failures_path(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text())
    assert set(record) == {'count', 'firstAt', 'lastAt', 'lastCode', 'lastPhase'}
    assert record['count'] == 2 and record['lastCode'] == 'MEMBER_CONFLICT'
    assert record['lastPhase'] == 'actor-runtime-forwarding-reconfigure'
    assert record['firstAt'] <= record['lastAt']
    assert read_start_failures(tmp_path) == record
