"""Target-kind honesty: classification, admission gates, and CLI rendering.

Allen: 主机 (host), user, adapter (channel route), connector (agent) are four
different object types, and ``targets`` is the delivery-promise view — a row
in it is a promise the address can receive a message.  Presence carries both
canonical connector URIs and every node's bare ``node_id`` self-token;
stamping both ``agent`` advertised nodes as connectors, and a send to one
landed in the node inbox, receipt-signed, with no consumer — a
delivered-and-receipted silent hole.

Pinned behaviors, each in both directions so a mutation cannot pass silently:

* the classifier's verdict for every shape, including an explicitly asserted
  ``unknown`` for shapes that match no known type (never an else-branch
  accident);
* targets lists classifier-verified connectors only — nodes and malformed
  shapes never appear; node visibility lives in the ``hosts`` method;
* the send-time gate rejects node-shaped and formless targets outright
  (Allen: development phase, break old scripts, facts-only errors), while
  canonical agents, registered aliases, user: and route: forms pass;
* the ``_ensure_interactive_route`` admission gate: canonical actors are
  advertised, non-canonical ones receive-but-are-not-advertised.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import close_application, owned_applications as owned_applications

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.configuration.identity import (
    DELIVERABLE_TARGET_KINDS,
    classify_target_identity,
    target_deliverable,
)
from hyprial.kernel.impl.primitives.uri import TARGET_KINDS
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxService,
    MemoryDeliveryTransport,
    ZenohInboxEndpoint,
)
from hyprial.daemon.impl.route_registration import RouteRegistrationClient, RouteRegistrationIo
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

NODE = "node-1"
OWNER = "op"
URI = f"agent:{OWNER}:{NODE}:foo"


# -- the classifier: every verdict is explicit ------------------------------


def _registered(app: DaemonApplication, name: str) -> str:
    """Lazily register a CLI sender persona (resolve-or-reject, Allen b)."""

    if app.agents.get(name) is None:
        app.handle("agent.create", {"name": name})
    return name


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # Connectors: exactly four non-empty segments.
        ("agent:bobadmin:node-hq-01.example-tailnet.ts.net:pi-dev", "agent"),
        ("agent:o:m:a", "agent"),
        # Nodes: the bare node_id self-token every daemon announces, and the
        # agent:<node> display spelling of the same shape.
        ("LAPTOP-EXAMPLE1", "host"),
        ("node-hq-01.example-tailnet.ts.net", "host"),
        ("devusr-dev", "host"),
        ("agent:LAPTOP-EXAMPLE1", "host"),
        ("agent:devusr-dev", "host"),
        # Humans and adapter-mediated route addresses.
        ("user:bobadmin", "user"),
        ("route:hq-adjutant-feishu:allen-dm", "channel_route"),
        # Unknown is a deliberate verdict for shapes no rule claims: wrong
        # segment counts, empty segments, empty names, alien schemes — and
        # channel: reply-bridge addresses, which are not delivery targets.
        ("agent:owner:machine", "unknown"),  # three segments
        ("agent:o:m:a:extra", "unknown"),  # five segments
        ("agent::machine:actor", "unknown"),  # empty owner segment
        ("agent:owner::actor", "unknown"),  # empty machine segment
        ("agent:owner:machine:", "unknown"),  # empty actor segment
        ("agent:", "unknown"),  # prefix with no name
        ("user:", "unknown"),
        ("user:owner:extra", "unknown"),
        ("route:adapter", "unknown"),  # missing route segment
        ("route::route", "unknown"),  # empty adapter segment
        ("route:a:b:c", "unknown"),
        ("channel:lark:lark-main", "unknown"),  # reply bridge, not a target
        ("channel:#ops", "unknown"),
        ("lark:oc_dm", "unknown"),  # legacy scheme
        ("foo:bar", "unknown"),  # alien scheme
        ("", "unknown"),
        ("   ", "unknown"),
    ],
)
def test_classify_target_identity(given: str, expected: str) -> None:
    assert classify_target_identity(given) == expected


@pytest.mark.parametrize(
    "value",
    [
        "agent:owner:machine",
        "agent:owner:machine:actor:extra",
        "agent:owner:machine:actor:",
        "agent::owner:machine:actor",
        "agent:owner::actor",
    ],
)
def test_agent_uri_edge_shapes_keep_the_old_unknown_verdict(value: str) -> None:
    assert classify_target_identity(value) == "unknown"


def test_every_classifier_verdict_is_a_known_kind() -> None:
    # A new rule added without extending TARGET_KINDS fails here, and the
    # deliverable set is a subset -- never the other way around.
    shapes = [
        "agent:o:m:a",
        "bare-node",
        "agent:bare-node",
        "user:o",
        "route:a:r",
        "channel:lark:x",
        "agent:o:m",
        "nonsense",
        "",
    ]
    for shape in shapes:
        kind = classify_target_identity(shape)
        assert kind in TARGET_KINDS
        assert target_deliverable(kind) is (kind in DELIVERABLE_TARGET_KINDS)


def test_only_agent_user_and_channel_route_are_deliverable() -> None:
    assert DELIVERABLE_TARGET_KINDS == {"agent", "user", "channel_route"}
    assert not target_deliverable("host")
    assert not target_deliverable("unknown")


# -- fixtures ----------------------------------------------------------------


class _StubPresence:
    def __init__(self, online: tuple[str, ...]) -> None:
        self._online = online

    def actor_online(self, actor: str) -> bool:
        return actor in self._online

    def online_actors(self) -> tuple[str, ...]:
        return self._online

    def raw_online_actors(self) -> tuple[str, ...]:
        # 桩无 interactive 层:raw 集合与过滤后集合同义。
        return self._online

    def liveness_keeps(self, actor: str) -> bool:
        # 桩无 interactive 层:复核一律保留(与 raw 同义,不改变测试语义)。
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _FakeSupervisor:
    def status(self) -> tuple[()]:
        return ()


def _application(
    request: pytest.FixtureRequest, tmp_path: Path, online: tuple[str, ...]
) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    request.addfinalizer(lambda: close_application(app))
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = _FakeSupervisor()  # type: ignore[assignment]
    app._presence = _StubPresence(online)  # type: ignore[assignment]
    return app


# -- targets lists delivery promises only ------------------------------------


def test_targets_lists_only_classifier_verified_connectors(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The mutation this kills: letting non-agent presence shapes into targets.

    Presence here carries the exact shapes the live network showed: one
    canonical connector, one bare node self-token, one malformed
    three-segment shape.  Only the connector may appear in targets.
    """

    app = _application(
        request,
        tmp_path,
        (
            "agent:bobadmin:node-hq-01.example-tailnet.ts.net:pi-dev",
            "LAPTOP-EXAMPLE1",
            "agent:owner:machine",  # malformed: three segments
        ),
    )

    rows = app.handle("targets", {})["targets"]

    assert [row["targetUri"] for row in rows] == [
        "agent:bobadmin:node-hq-01.example-tailnet.ts.net:pi-dev"
    ]
    (connector,) = rows
    assert connector["targetKind"] == "agent"
    assert connector["deliverable"] is True
    assert connector["status"] == "online"


def test_targets_kind_filter_admits_only_listable_kinds(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The filter vocabulary is exactly the kinds targets can list:
    agent/user/channel_route (delivery promises).  host/unknown are not
    delivery targets, and anything else is rejected with facts."""

    app = _application(
        request, tmp_path, ("agent:bobadmin:hq:pi-dev", "LAPTOP-EXAMPLE1")
    )

    agents = app.handle("targets", {"kind": "agent"})["targets"]
    assert [row["targetUri"] for row in agents] == ["agent:bobadmin:hq:pi-dev"]

    # Listable kinds with no rows answer honestly with an empty list.
    for kind in ("user", "channel_route"):
        assert app.handle("targets", {"kind": kind})["targets"] == []

    for kind in ("host", "unknown", "router"):
        with pytest.raises(DaemonRequestError) as failure:
            app.handle("targets", {"kind": kind})
        assert failure.value.code == "INVALID_ARGUMENT"


def test_a_registered_agent_row_stays_an_agent(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Guard against the classifier gate leaking into the registry loop."""

    app = _application(request, tmp_path, ())
    app.handle("agent.create", {"name": "foo"})

    (row,) = app.handle("targets", {})["targets"]
    assert row["targetKind"] == "agent"
    assert row["targetUri"] == URI
    assert row["deliverable"] is True
    assert row["status"] == "offline"


# -- hosts: node visibility without a delivery promise -----------------------


def test_hosts_lists_announced_nodes_and_nothing_else(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(
        request,
        tmp_path,
        (
            "agent:bobadmin:node-hq-01.example-tailnet.ts.net:pi-dev",
            "LAPTOP-EXAMPLE1",
            "devusr-dev",
            "agent:owner:machine",  # malformed: not a node, not an agent
        ),
    )

    rows = app.handle("hosts", {})["hosts"]

    assert [
        {"nodeId": row["nodeId"], "status": row["status"]} for row in rows
    ] == [
        # Mesh presence is the authority for status; freshness fields stay
        # honest (no observation yet) and are asserted separately below.
        {"nodeId": "LAPTOP-EXAMPLE1", "status": "online"},
        {"nodeId": "devusr-dev", "status": "online"},
    ]
    assert all(
        row["observedAtMs"] is None
        and row["ageMs"] is None
        and row["stale"] is True
        and row["lastError"] is None
        for row in rows
    )


def test_hosts_and_targets_partition_presence(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Every online identity shows up in exactly one of the two views."""

    online = ("agent:bobadmin:hq:pi-dev", "LAPTOP-EXAMPLE1")
    app = _application(request, tmp_path, online)

    targets = app.handle("targets", {})["targets"]
    hosts = app.handle("hosts", {})["hosts"]

    assert [row["targetUri"] for row in targets] == ["agent:bobadmin:hq:pi-dev"]
    assert [row["nodeId"] for row in hosts] == ["LAPTOP-EXAMPLE1"]


# -- the send-time admission gate --------------------------------------------


def _send(app: DaemonApplication, target: str) -> dict[str, Any]:
    return app.handle(
        "message.send", {"from": _registered(app, "driver"), "to": [target], "message": "hi", "summary": "hi"}
    )


def test_send_to_a_node_address_is_rejected_with_facts(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Rejected really is rejected: both node spellings, nothing queued."""

    app = _application(request, tmp_path, ("peer-node",))

    for spelling in ("peer-node", "agent:peer-node"):
        with pytest.raises(DaemonRequestError) as failure:
            _send(app, spelling)
        assert failure.value.code == "TARGET_IS_NODE"
        assert failure.value.data == {"target": "peer-node"}
        message = str(failure.value)
        # Facts only: what the target is and what nodes do not do.
        assert "node" in message and "not an agent" in message

    assert app._inbox.outbox_count() == 0  # type: ignore[union-attr]


def test_send_to_a_formless_target_is_rejected(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path, ())

    for shape in ("agent:three:seg", "agent:", "channel:lark:main", "foo:bar"):
        with pytest.raises(DaemonRequestError) as failure:
            _send(app, shape)
        assert failure.value.code == "UNSUPPORTED_TARGET"

    assert app._inbox.outbox_count() == 0  # type: ignore[union-attr]


def test_send_to_a_canonical_agent_is_admitted(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Admitted really is admitted: the canonical send flows unchanged."""

    app = _application(request, tmp_path, ())

    result = _send(app, "agent:her:other-host:remote")

    (delivery,) = result["deliveries"]
    assert result["ok"] is True
    assert delivery["target"] == "agent:her:other-host:remote"


def test_send_to_a_registered_alias_is_admitted(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A bare name that resolves to a registered agent is an agent address."""

    app = _application(request, tmp_path, ())
    app.handle("agent.create", {"name": "foo"})

    result = _send(app, "foo")

    (delivery,) = result["deliveries"]
    assert result["ok"] is True
    assert delivery["target"] == URI


def test_reply_to_a_node_sender_is_rejected_and_stays_pending(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """The reply path shares the gate; failure is loud, nothing is acked."""

    app = _application(request, tmp_path, ())
    app.handle("agent.create", {"name": "foo"})
    inbox = app._inbox
    assert inbox is not None
    # A legacy bare-sender row, seeded directly as if it predated the
    # ingress gate (receive() now rejects bare senders by design).
    with inbox._db:  # type: ignore[attr-defined]
        inbox._db.execute(  # type: ignore[attr-defined]
            """INSERT INTO inbox (
                   message_id, conversation_id, sender, recipient, payload, intent,
                   lifecycle, idempotency_key, created_at_ms, received_at_ms
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "m-from-node",
                "c-1",
                "peer-node",
                URI,
                json.dumps({"message": "question"}).encode(),
                "request",
                DeliveryLifecycle.DURABLE_SERVICE.value,
                None,
                1,
                1,
            ),
        )

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {"from": "foo", "messageId": "m-from-node", "message": "answer", "summary": "answer"},
        )

    assert failure.value.code == "TARGET_IS_NODE"
    # The pending message survives the refusal: no silent ack.
    assert [m.message_id for m in inbox.pending_messages(URI)] == ["m-from-node"]


# -- the _ensure_interactive_route admission gate ----------------------------


class _FakeRegistration:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeTransport:
    """Records liveliness declarations and inbox subscriptions."""

    def __init__(self) -> None:
        self.liveliness_keys: list[str] = []
        self.subscribed_keys: list[str] = []

    def declare_liveliness(self, key: str) -> _FakeRegistration:
        self.liveliness_keys.append(key)
        return _FakeRegistration()

    def subscribe(self, key: str, _callback: Any) -> _FakeRegistration:
        self.subscribed_keys.append(key)
        return _FakeRegistration()

    def close(self) -> None:
        pass


def _routable_application(tmp_path: Path) -> tuple[DaemonApplication, _FakeTransport]:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    transport = _FakeTransport()
    app._transport = transport  # type: ignore[assignment]
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._inbox = inbox
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
        transport,
        lambda _route: lambda _selector: b"ok",
        router,
        registration_factory=register,
    )
    app._route_registration = routes
    app._routes = RouteRegistrationClient(routes, router)
    app._lifecycle_router = router
    return app, transport


def _close_routable(app: DaemonApplication) -> None:
    assert app._route_registration is not None
    app._route_registration.close_registrations()
    assert app._route_registration.drain(1)
    assert app._lifecycle_router is not None
    app._lifecycle_router.close()
    assert app._agent_session_domains.close()
    assert app._inbox is not None
    app._inbox.close()


def test_interactive_route_advertises_a_canonical_actor(tmp_path: Path) -> None:
    """Admitted really is admitted: the liveliness token is declared."""

    app, transport = _routable_application(tmp_path)

    try:
        assert app._ensure_interactive_route(URI) is True  # type: ignore[attr-defined]

        assert any(key.endswith(f"/{URI}") for key in transport.liveliness_keys)
        assert any(URI in key for key in transport.subscribed_keys)
        app._close_interactive_route(URI)  # type: ignore[attr-defined]
        assert app._routes is not None
        assert app._routes.owners(URI) == ()
    finally:
        _close_routable(app)


def test_interactive_route_downgrades_a_noncanonical_actor(tmp_path: Path) -> None:
    """Rejected really is rejected: no token, but the inbox still receives.

    A pre-canonical desired-state session can restore a bare actor name.
    The gate must not advertise it into the agent liveliness keyspace (that
    keyspace feeds `targets`), while the inbox endpoint stays open so
    in-flight messages are not dropped.
    """

    app, transport = _routable_application(tmp_path)
    events: list[tuple[str, str, str]] = []

    def capture_log(level: str, component: str, event: str, **fields: Any) -> None:
        del component
        events.append((level, event, str(fields.get("targetKind"))))

    app._log = capture_log  # type: ignore[method-assign]

    try:
        ensured = app._ensure_interactive_route("legacy-bare-actor")  # type: ignore[attr-defined]
        assert ensured is True

        assert transport.liveliness_keys == []
        assert any("legacy-bare-actor" in key for key in transport.subscribed_keys)
        assert ("warn", "daemon.interactive_route.not_advertised", "host") in events
        app._close_interactive_route("legacy-bare-actor")  # type: ignore[attr-defined]
        assert app._routes is not None
        assert app._routes.owners("legacy-bare-actor") == ()
    finally:
        _close_routable(app)


# -- the CLI rendering -------------------------------------------------------


def _targets_payload() -> dict[str, object]:
    return {
        "targets": [
            {
                "targetKind": "agent",
                "targetUri": "agent:bobadmin:node-hq-01.example-tailnet.ts.net:pi-dev",
                "actor": "agent:bobadmin:node-hq-01.example-tailnet.ts.net:pi-dev",
                "status": "online",
                "deliverable": True,
            },
        ]
    }


def test_targets_default_output_is_a_table_not_a_python_repr(
    monkeypatch: Any,
) -> None:
    """The default rendering is a plain-text table in the `hyprial agent top` style."""

    monkeypatch.setattr(
        _cli_support, "_daemon_request", lambda *_a, **_k: _targets_payload()
    )
    result = CliRunner().invoke(_cli_root_app, ["message", "targets"])

    assert result.exit_code == 0
    out = result.stdout
    # No Python-repr residue: single-quoted dicts are the old Pretty output.
    assert "'targetKind'" not in out
    assert "TARGET" in out and "KIND" in out and "STATUS" in out
    assert "agent:bobadmin:node-hq-01.example-tailnet.ts.net:pi-dev" in out
    assert "1 targets" in out


def test_targets_json_output_is_valid_and_pretty_printed(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        _cli_support, "_daemon_request", lambda *_a, **_k: _targets_payload()
    )
    result = CliRunner().invoke(_cli_root_app, ["message", "targets", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)  # valid JSON, full stop
    assert payload["ok"] is True
    assert payload["targets"][0]["targetKind"] == "agent"
    # Pretty-printed: indentation means the payload spans multiple lines.
    assert "\n  " in result.stdout


def test_targets_cli_rejects_non_listable_kinds(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        _cli_support, "_daemon_request", lambda *_a, **_k: _targets_payload()
    )
    runner = CliRunner()

    # B9: the CLI filter vocabulary is agent/user only.  channel_route rows may
    # still appear unfiltered, but the filter entry moved to `adapter route list`.
    for kind in ("agent", "user"):
        assert runner.invoke(_cli_root_app, ["message", "targets", "--kind", kind]).exit_code == 0
    for kind in ("channel_route", "host", "unknown"):
        assert runner.invoke(_cli_root_app, ["message", "targets", "--kind", kind]).exit_code == 1


def test_hosts_default_and_json_output(monkeypatch: Any) -> None:
    payload = {
        "hosts": [
            {"nodeId": "node-hq-01.example-tailnet.ts.net", "status": "online"},
        ]
    }
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_a, **_k: payload)
    runner = CliRunner()

    table = runner.invoke(_cli_root_app, ["host", "list"])
    assert table.exit_code == 0
    assert "NODE" in table.stdout and "STATUS" in table.stdout
    assert "node-hq-01.example-tailnet.ts.net" in table.stdout
    assert "1 nodes" in table.stdout

    as_json = runner.invoke(_cli_root_app, ["host", "list", "--json"])
    assert as_json.exit_code == 0
    parsed = json.loads(as_json.stdout)
    assert parsed["hosts"][0]["nodeId"] == "node-hq-01.example-tailnet.ts.net"
    assert "\n  " in as_json.stdout
