"""The dsh carrier's terminal-signal contract, without spawning processes.

SIGTERM and SIGHUP are normal stops (SIGHUP is what a closed terminal sends),
and SIGINT is deliberately left to the interactive child so Ctrl-C inside
dsh-tui cannot tear the bridge down before it unregisters.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from hyprial.daemon.impl.mcp import entry as mcp_module


class _FakeSignal:
    SIGTERM = 15
    SIGHUP = 1
    SIGINT = 2
    SIG_IGN = "ignored"

    def __init__(self) -> None:
        self.installed: dict[int, Any] = {}

    def signal(self, signum: int, handler: Any) -> tuple[str, int]:
        self.installed[signum] = handler
        return ("previous", signum)


def test_carrier_stops_on_sigterm_and_sighup_but_not_on_sigint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSignal()
    monkeypatch.setattr(mcp_module, "signal", fake)
    stop = threading.Event()

    previous = mcp_module._install_carrier_signal_handlers(stop)

    assert previous == {
        _FakeSignal.SIGTERM: ("previous", _FakeSignal.SIGTERM),
        _FakeSignal.SIGHUP: ("previous", _FakeSignal.SIGHUP),
        _FakeSignal.SIGINT: ("previous", _FakeSignal.SIGINT),
    }
    assert fake.installed[_FakeSignal.SIGINT] == _FakeSignal.SIG_IGN

    fake.installed[_FakeSignal.SIGTERM](None, None)
    assert stop.is_set()
    stop.clear()
    fake.installed[_FakeSignal.SIGHUP](None, None)
    assert stop.is_set()

    mcp_module._restore_carrier_signal_handlers(previous)
    assert fake.installed[_FakeSignal.SIGTERM] == ("previous", _FakeSignal.SIGTERM)
    assert fake.installed[_FakeSignal.SIGHUP] == ("previous", _FakeSignal.SIGHUP)
    assert fake.installed[_FakeSignal.SIGINT] == ("previous", _FakeSignal.SIGINT)


_STDERR_PROBE = r"""
import logging
import os
import sys
import time
from pathlib import Path

from hyprial.daemon.impl.mcp import entry as mcp_module

launch_dir = Path(sys.argv[1])
tui = mcp_module._spawn_owned_tui(
    [
        sys.executable,
        "-c",
        "import sys, time; time.sleep(0.3); sys.stderr.write('TUI-STDERR\\n')",
    ],
    cwd=str(launch_dir),
    launch_dir=launch_dir,
)
sys.stderr.write("CARRIER-PY-STDERR\n")
sys.stderr.flush()
os.write(2, b"CARRIER-FD-STDERR\n")
logging.getLogger("probe").warning("CARRIER-LOG-WARNING")
tui.wait()
"""


def test_carrier_output_leaves_the_terminal_to_the_tui(tmp_path: Any) -> None:
    """The TUI owns the terminal; the carrier's stderr must not draw over it.

    The TUI is spawned first and inherits the terminal's stderr; the carrier
    then moves its own fd 2 into the launch directory (0600), where the
    launcher can quote it when the carrier fails.
    """

    import subprocess
    import sys

    launch_dir = tmp_path / "launch"
    launch_dir.mkdir(mode=0o700)
    completed = subprocess.run(
        [sys.executable, "-c", _STDERR_PROBE, str(launch_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert "TUI-STDERR" in completed.stderr
    assert "CARRIER" not in completed.stderr
    log = launch_dir / mcp_module.DSH_CARRIER_STDERR_FILENAME
    text = log.read_text(encoding="utf-8")
    for line in ("CARRIER-PY-STDERR", "CARRIER-FD-STDERR", "CARRIER-LOG-WARNING"):
        assert line in text
    assert log.stat().st_mode & 0o777 == 0o600
