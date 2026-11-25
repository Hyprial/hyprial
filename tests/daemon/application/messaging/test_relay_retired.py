"""Owner relay evidence is retired (Allen, 2026-11-19, tier-2 section 9.1 item 1).

An IM sender is simply the person who owns that IM account, so there is no
"owner said this in a DM" judgement left for the daemon to attest.  The
verification method is gone, and a ``relay`` key on ``message.send`` is treated
like any other key the method does not know: no package, no one-agent guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from tests.kernel.actors.identity.support import _registered
from tests.support.runtime._owned_applications import close_application

class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _Presence:
    """Both local actors are live, so a send lands in the peer's inbox."""

    def __init__(self, actors: tuple[str, ...]) -> None:
        self._actors = actors

    def online_actors(self) -> tuple[str, ...]:
        return self._actors

    def actor_online(self, actor: str) -> bool:
        return actor in self._actors

    def online_mailboxes(self) -> tuple[str, ...]:
        return self._actors


def _app(tmp_path: Path) -> DaemonApplication:
    state = tmp_path / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="node-1",
        owner="op",
        **_deep_module_daemon_dependencies(),
    )
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence(  # type: ignore[assignment]
        ("agent:op:node-1:driver", "agent:op:node-1:foo")
    )
    app._inbox = InboxService(  # type: ignore[attr-defined]
        state / "inbox.sqlite3",
        MemoryDeliveryTransport(),
    )
    app.handle("agent.create", {"name": "driver"})
    app.handle("agent.create", {"name": "foo"})
    return app


def _actor_uri(app: DaemonApplication, name: str) -> str:
    """Canonical URI, which is what the inbox stores as ``recipient``."""

    _registered(app, name)
    agent = app.agents.get(name)
    assert agent is not None
    return str(agent.uri)


_THREE_AGENTS = (
    "agent:op:node-1:driver",
    "agent:op:node-1:foo",
    "agent:op:node-1:bar",
)


def _multi_app(tmp_path: Path) -> DaemonApplication:
    app = _app(tmp_path)
    app.handle("agent.create", {"name": "bar"})
    app._presence = _Presence(_THREE_AGENTS)  # type: ignore[assignment]
    return app


def _outbox_payloads(app: DaemonApplication) -> list[dict[str, Any]]:
    rows = app._inbox._db.execute(  # type: ignore[attr-defined]
        "SELECT payload FROM outbox"
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_the_relay_verification_method_is_not_registered(tmp_path: Path) -> None:
    from hyprial.daemon.impl.request_catalog import MIGRATED_METHODS
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

    assert "message.relay.verify" not in METHOD_REGISTRY.names
    assert "message.relay.verify" not in MIGRATED_METHODS
    app = _app(tmp_path)
    try:
        with pytest.raises(DaemonRequestError) as unknown:
            app.handle("message.no.such.method", {"from": "driver"})
        with pytest.raises(DaemonRequestError) as retired:
            app.handle("message.relay.verify", {"from": "driver", "messageId": "m-1"})
        assert retired.value.code == unknown.value.code
    finally:
        close_application(app)


@pytest.mark.parametrize("key", ["relay", "notADeclaredKey"])
def test_a_relay_key_on_send_is_an_ordinary_unknown_key(tmp_path: Path, key: str) -> None:
    """Two agent targets used to be refused when ``relay`` was present."""

    app = _multi_app(tmp_path)
    try:
        relayer = _actor_uri(app, "driver")
        first = _actor_uri(app, "foo")
        second = _actor_uri(app, "bar")

        result = app.handle(
            "message.send",
            {
                "from": relayer,
                "to": [first, second],
                "summary": "fan-out",
                "message": "hi",
                key: {"messageId": "m-owner-1"},
            },
        )

        assert result["ok"] is True
        assert len(result["deliveries"]) == 2
        assert all("relay" not in payload for payload in _outbox_payloads(app))
    finally:
        close_application(app)


def test_the_relay_package_module_is_gone() -> None:
    import importlib.util

    assert importlib.util.find_spec("hyprial.daemon.impl.inbox.relay") is None
