"""Persona network presence: the daemon backs every registered agent's URI.

`agent create` writes a registry row but runs no process — before this,
the URI had no zenoh presence at all, so a remote node's `targets` never
showed the persona and cross-node delivery to it could not start (the
docker two-machine failure in #196's acceptance).  Registration is the
daemon's delivery promise (#195: self-node URIs are locally deliverable);
this is the cross-node twin — the daemon declares the actor liveliness
token and holds the inbox endpoint itself.  The promise dies with its
backer: daemon down, token gone.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.route_registration import (
    RouteRegistrationClient,
    RouteRegistrationIo,
)
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.links.zenoh_link import encode_delivery_frame
from hyprial.daemon.impl.inbox import ZenohInboxEndpoint
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

NODE = "node-1"
OWNER = "op"
URI = f"agent:{OWNER}:{NODE}:persona"


class _FakeRegistration:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubHarnesses:
    def status(self) -> tuple[()]:
        return ()


class _StubPresence:
    def actor_online(self, actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _FakeTransport:
    """Records liveliness declarations and inbox subscriptions."""

    def __init__(self) -> None:
        self.liveliness_keys: list[str] = []
        self.subscriptions: dict[str, Any] = {}
        self.registrations: list[_FakeRegistration] = []

    def declare_liveliness(self, key: str) -> _FakeRegistration:
        registration = _FakeRegistration()
        self.liveliness_keys.append(key)
        self.registrations.append(registration)
        return registration

    def subscribe(self, key: str, callback: Any) -> _FakeRegistration:
        registration = _FakeRegistration()
        self.subscriptions[key] = callback
        self.registrations.append(registration)
        return registration

    def close(self) -> None:
        pass


def _application(tmp_path: Path) -> tuple[DaemonApplication, _FakeTransport, InboxService]:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    transport = _FakeTransport()
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._transport = transport  # type: ignore[assignment]
    app._inbox = inbox
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _StubPresence()  # type: ignore[assignment]
    router = CorrelationEventRouter()

    def register(spec: Any) -> tuple[Any, Any]:
        token = (
            transport.declare_liveliness(spec.liveliness_key)
            if spec.advertise
            else None
        )
        return token, ZenohInboxEndpoint(
            transport, inbox, spec.route_id, declare_receipts=False
        )

    routes = RouteRegistrationIo(
        transport, lambda _route: lambda _selector: b"ok", router,
        registration_factory=register,
    )
    app._route_registration = routes
    app._routes = RouteRegistrationClient(routes, router)
    app._lifecycle_router = router
    return app, transport, inbox


def _close_application(app: DaemonApplication, inbox: InboxService) -> None:
    routes = app._route_registration
    if routes is not None:
        routes.close_registrations()
        assert routes.drain(1)
    if app._lifecycle_router is not None:
        app._lifecycle_router.close()
    assert app._agent_session_domains.close()
    inbox.close()


def test_agent_create_declares_daemon_backed_presence(tmp_path: Path) -> None:
    """The mutation this kills: create without the liveliness declaration."""

    app, transport, inbox = _application(tmp_path)
    try:
        app.handle("agent.create", {"name": "persona"})

        assert any(key.endswith(f"/{URI}") for key in transport.liveliness_keys)
        # The inbox endpoint is subscribed, so a delivery to the persona URI
        # is actually taken — presence without a receiver would be a lie.
        assert any(URI in key for key in transport.subscriptions)
    finally:
        _close_application(app, inbox)


def test_persona_endpoint_takes_delivery_into_the_durable_inbox(
    tmp_path: Path,
) -> None:
    """The full receive path through the persona's endpoint callback."""

    app, transport, inbox = _application(tmp_path)
    try:
        app.handle("agent.create", {"name": "persona"})
        (callback,) = transport.subscriptions.values()
        message = InboxMessage(
            message_id="m-persona",
            conversation_id="c-1",
            sender="agent:op:node-2:sender",
            recipient=URI,
            payload=json.dumps({"message": "hi"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )

        class _Sample:
            payload = encode_delivery_frame(message)

        callback(_Sample())

        assert [m.message_id for m in inbox.pending_messages(URI)] == ["m-persona"]
    finally:
        _close_application(app, inbox)


def test_agent_destroy_withdraws_the_presence(tmp_path: Path) -> None:
    """The promise leaves when the registry row leaves."""

    app, transport, inbox = _application(tmp_path)
    try:
        app.handle("agent.create", {"name": "persona"})
        declared = list(transport.registrations)

        app.handle("agent.destroy", {"name": "persona"})

        assert declared and all(registration.closed for registration in declared)
    finally:
        _close_application(app, inbox)


def test_restart_sweep_reddeclares_every_registered_agent(tmp_path: Path) -> None:
    """Presence is rebuilt from the registry at startup, not from memory."""

    app, transport, inbox = _application(tmp_path)
    app.handle("agent.create", {"name": "persona"})
    app.handle("agent.create", {"name": "other"})
    _close_application(app, inbox)

    # A fresh daemon generation over the same state dir: the registry
    # persists, the in-memory routes do not.
    app2, transport2, inbox2 = _application(tmp_path)
    try:
        assert transport2.liveliness_keys == []
        app2._restore_persona_routes()  # type: ignore[attr-defined]

        declared = set(transport2.liveliness_keys)
        assert any(key.endswith(f"/{URI}") for key in declared)
        assert any(key.endswith(f"/agent:{OWNER}:{NODE}:other") for key in declared)
        # Idempotent: a second sweep does not double-declare.
        transport2.liveliness_keys.clear()
        app2._restore_persona_routes()  # type: ignore[attr-defined]
        assert transport2.liveliness_keys == []
    finally:
        _close_application(app2, inbox2)


def test_create_without_transport_is_a_noop_not_a_crash(tmp_path: Path) -> None:
    """Unit-level handle() seams never start transport; declaration skips."""

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._inbox = inbox
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _StubPresence()  # type: ignore[assignment]
    try:
        result = app.handle("agent.create", {"name": "persona"})
        assert result["created"] is True
        assert app._routes is None
    finally:
        assert app._agent_session_domains.close()
        inbox.close()


def test_ps_pending_messages_is_node_wide_not_node_scoped(tmp_path: Path) -> None:
    """The runner-observation blind spot, pinned: a row waiting under a
    registered agent's URI must appear in ps.pendingMessages.

    The mutation this kills: scoping ps back to the node inbox
    (pending_messages(self.node_id)) — which reports an empty node while
    rows wait under agent URIs, the exact misread that made a working
    cross-node delivery look like a delivery failure.
    """

    app, _transport, inbox = _application(tmp_path)
    app._harnesses = _StubHarnesses()  # type: ignore[assignment]
    try:
        app.handle("agent.create", {"name": "persona"})
        inbox.receive(
            InboxMessage(
                message_id="m-under-agent",
                conversation_id="c-1",
                sender="agent:op:node-2:sender",
                recipient=URI,
                payload=b'{"message":"hi"}',
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=1,
            )
        )

        ps = app.handle("ps", {})

        assert [m["messageId"] for m in ps["pendingMessages"]] == ["m-under-agent"]
        assert ps["pendingMessages"][0]["recipient"] == URI
    finally:
        _close_application(app, inbox)


def test_targets_status_is_the_runtime_verdict_not_the_presence_promise(
    tmp_path: Path,
) -> None:
    """status = running?, deliverable = sendable? — the two axes stay apart.

    A registered persona with no connector is daemon-backed presence:
    deliverable, but NOT running.  The registry verdict must override the
    presence row's raw "online", or a downed agent reads as alive (the
    E2E-013 teardown misread).
    """

    app, transport, inbox = _application(tmp_path)

    class _Presence:
        def __init__(self, online: tuple[str, ...]) -> None:
            self._online = online

        def actor_online(self, actor: str) -> bool:
            return actor in self._online

        def online_actors(self) -> tuple[str, ...]:
            return self._online

        def online_mailboxes(self) -> tuple[str, ...]:
            return ()

    # Presence (the daemon-backed token) reports the persona online...
    app._presence = _Presence((URI,))  # type: ignore[assignment]
    try:
        app.handle("agent.create", {"name": "persona"})

        rows = {row["targetUri"]: row for row in app.handle("targets", {})["targets"]}

        # ...but the registry knows no connector runs: offline AND deliverable.
        assert rows[URI]["status"] == "offline"
        assert rows[URI]["deliverable"] is True
    finally:
        _close_application(app, inbox)
