"""Host ready budget per driver, the slow-start notice and a host that ends on its own."""

from __future__ import annotations

import queue
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyprial.shell.impl.gui.backend import lifecycle
from hyprial.shell.impl.gui.backend import source as backend_source
from hyprial.shell.impl.gui.errors import GuiError
from tests.shell.gui.backend.test_source_session import (
    GRANT,
    MOCK_TOKEN,
    FakeProcess,
    _plan,
    _ready_line,
    _Response,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _LateLines:
    """A captured stream that delivers one line once the fake clock reaches ``at``."""

    def __init__(self, clock: _Clock, at: float | None, line: str = "") -> None:
        self.clock = clock
        self.at = at
        self.line = line

    def get(self, timeout: float | None = None) -> Any:
        if self.at is not None and self.clock.now >= self.at:
            return self.line
        raise queue.Empty


def _start_with_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    driver: str,
    announce_at: float | None,
    notices: list[float],
) -> _Clock:
    plan = _plan(tmp_path, driver=driver)
    clock = _Clock()

    def spawn(active, argv, *, name, **kwargs):
        lines: Any
        if name == "contract-mock":
            lines = queue.Queue()
            lines.put(("endpoint", "endpoint: http://127.0.0.1:4519/daemon/request/<method>\n"))
            lines.put(("token", MOCK_TOKEN))
        else:
            lines = _LateLines(clock, announce_at, _ready_line(active))
        return lifecycle.GuiChild(name, FakeProcess(301), None, lines)

    monkeypatch.setattr(lifecycle.process, "_spawn_captured", spawn)
    monkeypatch.setattr(backend_source, "generate_launch_grant", lambda: GRANT)
    session = lifecycle.start_session(
        plan,
        run_build=lambda *args, **kwargs: None,
        wait_for_http=lambda *args, **kwargs: None,
        terminate_child=lambda child: None,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        urlopen=lambda url, timeout: _Response(
            {"ok": True, "dataMode": "hosted", "assets": "installed", "assetsVerified": True}
        ),
        on_backend_wait=lambda: notices.append(clock.now),
        popen=lambda *args, **kwargs: pytest.fail("fake Popen must not be used"),
    )
    session.stop()
    return clock


def test_a_slow_host_start_is_announced_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notices: list[float] = []

    clock = _start_with_host(
        monkeypatch, tmp_path, driver="daemon", announce_at=40.0, notices=notices
    )

    assert 40.0 <= clock.now < 40.5
    assert len(notices) == 1 and 5.0 <= notices[0] < 5.5


def test_a_fast_host_start_is_not_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notices: list[float] = []

    _start_with_host(monkeypatch, tmp_path, driver="daemon", announce_at=1.0, notices=notices)

    assert notices == []


def test_a_daemon_host_waits_for_the_sixty_second_budget_and_the_mock_host_for_thirty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notices: list[float] = []

    clock = _start_with_host(
        monkeypatch, tmp_path / "daemon", driver="daemon", announce_at=59.0, notices=notices
    )
    assert 59.0 <= clock.now < 59.5

    with pytest.raises(GuiError) as caught:
        _start_with_host(
            monkeypatch, tmp_path / "mock", driver="contract-mock", announce_at=59.0, notices=[]
        )
    assert caught.value.code == "GUI_READY_TIMEOUT"
    assert caught.value.data["stages_seconds"]["failed"] < 31.0


WATCHER_SCRIPT = f"""
import sys
print(sys.argv[1], flush=True)
print('{{"event":"watcher_failed","detail":"bearer={MOCK_TOKEN} grant={GRANT}"}}', flush=True)
sys.exit(3)
"""


def _scripted_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    script: str,
    reports: list[tuple[int, str]],
) -> tuple[Any, Any]:
    base = _plan(tmp_path, driver="daemon")
    ready = _ready_line(base)
    plan = replace(base, frontend_argv=(sys.executable, "-c", script, ready))
    plan.project.home.mkdir(parents=True)
    monkeypatch.setattr(backend_source, "generate_launch_grant", lambda: GRANT)
    real_spawn = lifecycle.process._spawn_captured

    def spawn(active, argv, **kwargs):
        child = real_spawn(active, argv, **kwargs)
        child.redactions.add(MOCK_TOKEN)
        return child

    monkeypatch.setattr(lifecycle.process, "_spawn_captured", spawn)
    session = lifecycle.start_session(
        plan,
        run_build=lambda *args, **kwargs: None,
        wait_for_http=lambda *args, **kwargs: None,
        urlopen=lambda url, timeout: _Response(
            {"ok": True, "dataMode": "hosted", "assets": "installed", "assetsVerified": True}
        ),
        popen=subprocess.Popen,
        on_host_exit=lambda code, tail: reports.append((code, tail)),
    )
    return plan, session


def test_a_host_that_exits_after_its_ready_line_returns_its_status_and_is_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reports: list[tuple[int, str]] = []
    plan, session = _scripted_session(monkeypatch, tmp_path, WATCHER_SCRIPT, reports)
    try:
        assert session.wait() == 3
        assert (plan.runtime_dir / "host.log").exists()
    finally:
        session.stop()

    assert len(reports) == 1
    code, tail = reports[0]
    assert code == 3
    assert "watcher_failed" in tail
    assert MOCK_TOKEN not in tail and GRANT not in tail
    assert not (plan.runtime_dir / "host.log").exists()


def test_a_host_that_exits_zero_is_not_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reports: list[tuple[int, str]] = []
    script = "import sys\nprint(sys.argv[1], flush=True)\nsys.exit(0)\n"
    _plan_, session = _scripted_session(monkeypatch, tmp_path, script, reports)
    try:
        assert session.wait() == 0
    finally:
        session.stop()

    assert reports == []


def test_a_user_stop_while_the_host_runs_is_not_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reports: list[tuple[int, str]] = []
    script = "import sys, time\nprint(sys.argv[1], flush=True)\ntime.sleep(60)\n"
    _plan_, session = _scripted_session(monkeypatch, tmp_path, script, reports)

    def interrupted(_: float) -> None:
        raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            session.wait(sleep=interrupted)
    finally:
        session.stop()

    assert reports == []


def test_a_host_that_ends_cleanly_is_not_an_error(tmp_path: Path) -> None:
    session = lifecycle.GuiSession(
        plan=_plan(tmp_path, driver="daemon"),
        frontend=lifecycle.GuiChild("frontend", FakeProcess(302, 0), None),
        backend=None,
    )

    assert session.wait(sleep=lambda _: None) == 0
