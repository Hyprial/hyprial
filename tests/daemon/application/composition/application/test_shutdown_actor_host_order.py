"""Actor hosts close after every consumer that borrows them."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


def _app(tmp_path: Path) -> DaemonApplication:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    return DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="actor-host-close-order",
        hyprial_home=home,
        owner="owner",
        **_app_injections(),
    )


def _record_consumers(
    app: DaemonApplication, events: list[str], domains_seen: list[bool]
) -> None:
    domains = app._agent_session_domains
    original_domains_close = domains.close

    def close_domains(timeout: float = 5.0, **kwargs: object) -> bool:
        domains_seen.append(bool(kwargs.get("shutdown_hosts", True)))
        return original_domains_close(timeout, **kwargs)

    domains.close = close_domains  # type: ignore[method-assign]

    original_state_close = app._state_persistence.close

    def close_state(timeout: float) -> bool:
        events.append("state-persistence.close")
        return original_state_close(timeout)

    app._state_persistence.close = close_state  # type: ignore[method-assign]

    original_logger_close = app._logger.close

    def close_logger(*, timeout: float) -> bool:
        events.append("logger.close")
        return original_logger_close(timeout=timeout)

    app._logger.close = close_logger  # type: ignore[method-assign]


class _RecordingRegistry:
    """Proxy around the real registry; ActorHostRegistry forbids attribute writes."""

    def __init__(self, registry: object, events: list[str], *, fail: bool = False) -> None:
        self._registry = registry
        self._events = events
        self._fail = fail
        self.shutdown_calls = 0

    def host_for(self, owner: str):
        return self._registry.host_for(owner)  # type: ignore[attr-defined]

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.shutdown_calls += 1
        self._events.append("actor-hosts.shutdown")
        stopped = self._registry.shutdown(timeout)  # type: ignore[attr-defined]
        assert stopped is True
        return False if self._fail else True


def _install_registry(
    app: DaemonApplication, registry: _RecordingRegistry
) -> None:
    app._agent_session_domains.actor_hosts = registry  # type: ignore[assignment]
    if hasattr(app, "_actor_hosts"):
        app._actor_hosts = registry  # type: ignore[attr-defined]


def test_actor_hosts_shut_down_after_their_consumers(tmp_path: Path) -> None:
    app = _app(tmp_path)
    events: list[str] = []
    domains_seen: list[bool] = []
    registry = _RecordingRegistry(app._agent_session_domains.actor_hosts, events)
    _install_registry(app, registry)
    _record_consumers(app, events, domains_seen)
    try:
        app._close()
    finally:
        app._close_home_lease()

    assert domains_seen == [False]
    assert events.count("actor-hosts.shutdown") == 1
    shutdown_at = events.index("actor-hosts.shutdown")
    assert events.index("state-persistence.close") < shutdown_at
    assert events.index("logger.close") < shutdown_at


def test_actor_host_shutdown_failure_is_aggregated(tmp_path: Path) -> None:
    app = _app(tmp_path)
    events: list[str] = []
    domains_seen: list[bool] = []
    registry = _RecordingRegistry(
        app._agent_session_domains.actor_hosts, events, fail=True
    )
    _install_registry(app, registry)
    _record_consumers(app, events, domains_seen)
    try:
        with pytest.raises(RuntimeError, match="actor hosts did not shut down"):
            app._close()
    finally:
        app._close_home_lease()

    assert domains_seen == [False]
    assert events.count("actor-hosts.shutdown") == 1
