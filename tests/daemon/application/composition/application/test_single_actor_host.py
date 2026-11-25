"""P7: the daemon process composes exactly one ActorHostRegistry and lends it."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.actor_custody import ActorHostCustody
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.composition import actors as composition_actors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


def _app(tmp_path: Path) -> DaemonApplication:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    return DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="single-actor-host",
        hyprial_home=home,
        owner="owner",
        **_app_injections(),
    )


def _count_registries(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    created: list[object] = []
    original = composition_actors.ActorHostRegistry.__init__

    def counting_init(registry: object, *args: object, **kwargs: object) -> None:
        created.append(registry)
        original(registry, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        composition_actors.ActorHostRegistry, "__init__", counting_init
    )
    return created


def test_application_construction_creates_exactly_one_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _count_registries(monkeypatch)
    app = _app(tmp_path)
    try:
        assert len(created) == 1
        assert app._actor_hosts is created[0]
        assert app._agent_session_domains.actor_hosts is app._actor_hosts
    finally:
        try:
            app._close()
        finally:
            app._close_home_lease()


def test_wired_consumers_borrow_the_daemon_host(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        host = app._actor_hosts.host_for("p7-probe")
        assert app._actor_hosts.host_for("another-owner") is host
        # Wired consumers hold the same host object, not a private engine.
        assert app._blocking_failures._actor_host is host
        assert app._restore_policy._actor_host is host
        assert app._agent_session_domains.actor_hosts is app._actor_hosts

        # A custody given the daemon host borrows it and never closes it.
        borrowed = ActorHostCustody(host, owner="p7-probe")
        assert borrowed.owned is False
        assert borrowed.shutdown(0.1) is True
        assert app._actor_hosts.host_for("p7-probe") is host
    finally:
        try:
            app._close()
        finally:
            app._close_home_lease()
