"""``daemon restart`` replaces a bootstrap daemon end to end (KP-084).

Decision: daemon-first-z4-bootstrap-restart-20261117.  Real CLI and daemon on an
isolated ownerless home.  The restart consults the per-user service manager,
which HOME cannot isolate (KP-093), so the commands run through
``unsupervised_entry.py``, which reports the platform as having no manager; the
supervised leg is not covered here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .conftest import pytestmark  # noqa: F401

ENTRY = Path(__file__).with_name("unsupervised_entry.py")


def _cli_json(lifecycle, *arguments):
    result = subprocess.run(
        [sys.executable, str(ENTRY), *arguments],
        env=lifecycle.env, capture_output=True, text=True, timeout=120, cwd=str(lifecycle.root),
    )
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, (result.returncode, result.stdout, result.stderr[-2000:])
    return result.returncode, json.loads(lines[0])


def test_restart_replaces_a_bootstrap_daemon_and_reports_its_phase(lifecycle):
    code, started = _cli_json(lifecycle, "daemon", "start", "--json")
    assert code == 0 and started["phase"] == "bootstrap", started
    first = started["pid"]

    code, restarted = _cli_json(lifecycle, "daemon", "restart", "--json")

    assert code == 0 and restarted["ok"] is True, restarted
    assert restarted["supervision"] == "unsupervised"
    assert restarted["restarted"] is True and restarted["started"] is True
    assert restarted["before"]["pid"] == first and restarted["before"]["phase"] == "bootstrap"
    assert restarted["after"]["pid"] != first and restarted["after"]["phase"] == "bootstrap"
    assert restarted["interruptedTurns"] == []

    code, status = _cli_json(lifecycle, "daemon", "status", "--json")
    pid = status["daemon"]["pid"]
    assert pid == restarted["after"]["pid"]
    code, stopped = _cli_json(lifecycle, "daemon", "stop", "--json")
    assert code == 0, stopped
    lifecycle.assert_nothing_left(pid, first)
