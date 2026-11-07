"""Full-link sender identity honesty (Allen A+B): canonical on the wire out,
canonical-or-rejected at ingress in.

A: the daemon send boundary canonicalizes every unfenced sender (bare alias
→ the registered agent's URI; unresolved bare name → the locally minted
``agent:<owner>:<node>:<name>``) before anything reaches the wire, reusing
the one targeting pipeline (``normalize_agent_recipient`` +
``_resolve_agent_alias``) — never a second resolver.  Sender resolution is
deliberately local-only: presence spans remote actors, and resolving a local
sender to a remote identity would mint an impersonating message.

B: once no legitimate path can author a bare-sender message, the inbox
ingress rejects one outright — no commit, so no receipt is ever signed —
and a deterministic system notice tells the origin node why.  Every
committed row carries the transport-stamped ``origin_node`` so the physical
source survives even a bypassing client.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path
from typing import Any

import pytest

from typer.testing import CliRunner

from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    ZenohDeliveryTransport,
    decode_delivery_frame,
    encode_delivery_frame,
)
from tests.support.links.gate import PermissiveLinkGate
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

NODE = "node-1"
OWNER = "op"
URI = f"agent:{OWNER}:{NODE}:foo"


class _StubPresence:
    def __init__(self, online: tuple[str, ...] = ()) -> None:
        self._online = online

    def actor_online(self, actor: str) -> bool:
        return actor in self._online

    def online_actors(self) -> tuple[str, ...]:
        return self._online

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _RecordingTransport(MemoryDeliveryTransport):
    """Memory transport that keeps the full message, not just the id."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[InboxMessage] = []

    def deliver(self, message: InboxMessage) -> bool:
        self.frames.append(message)
        return super().deliver(message)


def _application(
    tmp_path: Path,
    transport: _RecordingTransport,
    online: tuple[str, ...] = (),
) -> DaemonApplication:
    app = DaemonApplication(
        **daemon_dependencies(),
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    )
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", transport)
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _StubPresence(online)  # type: ignore[assignment]
    # Explicit premise (wp2.md §4.9 option a): this architecture gate measures
    # sender canonicalization, not link admission, so a permissive gate keeps
    # every admitted send reaching the wire instead of holding (ruling 54).
    app.link_admission.bind(PermissiveLinkGate())
    return app


def _send(app: DaemonApplication, source: str, target: str) -> dict[str, Any]:
    return app.handle(
        "message.send", {"from": source, "to": [target], "message": "hi", "summary": "hi"}
    )


def _message(message_id: str, sender: str, **kwargs: Any) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c-1",
        sender=sender,
        recipient=URI,
        payload=b'{"message":"hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        **kwargs,
    )


# -- A: sender canonicalization at the daemon send boundary ------------------


def test_bare_alias_sender_becomes_the_registered_canonical_uri(
    tmp_path: Path,
) -> None:
    transport = _RecordingTransport()
    transport.set_online(URI, True)
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})

    result = _send(app, "foo", URI)

    assert result["ok"] is True
    (frame,) = transport.frames
    assert frame.sender == URI  # on the wire: four segments, never bare


def test_unresolved_bare_sender_is_rejected_with_facts(tmp_path: Path) -> None:
    """Allen (b): resolve-or-reject — no registry row, no send, no minting.

    The mutation this kills: silently minting (or passing through) an
    unbacked identity.  Facts-only error, nothing queued, nothing sent.
    """

    transport = _RecordingTransport()
    transport.set_online(URI, True)
    app = _application(tmp_path, transport)

    with pytest.raises(DaemonRequestError) as failure:
        _send(app, "ad-hoc-ops", URI)

    assert failure.value.code == "SENDER_UNRESOLVED"
    assert "ad-hoc-ops" in str(failure.value)
    assert "not a registered agent" in str(failure.value)
    assert transport.frames == []
    assert app._inbox.outbox_count() == 0  # type: ignore[union-attr]


def test_sender_alias_never_resolves_to_a_remote_presence_actor(
    tmp_path: Path,
) -> None:
    """Presence spans REMOTE actors; a local sender must stay local.

    The mutation this kills: sender resolution consulting presence and
    thereby authoring messages that impersonate a remote node's actor.
    """

    remote = f"agent:{OWNER}:peer-host:bob"
    transport = _RecordingTransport()
    transport.set_online(URI, True)
    app = _application(tmp_path, transport, online=(remote,))

    # "bob" is unregistered locally: rejected — never resolved to the
    # remote actor, never minted, never sent.
    with pytest.raises(DaemonRequestError) as failure:
        _send(app, "bob", URI)
    assert failure.value.code == "SENDER_UNRESOLVED"
    assert transport.frames == []


def test_bridge_senders_pass_and_foreign_identities_are_rejected(
    tmp_path: Path,
) -> None:
    transport = _RecordingTransport()
    transport.set_online(URI, True)
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})

    # Lark inbound forwards carry the reply-bridge address as sender.
    _send(app, "channel:lark:main", URI)
    assert [frame.sender for frame in transport.frames] == ["channel:lark:main"]

    # A canonical URI naming a FOREIGN node is not this node's identity:
    # sending with it would impersonate.  Rejected with facts.
    with pytest.raises(DaemonRequestError) as failure:
        _send(app, f"agent:{OWNER}:node-2:worker", URI)
    assert failure.value.code == "SENDER_UNRESOLVED"

    # A malformed agent: URI is rejected as an argument, not as identity.
    with pytest.raises(DaemonRequestError) as failure:
        _send(app, "agent:three:seg", URI)
    assert failure.value.code == "INVALID_ARGUMENT"


def test_local_canonical_registered_sender_passes(tmp_path: Path) -> None:
    """A canonical URI naming THIS node and a registered agent resolves."""

    transport = _RecordingTransport()
    transport.set_online(URI, True)
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})

    _send(app, URI, URI)

    (frame,) = transport.frames
    assert frame.sender == URI


def test_status_read_keys_by_the_canonical_sender(tmp_path: Path) -> None:
    """A bare --from must find the records its canonicalized sends wrote."""

    transport = _RecordingTransport()
    transport.set_online(URI, True)
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})

    sent = _send(app, "foo", URI)
    status = app.handle(
        "message.status", {"from": "foo", "messageId": sent["messageId"]}
    )

    # The unconfirmed loopback leaves the message pending — found under the
    # canonical sender key ("unknown" would mean the read missed it).
    assert status["state"] == "pending"


# -- B: the ingress gate -----------------------------------------------------


def test_ingress_rejects_a_bare_sender_without_commit(tmp_path: Path) -> None:
    """Rejected really is rejected: no row, so no receipt can ever be signed."""

    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        result = inbox.receive(_message("m-bare", "onboarding-check"))

        assert result.accepted is False
        assert result.acknowledged is False
        assert result.code == "SENDER_NOT_CANONICAL"
        assert inbox.pending_messages(URI) == ()
        # has_received backs the receipt queryable: unsigned, by construction.
        assert inbox.has_received("m-bare") is False
    finally:
        inbox.close()


def test_ingress_rejection_notices_the_origin_node_once(tmp_path: Path) -> None:
    """The refusal is never silent: a deterministic notice states why.

    The notice id derives from the rejected message id, so the sender's
    unreceipted retry storm dedups at the receiving node instead of piling
    up notices.
    """

    transport = _RecordingTransport()
    inbox = InboxService(
        tmp_path / "inbox.sqlite3", transport, node_id="receiver-node"
    )
    try:
        rejected = inbox.receive(
            _message("m-bare", "onboarding-check", origin_node="origin-node")
        )
        retried = inbox.receive(
            _message("m-bare", "onboarding-check", origin_node="origin-node")
        )
        assert rejected.code == retried.code == "SENDER_NOT_CANONICAL"

        # Each rejection attempt offers a notice to the origin node...
        assert [node for node, _notice in transport.notices] == [
            "origin-node",
            "origin-node",
        ]
        first, second = (notice for _node, notice in transport.notices)
        body = json.loads(first.payload)
        assert body["reason"] == "SENDER_NOT_CANONICAL"
        assert body["originalMessageId"] == "m-bare"
        assert "onboarding-check" in body["message"]
        # ...with one deterministic id, so the origin node stores it once.
        assert first.message_id == second.message_id
        origin_inbox = InboxService(
            tmp_path / "origin.sqlite3", MemoryDeliveryTransport()
        )
        try:
            assert origin_inbox.receive_system_notice(first) is True
            assert origin_inbox.receive_system_notice(second) is True
            assert len(origin_inbox.system_notices("onboarding-check")) == 1
        finally:
            origin_inbox.close()
    finally:
        inbox.close()


def test_ingress_rejection_falls_back_to_an_online_bare_sender_node(
    tmp_path: Path,
) -> None:
    """Pre-stamp peers carry no origin; their bare sender IS the node id."""

    transport = _RecordingTransport()
    transport.set_online("old-node", True)
    inbox = InboxService(tmp_path / "inbox.sqlite3", transport, node_id=NODE)
    try:
        inbox.receive(_message("m-old", "old-node"))  # no origin stamp

        (node, _notice) = transport.notices[0]
        assert node == "old-node"
    finally:
        inbox.close()


def test_ingress_rejection_with_no_reachable_origin_is_stored_locally(
    tmp_path: Path,
) -> None:
    transport = _RecordingTransport()  # nothing online
    inbox = InboxService(tmp_path / "inbox.sqlite3", transport, node_id=NODE)
    try:
        inbox.receive(_message("m-lost", "ghost-sender"))

        assert transport.notices == []
        notices = inbox.system_notices("ghost-sender")
        assert len(notices) == 1
        assert json.loads(notices[0].payload)["reason"] == "SENDER_NOT_CANONICAL"
    finally:
        inbox.close()


def test_ingress_admits_canonical_and_bridge_senders(tmp_path: Path) -> None:
    """Admitted really is admitted: canonical agents AND channel: reply
    bridges (the Lark inbound forward shape) both land."""

    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        canonical = inbox.receive(_message("m-ok", "agent:o:m:actor"))
        bridge = inbox.receive(_message("m-bridge", "channel:lark:main"))

        assert canonical.accepted is True
        assert bridge.accepted is True
        assert {m.message_id for m in inbox.pending_messages(URI)} == {
            "m-ok",
            "m-bridge",
        }
    finally:
        inbox.close()


# -- B: the transport-layer origin stamp --------------------------------------


def test_committed_rows_carry_the_origin_stamp(tmp_path: Path) -> None:
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        inbox.receive(
            _message("m-origin", "agent:o:m:actor", origin_node="origin-node")
        )

        (stored,) = inbox.pending_messages(URI)
        assert stored.origin_node == "origin-node"
    finally:
        inbox.close()


def test_pre_stamp_rows_read_back_with_no_origin(tmp_path: Path) -> None:
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        inbox.receive(_message("m-plain", "agent:o:m:actor"))

        (stored,) = inbox.pending_messages(URI)
        assert stored.origin_node is None
    finally:
        inbox.close()


def test_delivery_frame_round_trips_the_origin_and_decodes_pre_stamp_frames() -> (
    None
):
    stamped = _message("m-1", "agent:o:m:actor", origin_node="origin-node")
    assert decode_delivery_frame(encode_delivery_frame(stamped)).origin_node == (
        "origin-node"
    )

    plain = _message("m-2", "agent:o:m:actor")
    frame = json.loads(encode_delivery_frame(plain))
    assert "origin_node" not in frame  # additive key: absent, not null
    assert decode_delivery_frame(encode_delivery_frame(plain)).origin_node is None


class _FakeSession:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, key: str, payload: bytes) -> None:
        self.puts.append((key, payload))

    def get(self, key: str, timeout: float) -> tuple[()]:
        del key, timeout
        return ()


def test_zenoh_transport_stamps_origin_at_the_wire_boundary() -> None:
    session = _FakeSession()
    transport = ZenohDeliveryTransport(
        session,  # type: ignore[arg-type]
        _StubPresence(),
        receipt_timeout=0.01,
        origin_node="this-node",
    )

    transport.deliver(_message("m-stamp", "agent:o:m:actor"))
    # An author-set origin wins over the transport's own node (relay honesty).
    transport.deliver(
        _message("m-relay", "agent:o:m:actor", origin_node="author-node")
    )

    first = decode_delivery_frame(session.puts[0][1])
    second = decode_delivery_frame(session.puts[1][1])
    assert first.origin_node == "this-node"
    assert second.origin_node == "author-node"


def test_zenoh_transport_without_origin_config_stamps_nothing() -> None:
    session = _FakeSession()
    transport = ZenohDeliveryTransport(
        session, _StubPresence(), receipt_timeout=0.01  # type: ignore[arg-type]
    )

    transport.deliver(_message("m-plain", "agent:o:m:actor"))

    assert decode_delivery_frame(session.puts[0][1]).origin_node is None


# -- A: the gate and the boundary agree (no drift) ----------------------------


def test_every_send_the_boundary_admits_passes_ingress(tmp_path: Path) -> None:
    """A/B sequencing proof: after canonicalization, no admitted send
    produces a sender the ingress gate would reject."""

    transport = _RecordingTransport()
    transport.set_online(URI, True)
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})

    app.handle("agent.create", {"name": "ad-hoc-ops"})
    for source in ("foo", "ad-hoc-ops", "channel:lark:main", URI):
        result = _send(app, source, URI)
        assert result["ok"] is True

    assert len(transport.frames) == 4
    for frame in transport.frames:
        assert ":" in frame.sender  # never bare on the wire


# -- #96: 拒绝后果陈述(A)+ 回复路径不可用警告(B) ------------------------------


def test_sender_unresolved_error_states_the_reply_consequence(tmp_path: Path) -> None:
    """A: the rejection tells the fact about replies, not just the miss."""

    app = DaemonApplication(
        **daemon_dependencies(),
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    )
    try:
        app._resolve_send_sender("nobody-here")  # type: ignore[attr-defined]
        raise AssertionError("expected SENDER_UNRESOLVED")
    except DaemonRequestError as failure:
        assert failure.code == "SENDER_UNRESOLVED"
        assert "cannot receive replies" in str(failure)


def test_send_flags_a_user_sender_without_a_squire_profile(tmp_path: Path) -> None:
    """B: user:<owner> without a complete profile provably cannot be replied to."""

    transport = _RecordingTransport()
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})

    result = _send(app, "user:nobody", URI)

    assert result["ok"] is True  # the send itself is legal
    assert result["replyPathUnavailable"] is True


def test_send_does_not_flag_a_user_sender_with_a_complete_profile(
    tmp_path: Path,
) -> None:
    transport = _RecordingTransport()
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})
    app.user_profiles.ensure(
        owner="allen",
        owner_key="owner-allen",
        login_name="allen",
        machine=NODE,
        machine_key="machine-key",
    )
    adapter_uri = f"channel:{OWNER}:{NODE}:squire-app"
    app.user_profiles.set_squire_channel("owner-allen", adapter_uri)
    app.user_profiles.bind_owner_open_id(
        "owner-allen", channel=adapter_uri, open_id="ou_allen"
    )

    result = _send(app, "user:allen", URI)

    assert "replyPathUnavailable" not in result


def test_send_flags_a_channel_sender_whose_adapter_is_missing(
    tmp_path: Path,
) -> None:
    transport = _RecordingTransport()
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})

    class _Adapters:
        gateway_names = ("lark-main",)

        def stop(self) -> None:
            pass

    app._adapters = _Adapters()  # type: ignore[assignment]

    flagged = _send(app, "channel:lark:not-configured", URI)
    assert flagged["replyPathUnavailable"] is True

    fine = _send(app, "channel:lark:lark-main", URI)
    assert "replyPathUnavailable" not in fine


def test_send_never_flags_a_registered_agent_sender(tmp_path: Path) -> None:
    """宁可漏报:the registered path is never warned."""

    transport = _RecordingTransport()
    app = _application(tmp_path, transport)
    app.handle("agent.create", {"name": "foo"})

    result = _send(app, "foo", URI)

    assert "replyPathUnavailable" not in result


def test_send_cli_warns_on_stderr_and_marks_json(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_a, **_k: {
            "ok": True,
            "deliveries": [{"accepted": True}],
            "replyPathUnavailable": True,
        },
    )
    runner = CliRunner()

    plain = runner.invoke(
        _cli_root_app,
        ["message", "send", "--from", "user:nobody", "--to", "agent:o:m:a", "--summary", "hi", "hi"],
    )
    assert plain.exit_code == 0
    assert "no reply path" in (plain.stderr or plain.stdout)

    as_json = runner.invoke(
        _cli_root_app,
        [
            "message",
            "send",
            "--from",
            "user:nobody",
            "--to",
            "agent:o:m:a",
            "--summary",
            "hi",
            "hi",
            "--json",
        ],
    )
    assert as_json.exit_code == 0
    assert json.loads(as_json.stdout)["replyPathUnavailable"] is True
