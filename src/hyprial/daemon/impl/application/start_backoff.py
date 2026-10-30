"""Consecutive start-failure accounting and the relaunch backoff it implies.

launchd (``KeepAlive`` + ``ThrottleInterval=5``) and systemd (``Restart=``)
relaunch a daemon that exits during startup without knowing why it failed, so
a deterministic refusal became a hot loop (2026-10-28: 271 failed starts in
1 h 49 min, ~100 s of CPU each, invisible from the product surface).  This
file keeps ``state/daemon-start-failures.json`` -- a small 0600 record an
operator (and ``hyprial doctor``) can read without a daemon -- and derives a
bounded backoff from the count.  Every ``daemon.start.failed`` logs
``attempt``/``backoffMs``; a service-managed daemon waits that long
(interruptibly) before exiting; a successful ``daemon.ready`` clears the file.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from hyprial.kernel import atomic_json_write

FAILURES_FILE = 'daemon-start-failures.json'
BASE_BACKOFF_MS = 5000
MAX_BACKOFF_MS = 60000
SERVICE_MANAGED_ENV = 'HYPRIAL_SERVICE_MANAGED'
_FIELDS = ('count', 'firstAt', 'lastAt', 'lastCode', 'lastPhase')


def failures_path(state_dir: Path) -> Path:
    return Path(state_dir) / FAILURES_FILE


def backoff_ms(attempt: int) -> int:
    return min(BASE_BACKOFF_MS * 2 ** max(attempt - 1, 0), MAX_BACKOFF_MS)


def read_start_failures(state_dir: Path) -> dict | None:
    """The persisted counter ``{count, firstAt, lastAt, lastCode, lastPhase}``, or None."""
    try:
        record = json.loads(failures_path(state_dir).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get('count'), int) or record['count'] < 1:
        return None
    return {key: record.get(key) for key in _FIELDS}


def record_start_failure(state_dir: Path, *, code: str | None = None, phase: str | None = None) -> dict:
    """Count one more consecutive failed start; return ``{attempt, backoffMs}`` for the log."""
    now = datetime.now(UTC).isoformat(timespec='seconds')
    previous = read_start_failures(state_dir)
    count = (previous['count'] if previous else 0) + 1
    record = {'count': count, 'firstAt': previous['firstAt'] if previous and previous.get('firstAt') else now,
              'lastAt': now, 'lastCode': code, 'lastPhase': phase}
    try:
        atomic_json_write(failures_path(state_dir), record)  # atomic, mode 0600
    except OSError:
        pass  # accounting must never mask the start failure it describes
    return {'attempt': count, 'backoffMs': backoff_ms(count)}


def reset_start_failures(state_dir: Path) -> None:
    try:
        failures_path(state_dir).unlink()
    except OSError:
        pass


def relaunch_wait_seconds(fields: dict, environ=None) -> float:
    """How long a service-managed daemon should wait before exiting after a failed start.

    The first failure never waits (an operator iterating by hand must not be
    slowed down); only a supervisor-driven relaunch loop is throttled.
    """
    env = os.environ if environ is None else environ
    attempt, delay = fields.get('attempt'), fields.get('backoffMs')
    if env.get(SERVICE_MANAGED_ENV) != '1' or not isinstance(attempt, int) or attempt <= 1 or not delay:
        return 0.0
    return delay / 1000.0
