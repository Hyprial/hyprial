"""Doctor row for the persisted consecutive daemon start-failure counter.

The daemon writes ``state/daemon-start-failures.json`` on every failed start
and clears it on ``daemon.ready``; this check reads it without a daemon, so
a restart loop is visible from the product surface (2026-10-28: 271 failed
starts in 1 h 49 min were not).
"""
from __future__ import annotations

from pathlib import Path

from hyprial.shell.impl.cli.commands.common.support import JsonObject

_MIGRATION_CODES = frozenset({'MIGRATION_AUTHORITY_MISSING', 'MEMBER_CONFLICT', 'AUTHORITY_PIN_MISMATCH',
                              'MEMBERSHIP_STORE_INVALID', 'MEMBERSHIP_NOT_INITIALIZED'})
FAIL_AFTER = 3


def start_failures_doctor_check(state_dir: Path) -> JsonObject | None:
    """One row when consecutive start failures are recorded; None when the file is absent."""
    from hyprial.daemon import read_start_failures

    record = read_start_failures(state_dir)
    if record is None:
        return None
    count, code, phase = record['count'], record.get('lastCode'), record.get('lastPhase')
    detail = (f"{count} consecutive failed daemon start(s) since {record.get('firstAt')} "
              f"(last {record.get('lastAt')}, phase {phase or 'unknown'}, code {code or 'none'}); "
              'see logs/daemon.jsonl daemon.start.failed')
    if code in _MIGRATION_CODES:
        action = {'command': 'hyprial org migration-status',
                  'description': ('Tailnet membership import refused: review each org space, then pin the '
                                  'authority with `hyprial org pin-authority <spaceId> --device <deviceId> --yes` '
                                  'and restart the daemon.')}
    else:
        action = {'command': 'hyprial daemon start',
                  'description': 'Read the last daemon.start.failed event, fix its cause, then start the daemon.'}
    return {'name': 'daemon-start-failures', 'status': 'fail' if count >= FAIL_AFTER else 'warn',
            'detail': detail, 'metrics': dict(record), 'action': action}


def start_failures_doctor_checks(state_dir: Path) -> list[JsonObject]:
    """List form for `checks.extend`: empty when no failure is recorded."""
    check = start_failures_doctor_check(state_dir)
    return [] if check is None else [check]
