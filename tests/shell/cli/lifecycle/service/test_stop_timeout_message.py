"""D2 (launchd acceptance, 2026-11-16): the stop timeout never claims an unobserved socket close."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.shell.impl.cli.commands.common import daemon_stop
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 1.0)


@pytest.fixture
def timed_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    services = _cli_get_services()
    clock = Clock()
    monkeypatch.setattr(services, "time", clock)
    monkeypatch.setattr(services, "_stopped_process", lambda pid, identity: False)
    socket_path = tmp_path / "daemon.sock"
    monkeypatch.setattr(services, "_socket_path", lambda: socket_path)

    def run() -> str:
        with pytest.raises(services.CliError) as raised:
            daemon_stop._wait_daemon_teardown_receipt(
                {}, 5.0, old_pid=77, old_identity=None, require_exit=False
            )
        assert raised.value.code == "DAEMON_STOP_TIMEOUT"
        return str(raised.value)

    return socket_path, run


def test_a_socket_that_is_still_there_is_not_reported_closed(timed_out) -> None:
    socket_path, run = timed_out
    socket_path.touch()

    message = run()

    assert "the control socket is still present" in message
    assert "socket closed" not in message


def test_a_missing_socket_is_reported_as_observed_gone(timed_out) -> None:
    _socket_path, run = timed_out

    assert "the control socket is gone" in run()


def test_an_unreadable_socket_path_is_reported_as_not_checked(
    timed_out, monkeypatch: pytest.MonkeyPatch
) -> None:
    _socket_path, run = timed_out

    def broken() -> Path:
        raise RuntimeError("no state dir")

    monkeypatch.setattr(_cli_get_services(), "_socket_path", broken)

    assert "the control socket could not be checked" in run()
