"""The launcher that started the daemon reaps it, so its exit leaves no zombie.

Real ``hyprial gui --no-open --json`` on an isolated owned home: the launcher
starts the daemon itself (detached, in its own session) and keeps running.  The
home already has an owner and its login settings (no identity issuer is
contacted), so the daemon the launcher starts is serving.  Before the spawner
reaped its child an exited daemon stayed a zombie of the long-lived launcher,
and a stop that looked for the exit still saw the pid.
Decision: portable-process-cleanup-20261119.  The parent is read from the ``ps``
table and "reaped" means no process with the daemon's recorded birth exists at
all, a zombie included (``Ledger.exists``), so the check is not vacuous without
``/proc``; the launcher is released by the ``lifecycle`` fixture even when an
assertion fails.
Decision: L1 (owner 2026-11-21).  ``hyprial logout`` is one daemon
``auth.logout`` and no longer stops the daemon or proves its exit: the serving
gateway stops and the same process, still the launcher's child, returns to
bootstrap (``daemon.stopRequested`` is true because the serving daemon removed
the login).  The exit that the launcher must reap now comes from
``hyprial daemon stop``, which keeps its receipt wait (``require_exit=False``,
so a pid still exiting is reported, not failed); the test therefore waits a
bounded time for the recorded daemon to be gone, which never happens if the
launcher leaves it a zombie.
"""

from __future__ import annotations

import json
import time

from tests.integration.gui_daemon_first.support import (
    ISSUER,
    OWNER,
    end_gui_command,
    read_record,
    start_gui_command,
    wheel_assets,
)
from tests.support.process_table import table

from .conftest import pytestmark  # noqa: F401

REAP_SECONDS = 30


def _parent(pid: int) -> int:
    return table()[pid].ppid


def _wait_reaped(lifecycle, pid: int) -> bool:
    deadline = time.monotonic() + REAP_SECONDS
    while lifecycle.ledger.exists(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def test_a_cli_logout_keeps_the_launcher_daemon_and_its_stop_is_reaped(lifecycle):
    (lifecycle.home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": OWNER, "identityMode": "casdoor", "identityIssuer": ISSUER}),
        encoding="utf-8",
    )
    assets = wheel_assets(lifecycle.root)
    stderr_path = lifecycle.root / "gui.stderr"
    with stderr_path.open("wb") as stderr:
        command = start_gui_command(lifecycle, assets, stderr)
    try:
        record = read_record(command, stderr_path)
        command.snapshot()
        assert record.get("ok") is True, record
        ping = lifecycle.ping()
        assert ping is not None and ping["owner"] == OWNER
        pid = ping["pid"]
        assert lifecycle.ledger.exists(pid)
        assert _parent(pid) == command.process.pid, "the launcher is not the daemon's parent"

        code, payload = lifecycle.cli_json("logout", "--yes", "--json")
        assert code == 0 and payload["status"] == "logged-out", payload
        assert payload["daemon"] == {"stopRequested": True}, payload
        back = lifecycle.wait_phase({"bootstrap"}, budget=60)
        assert back["pid"] == pid and back["owner"] is None, back
        assert back["epoch"] != ping["epoch"], back
        assert lifecycle.ledger.exists(pid)
        assert _parent(pid) == command.process.pid, "the daemon left the launcher after logout"
        assert command.process.poll() is None

        code, stopped = lifecycle.cli_json("daemon", "stop", "--json")
        assert code == 0 and stopped["ok"] is True, stopped
        assert stopped.get("pid") in (None, pid), stopped
        assert _wait_reaped(lifecycle, pid), "the stopped daemon was left as a zombie of the launcher"
        assert lifecycle.ping() is None
        assert command.process.poll() is None
    finally:
        end_gui_command(command)
        command.release()
