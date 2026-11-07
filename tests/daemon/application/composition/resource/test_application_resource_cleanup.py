"""Independent production composition close proof; no owned-app fixture import."""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from tests.conftest import _fd_set
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox.authority.facade import DeliveryCustodyFacade


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


def _loopback_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


def _open_real_application(root: Path) -> DaemonApplication:
    home = root / "home"
    home.mkdir()
    state = root / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="cleanup-node",
        hyprial_home=home,
        owner="cleanup-owner",
        zenoh_listen=(_loopback_endpoint(),),
        zenoh_connect=(),
        **_app_injections(),
    )
    app.network_isolated = True
    assert app.network_isolated and app.zenoh_connect == ()
    try:
        app._start_runtime()
    except BaseException:
        try:
            app._close()
        finally:
            app._close_home_lease()
        raise
    assert isinstance(app._inbox, DeliveryCustodyFacade)
    assert app._transport is not None
    assert app.desired_state.load().harnesses == ()
    return app


def _owned_snapshot() -> tuple[set[int], set[int]]:
    threads = {id(thread) for thread in threading.enumerate() if thread.is_alive()}
    return _fd_set(), threads


def _assert_returned(baseline: tuple[set[int], set[int]]) -> None:
    before_fds, before_threads = baseline
    added_fds = _fd_set() - before_fds
    added_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and id(thread) not in before_threads
    ]
    assert not added_fds and not added_threads, (
        f"application retained fd={sorted(added_fds)} threads={added_threads}"
    )


def test_real_application_closes_inbox_graph_threads_and_descriptors(
    tmp_path: Path,
) -> None:
    baseline = _owned_snapshot()
    app = _open_real_application(tmp_path)
    try:
        assert app._agent_session_domains is not None
        assert app._inbox is not None
    finally:
        try:
            app._close()
        finally:
            app._close_home_lease()
    _assert_returned(baseline)


def test_resource_assertion_detects_skipped_agent_domain_then_owner_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _owned_snapshot()
    app = _open_real_application(tmp_path)
    domains = app._agent_session_domains
    assert domains is not None
    try:
        # The mutant lies about one owned domain closing. The independent
        # resource assertion must reject it before we restore that owner.
        with monkeypatch.context() as patch:
            patch.setattr(domains, "close", lambda timeout=5.0: True)
            try:
                app._close()
            finally:
                app._close_home_lease()
        with pytest.raises(AssertionError, match="application retained"):
            _assert_returned(baseline)
    finally:
        # Cleanup uses the real domain owner; never hard-close observed FDs.
        assert domains.close()
    _assert_returned(baseline)
