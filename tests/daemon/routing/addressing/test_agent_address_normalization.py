"""Regression tests for the agent: display-prefix delivery failure.

`hyprial targets` used to advertise node targets as `agent:<node>` while
liveliness and the persisted outbox used the bare node name; sending to the
advertised URI queued forever with `ok: true`.  Normalization keeps the two
spellings as one address, and the send-time admission gate now rejects that
address outright (Allen: a node is announced on the network but is not a
delivery target; node visibility lives in `hyprial host list`).
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication,DaemonRequestError
from hyprial.daemon.impl.configuration.identity import normalize_agent_recipient
from hyprial.daemon.impl.inbox import InboxMessage, InboxService, MemoryDeliveryTransport
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate


class _AutoConfirmTransport(MemoryDeliveryTransport):
    """Simulates a reachable peer: every delivery is receipted at once."""

    def deliver(self, message: InboxMessage) -> bool:
        self.confirm_direct(message.message_id)
        return super().deliver(message)


class _Presence:
    def __init__(self, actors: tuple[str, ...]) -> None:
        self._actors = actors

    def online_actors(self) -> tuple[str, ...]:
        return self._actors

    def raw_online_actors(self) -> tuple[str, ...]:
        # 桩无 interactive 层:raw 集合与过滤后集合同义。
        return self._actors

    def liveness_keeps(self, actor: str) -> bool:
        # 桩无 interactive 层:复核一律保留(与 raw 同义,不改变测试语义)。
        return True

    def actor_online(self, actor: str) -> bool:
        return actor in self._actors

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def application(
    tmp_path: Path, transport: MemoryDeliveryTransport, actors: tuple[str, ...]
) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="node-1",
    **_deep_module_daemon_dependencies())
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", transport)
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _Presence(actors)
    # Explicit premise (wp2.md §4.9 option a): this test is about address
    # normalization, not links, so a permissive gate keeps A→B delivering.
    app.link_admission.bind(PermissiveLinkGate())
    return app


def _registered(app: DaemonApplication, name: str) -> str:
    """Lazily register a CLI sender persona (resolve-or-reject, Allen b)."""

    if app.agents.get(name) is None:
        app.handle("agent.create", {"name": name})
    return name


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # The advertised display form resolves to the bare node name.
        ("agent:member-laptop.example-tailnet.ts.net",
         "member-laptop.example-tailnet.ts.net"),
        ("agent:peer", "peer"),
        # Bare names already are canonical.
        ("peer", "peer"),
        # Canonical interactive actor URIs are identities, not decoration.
        ("agent:owner:host:hq-adjutant", "agent:owner:host:hq-adjutant"),
        # Other URI schemes are untouched.
        ("user:owner:host", "user:owner:host"),
        ("channel:#ops", "channel:#ops"),
        # A bare prefix with no name is not a valid node; leave it alone.
        ("agent:", "agent:"),
    ],
)
def test_normalize_agent_recipient_shapes(given: str, expected: str) -> None:
    assert normalize_agent_recipient(given) == expected


def test_a_node_is_not_a_target_and_sending_to_one_is_rejected(
    tmp_path: Path,
) -> None:
    """Allen's rule: a node is announced on the network but is not a target.

    targets lists delivery promises only, so the bare node_id presence token
    no longer appears there at all (node visibility lives in `hyprial host list`),
    and a send to either node spelling fails loudly with facts instead of
    landing receipted-but-never-consumed in the node inbox.
    """

    transport = _AutoConfirmTransport()
    transport.set_online("peer-node", True)
    app = application(tmp_path, transport, ("peer-node",))

    assert app.handle("targets", {})["targets"] == []
    host = app.handle("hosts", {})["hosts"][0]
    assert {"nodeId": host["nodeId"], "status": host["status"]} == {
        "nodeId": "peer-node",
        # set_online above is mesh presence, which is what status reports.
        "status": "online",
    }
    assert host["observedAtMs"] is None
    assert host["ageMs"] is None
    assert host["stale"] is True
    assert host["lastError"] is None

    for spelling in ("peer-node", "agent:peer-node"):
        with pytest.raises(DaemonRequestError) as failure:
            app.handle(
                "message.send", {"from": _registered(app, "driver"), "to": [spelling], "summary": "hi", "message": "hi"}
            )
        assert failure.value.code == "TARGET_IS_NODE"

    assert app._inbox.outbox_count() == 0
    assert transport.delivered == []


def test_prefixed_and_bare_node_forms_reject_identically(tmp_path: Path) -> None:
    """Normalization still maps both spellings to one address — the gate
    then rejects that one address, so the two forms can never diverge."""

    transport = _AutoConfirmTransport()
    transport.set_online("peer-node", True)
    app = application(tmp_path, transport, ("peer-node",))

    for spelling in ("agent:peer-node", "peer-node"):
        with pytest.raises(DaemonRequestError) as failure:
            app.handle(
                "message.send", {"from": _registered(app, "driver"), "to": [spelling], "summary": "a", "message": "a"}
            )
        assert failure.value.code == "TARGET_IS_NODE"
        assert failure.value.data == {"target": "peer-node"}


def test_canonical_interactive_actor_uri_passes_through_untouched(
    tmp_path: Path,
) -> None:
    transport = _AutoConfirmTransport()
    transport.set_online("agent:owner:host:hq-adjutant", True)
    app = application(tmp_path, transport, ("agent:owner:host:hq-adjutant",))

    result = app.handle(
        "message.send",
        {
            "from": _registered(app, "driver"),
            "to": ["agent:owner:host:hq-adjutant"],
            "summary": "hi",
            "message": "hi",
        },
    )

    (delivery,) = result["deliveries"]
    assert delivery["target"] == "agent:owner:host:hq-adjutant"
    assert delivery["queued"] is False


def test_queued_delivery_states_why(tmp_path: Path) -> None:
    """A deferred send must not hide behind ok:true without an explanation."""

    transport = MemoryDeliveryTransport()  # nothing online, no mailboxes
    app = application(tmp_path, transport, ())

    result = app.handle(
        "message.send",
        {"from": _registered(app, "driver"), "to": ["agent:op:ghost-node:ghost"], "summary": "hi", "message": "hi"},
    )

    (delivery,) = result["deliveries"]
    assert result["ok"] is True
    assert delivery["accepted"] is True
    assert delivery["queued"] is True
    assert delivery["reason"] == "target-not-visible"
    assert delivery["target"] == "agent:op:ghost-node:ghost"
    assert app._inbox.outbox_count() == 1


@pytest.mark.parametrize(
    ("presence", "expected"),
    [
        ((), "node-offline"),
        (("ghost-node",), "actor-unknown"),
        (
            ("ghost-node", "agent:op:ghost-node:ghost"),
            "no-route",
        ),
    ],
)
def test_queued_delivery_has_typed_resolution_reason(
    tmp_path: Path,
    presence: tuple[str, ...],
    expected: str,
) -> None:
    transport = MemoryDeliveryTransport()
    app = application(tmp_path, transport, presence)

    result = app.handle(
        "message.send",
        {
            "from": _registered(app, "driver"),
            "to": ["agent:op:ghost-node:ghost"],
            "summary": "hi",
            "message": "hi",
        },
    )

    (delivery,) = result["deliveries"]
    assert delivery["reason"] == "target-not-visible"
    assert delivery["resolutionReason"] == expected


def test_send_still_rejects_non_string_targets(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    app = application(tmp_path, transport, ())

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("message.send", {"from": _registered(app, "driver"), "to": [42], "summary": "hi", "message": "hi"})

    assert failure.value.code == "INVALID_ARGUMENT"
