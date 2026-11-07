"""Status must name a session that is closed even though the daemon answers.

zenoh 1.9 closes a session before it raises from ``close()`` (a timeout does
exactly that), so "the transport object exists" never proved "the session is
usable".  The 2026-10-17 production failure kept answering ``ps`` with no
session field while every declare and publish on the installed session raised
``session closed``.  These pin the status projection the daemon exposes for
``ps`` and ``doctor``; the reopen machinery is pinned in
``tests/daemon/transport/reconfigure``.
"""

from __future__ import annotations

import socket
import time

from hyprial.daemon.impl.transport.zenoh import transport as transport_module
from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig
from hyprial.daemon.impl.transport.zenoh.transport import ZenohTransport


def _endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


class _DeadSession:
    """A session that zenoh already closed but the transport still holds."""

    def is_closed(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def zid(self) -> str:
        return "dead"


def _transport() -> ZenohTransport:
    transport = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = transport._session
    original.close()
    transport._session = _DeadSession()
    return transport


def test_a_native_closed_session_is_reported_as_closed() -> None:
    transport = _transport()
    try:
        health = transport.session_health()
        assert health["state"] == "closed"
        assert health["closed"] is True
    finally:
        transport.close()


def test_a_rebuild_in_flight_is_not_reported_as_healthy_or_closed() -> None:
    transport = _transport()  # the old, already closed session is still installed
    transport._session_state = "open"
    transport._rebuilding = (None, "test-rebuild", time.monotonic())
    try:
        health = transport.session_health()
        assert health["state"] == "rebuilding"
        assert health["rebuilding"] is True
    finally:
        transport._rebuilding = None
        transport._session_state = "closed"
        transport.close()


def test_a_degraded_session_reports_its_recovery_attempts() -> None:
    transport = _transport()
    transport._session = None
    transport._session_state = "degraded"
    transport._degraded = True
    transport._recovery_attempts = 3
    transport._recovery_last_error = "TimeoutError: open timed out"
    try:
        health = transport.session_health()
        assert health["state"] == "degraded"
        assert health["closed"] is True
        assert health["recoveryAttempts"] == 3
        assert health["recoveryLastError"] == "TimeoutError: open timed out"
    finally:
        transport._session_state = "closed"
        transport.close()


class _LiveSession:
    def is_closed(self) -> bool:
        return False

    def close(self) -> None:
        return None

    def zid(self) -> str:
        return "live"


def test_rebuilding_since_is_fresh_on_every_entry(monkeypatch) -> None:
    """The rebuild clock restarts on each entry and clears on open.

    A carried-over timestamp from an earlier rebuild would make doctor warn
    immediately on a healthy second rebuild.
    """

    import threading

    transport = _transport()
    replaced = _LiveSession()
    monkeypatch.setattr(transport_module.zenoh, "open", lambda _config: replaced)
    try:
        transport.reconfigure_connect(())
        assert transport.session_health()["rebuildingSinceMs"] is None

        transport._rebuilding_since_ms = 123  # stale value from an older entry
        entered, release = threading.Event(), threading.Event()

        def slow_open(_config):
            entered.set()
            assert release.wait(3)
            return replaced

        monkeypatch.setattr(transport_module.zenoh, "open", slow_open)
        worker = threading.Thread(target=transport.reconfigure_connect, args=((),))
        worker.start()
        try:
            assert entered.wait(3)
            deadline = time.monotonic() + 3
            health = transport.session_health()
            while health["state"] != "rebuilding" and time.monotonic() < deadline:
                time.sleep(0.005)
                health = transport.session_health()
            assert health["state"] == "rebuilding"
            assert isinstance(health["rebuildingSinceMs"], int)
            assert health["rebuildingSinceMs"] > 123
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive()
        assert transport.session_health()["state"] == "open"
        assert transport.session_health()["rebuildingSinceMs"] is None
    finally:
        transport.close()
