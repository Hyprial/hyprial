"""The real ``hyprial gui`` on an ownerless home starts the bootstrap daemon (KP-083, Z-6a).

An isolated ownerless home, no daemon, an installed-shaped asset tree and the
real ``hyprial gui --no-open --json`` command.  The launcher ensures the daemon
through the one launch primitive, the child forwards to it, a second invocation
reuses it, and ending a GUI never stops it.  No login is performed, so no
identity issuer is contacted.
Decision: portable-process-cleanup-20261119.  The recorded daemon is stopped through
the lifecycle ledger (birth checked before the signal), and "no process holds the
home" uses the packaging verifier's ``carriers`` (``/proc`` on Linux, ``lsof`` on
macOS) instead of a ``/proc`` scan of this test.
"""

from __future__ import annotations

import time

from tests.tooling.release.shell.support import load

from .conftest import pytestmark  # noqa: F401
from .support import (
    end_gui_command,
    read_record,
    start_gui_command,
    wheel_assets,
)


def test_gui_on_an_ownerless_home_starts_the_bootstrap_daemon_and_leaves_it_running(lifecycle):
    assets = wheel_assets(lifecycle.root)
    stderr_path = lifecycle.root / "gui.stderr"
    children = []
    try:
        assert lifecycle.ping() is None
        with stderr_path.open("wb") as stderr:
            first = start_gui_command(lifecycle, assets, stderr)
        children.append(first)
        started = time.monotonic()
        record = read_record(first, stderr_path)
        first.snapshot()
        assert record.get("ok") is True, (
            f"GUI start failed after {time.monotonic() - started:.1f}s: {record}\n"
            f"{stderr_path.read_text(encoding='utf-8', errors='replace')[-6000:]}"
        )
        ping = lifecycle.ping()
        assert ping is not None and ping["phase"] == "bootstrap" and ping["owner"] is None
        pid, epoch = ping["pid"], ping["epoch"]
        assert end_gui_command(first) in {0, 143}
        survived = lifecycle.ping()
        assert survived is not None and (survived["pid"], survived["epoch"]) == (pid, epoch)

        with stderr_path.open("ab") as stderr:
            second = start_gui_command(lifecycle, assets, stderr)
        children.append(second)
        again = read_record(second, stderr_path)
        second.snapshot()
        assert again.get("ok") is True, again
        reused = lifecycle.ping()
        assert reused is not None and (reused["pid"], reused["epoch"]) == (pid, epoch)
        assert end_gui_command(second) in {0, 143}
        final = lifecycle.ping()
        assert final is not None and (final["pid"], final["epoch"]) == (pid, epoch)
    finally:
        for command in children:
            command.release()
        lifecycle.stop_recorded()
        for command in children:
            assert command.survivors() == []


def _holding(root) -> list[int]:
    return load("verify_runtime").carriers(root)


def test_after_gui_exits_the_homes_daemon_runs_and_daemon_stop_for_that_home_ends_it_and_leaves_no_process(lifecycle):
    """The fact the packaging verifier and smoke rely on (packaging/clients/verify_runtime.py header)."""

    assets = wheel_assets(lifecycle.root)
    stderr_path = lifecycle.root / "gui.stderr"
    command = None
    try:
        with stderr_path.open("wb") as stderr:
            command = start_gui_command(lifecycle, assets, stderr)
        record = read_record(command, stderr_path)
        command.snapshot()
        assert record.get("ok") is True, record
        assert end_gui_command(command) in {0, 143}
        command.release()
        recorded = lifecycle.recorded_pids()
        assert len(recorded) == 1, recorded
        (pid,) = recorded
        ping = lifecycle.ping()
        assert ping is not None and ping["pid"] == pid
        assert pid in _holding(lifecycle.root)

        code, answer = lifecycle.cli_json("daemon", "stop", "--json")
        assert code == 0 and answer["ok"] is True, answer
        lifecycle.wait_dead(pid)
        lifecycle.assert_nothing_left(pid)
        assert lifecycle.ping() is None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _holding(lifecycle.root):
            time.sleep(0.1)
        assert _holding(lifecycle.root) == []
    finally:
        if command is not None:
            command.release()
        lifecycle.stop_recorded()
