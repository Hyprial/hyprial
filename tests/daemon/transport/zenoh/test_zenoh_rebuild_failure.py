"""A redial that fails must name the cause that explains why it failed.

``zenoh.forwarding.redial_failed`` logs only ``str(error)``.  On 2026-10-17 the
detail said "session closed" -- the close failure -- while the failure that
mattered (the new endpoint set could not open) was not in the message.  The
transport composes the requested-config failure, the fallback failure and the
old-close failure into one message, in that order, so the first clause is the
reason the redial did not take effect.
"""

from __future__ import annotations

import socket

import pytest

from hyprial.daemon.impl.transport.zenoh import transport as transport_module
from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig
from hyprial.daemon.impl.transport.zenoh.transport import ZenohTransport


def _endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


class _FakeSession:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_error = close_error

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error

    def is_closed(self) -> bool:
        return self.close_error is not None

    def zid(self) -> str:
        return "fake"


def test_every_failed_open_is_named_in_the_rebuild_error(monkeypatch) -> None:
    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    a._session = _FakeSession(close_error=RuntimeError("session closed"))
    opens: list[str] = []

    def fake_open(_config):
        opens.append("candidate" if len(opens) == 0 else "fallback")
        raise RuntimeError(f"{opens[-1]} open refused")

    monkeypatch.setattr(transport_module.zenoh, "open", fake_open)
    try:
        with pytest.raises(RuntimeError) as caught:
            a.reconfigure_connect(("tcp/127.0.0.1:1",))
        message = str(caught.value)
        assert "requested open" in message
        assert "candidate open refused" in message.split(";")[0]
        assert "fallback open refused" in message
        assert "session closed" in message
        assert caught.value.__cause__ is not None
        assert "candidate open refused" in str(caught.value.__cause__)
        assert a._session is None
        assert a.session_health()["state"] == "degraded"
    finally:
        a.close()


def test_a_restored_fallback_still_names_the_requested_endpoint_failure(
    monkeypatch,
) -> None:
    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    dead = _FakeSession(close_error=RuntimeError("session closed"))
    restored = _FakeSession()
    a._session = dead
    opens: list[str] = []

    def fake_open(_config):
        opens.append("candidate" if len(opens) == 0 else "fallback")
        if len(opens) == 1:
            raise RuntimeError("candidate open refused")
        return restored

    monkeypatch.setattr(transport_module.zenoh, "open", fake_open)
    try:
        with pytest.raises(RuntimeError, match="candidate open refused"):
            a.reconfigure_connect(("tcp/127.0.0.1:1",))
        assert a._session is restored
        assert a.session_health()["state"] == "open"
    finally:
        a.close()
