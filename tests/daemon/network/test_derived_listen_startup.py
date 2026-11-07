"""A best-effort derived listener must never become a startup dependency.

Since the tailnet cutover (2026-10-03) ``_derive_listen_endpoint`` is an
always-None seam -- there is no host tailscale to derive from.  What these
tests still pin is the fallback budget around the seam: a derived listener
that fails to bind is dropped and the session reopens outbound-only, while a
*configured* listener keeps every failure loud.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import socket

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


def _application(tmp_path, monkeypatch, *, listen: tuple[str, ...] = ()):
    monkeypatch.delenv("HYPRIAL_NETWORK_ISOLATED", raising=False)
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="derived-listen-test",
        hyprial_home=tmp_path / "home",
        owner="tester",
        zenoh_listen=listen,
    **_deep_module_daemon_dependencies())
    monkeypatch.setattr(app, "_discover_peer_endpoints", lambda: ())
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    return app, events


def test_the_seam_itself_derives_nothing() -> None:
    """No host tailscale means nothing to derive, for any daemon."""

    assert DaemonApplication._derive_listen_endpoint(
        object()  # type: ignore[arg-type]
    ) is None


def test_derived_real_port_bind_failure_reopens_outbound_only(
    tmp_path, monkeypatch
) -> None:
    """The port-zero probe can pass even when the eventual real port is held."""

    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    endpoint = f"tcp/127.0.0.1:{held.getsockname()[1]}"
    monkeypatch.setattr(
        DaemonApplication, "_derive_listen_endpoint", lambda self: endpoint
    )
    app, events = _application(tmp_path, monkeypatch)
    try:
        app._start_runtime()
        assert app._transport is not None
        assert app.zenoh_listen == ()
    finally:
        app._close()
        held.close()

    unavailable = [
        fields
        for event, fields in events
        if event == "zenoh.listen.derived_unavailable"
    ]
    assert len(unavailable) == 1
    assert unavailable[0]["endpoint"] == endpoint
    assert unavailable[0]["error"]
    assert unavailable[0]["reason"] == "DERIVED_LISTEN_OPEN_FAILED"
    assert [event for event, _fields in events if event == "zenoh.listen.absent"]


def test_configured_real_port_bind_failure_still_fails(tmp_path, monkeypatch) -> None:
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    endpoint = f"tcp/127.0.0.1:{held.getsockname()[1]}"
    app, _events = _application(tmp_path, monkeypatch, listen=(endpoint,))
    try:
        with pytest.raises(Exception, match="Address already in use"):
            app._start_runtime()
    finally:
        app._close()
        held.close()
