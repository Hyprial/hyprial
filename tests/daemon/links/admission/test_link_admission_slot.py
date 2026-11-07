"""WP4 §2.1: the admission slot, the governance predicate and its defaults."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.dispatch.admission import (
    LINK_GATE_ERROR_EVENT,
    LINK_GATE_UNBOUND_EVENT,
    LinkAdmission,
    link_governed,
)
from hyprial.daemon.impl.dispatch.identity import (
    DISPATCH_SERVICE_ACTOR_NAME,
    dispatch_service_actor_uri,
)
from hyprial.daemon.impl.links.ports import LinkGate
from hyprial.kernel import canonical_agent_uri
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate, ScriptedLinkGate

NODE = "node-1"
OWNER = "op"
# README rulings 45/51: the mechanism exemption is keyed on the reserved ACTOR
# NAME, never on the owner or the machine segment.
TURN_HOOKS_ACTOR_NAME = "_turn-hooks"
RESERVED_ACTORS = frozenset({DISPATCH_SERVICE_ACTOR_NAME, TURN_HOOKS_ACTOR_NAME})
# One canonical URI per reserved name, on this node AND on a remote node.
INTERNAL_URIS = tuple(
    canonical_agent_uri(owner, machine, name)
    for owner, machine in ((OWNER, NODE), ("other", "elsewhere"))
    for name in sorted(RESERVED_ACTORS)
)
A = canonical_agent_uri(OWNER, NODE, "alice")
B = canonical_agent_uri(OWNER, NODE, "bob")


class _Logs:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def __call__(self, level: str, component: str, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def named(self, event: str) -> list[dict[str, object]]:
        return [fields for name, fields in self.events if name == event]


class _OnlySenderKnown:
    """A partial double: enough for isinstance-of-method checks to fail."""

    def sender_known(self, *, recipient: str, sender: str) -> bool:  # pragma: no cover
        return True


def test_fakes_satisfy_the_runtime_checkable_protocol() -> None:
    assert isinstance(ScriptedLinkGate(), LinkGate)
    assert isinstance(PermissiveLinkGate(), LinkGate)
    assert not isinstance(_OnlySenderKnown(), LinkGate)
    with pytest.raises(TypeError):
        LinkAdmission().bind(_OnlySenderKnown())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("sender", "recipient", "governed"),
    [
        (A, B, True),
        (f"user:{OWNER}", B, False),
        ("adapter:lark:main", B, False),
        ("channel:lark:main", B, False),
        (f"system:hyprial:{NODE}", B, False),
        (dispatch_service_actor_uri(OWNER, NODE), B, False),
        (canonical_agent_uri(OWNER, NODE, "_turn-hooks"), B, False),
        # README ruling 51: an internal actor is exempt in BOTH directions.
        (A, dispatch_service_actor_uri(OWNER, NODE), False),
        (A, canonical_agent_uri(OWNER, NODE, "_turn-hooks"), False),
        # README rulings 45/51: the reserved name exempts on ANY node/owner,
        # as sender and as recipient (a remote node has no link file either).
        (canonical_agent_uri("other", "elsewhere", "dispatch-coordinator"), B, False),
        (canonical_agent_uri("other", "elsewhere", "_turn-hooks"), B, False),
        (A, canonical_agent_uri("other", "elsewhere", "dispatch-coordinator"), False),
        (A, canonical_agent_uri("other", "elsewhere", "_turn-hooks"), False),
        (A, A, False),
        (A, f"user:{OWNER}", False),
        # A name that merely starts with "_" is an ordinary agent (review B2).
        (canonical_agent_uri(OWNER, NODE, "_named-worker"), B, True),
    ],
)
def test_link_governed_matrix(sender: str, recipient: str, governed: bool) -> None:
    assert link_governed(sender, recipient, internal_actors=RESERVED_ACTORS) is governed


def test_unbound_slot_defaults() -> None:
    logs = _Logs()
    slot = LinkAdmission(internal_actors=RESERVED_ACTORS, logger=logs)

    assert slot.ingress_refusal(sender=A, recipient=B, intent="request") is None
    assert slot.dispatch_admitted(
        sender=A,
        recipient=B,
        intent="request",
        message_id="m1",
        now_ms=1,
        reply_to=None,
    ) is False
    # Not governed at all: admitted without ever touching the gate.
    assert slot.dispatch_admitted(
        sender=f"user:{OWNER}",
        recipient=B,
        intent="request",
        message_id="m2",
        now_ms=1,
        reply_to=None,
    ) is True

    for _ in range(10):
        slot.dispatch_admitted(
            sender=A,
            recipient=B,
            intent="request",
            message_id="m3",
            now_ms=1,
            reply_to=None,
        )
    assert len(logs.named(LINK_GATE_UNBOUND_EVENT)) == 1


def test_gate_errors_hold_but_never_refuse() -> None:
    logs = _Logs()
    slot = LinkAdmission(internal_actors=RESERVED_ACTORS, logger=logs)
    slot.bind(ScriptedLinkGate(raise_on={"sender_known", "admit_dispatch"}))

    assert slot.ingress_refusal(sender=A, recipient=B, intent="request") is None
    assert slot.dispatch_admitted(
        sender=A,
        recipient=B,
        intent="request",
        message_id="m1",
        now_ms=5,
        reply_to=None,
    ) is False
    errors = logs.named(LINK_GATE_ERROR_EVENT)
    assert {item["stage"] for item in errors} == {"ingress", "dispatch"}


def test_bind_once_and_call_shape() -> None:
    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    gate = ScriptedLinkGate(default_admit=True)
    slot.bind(gate)
    with pytest.raises(RuntimeError):
        slot.bind(PermissiveLinkGate())

    assert slot.dispatch_admitted(
        sender=A,
        recipient=B,
        intent="reply",
        message_id="m9",
        now_ms=42,
        reply_to="m0",
    ) is True
    assert gate.admit_calls() == [
        {
            "recipient": B,
            "sender": A,
            "message_id": "m9",
            "now_ms": 42,
            "intent": "reply",
            "reply_to": "m0",
        }
    ]


def test_internal_recipient_never_reaches_the_gate() -> None:
    """README ruling 51: replies addressed to a mechanism actor are exempt."""

    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    gate = ScriptedLinkGate(default_admit=False)
    slot.bind(gate)

    for internal in INTERNAL_URIS:
        assert slot.ingress_refusal(sender=A, recipient=internal, intent="reply") is None
        assert slot.dispatch_admitted(
            sender=A,
            recipient=internal,
            intent="reply",
            message_id=f"m-{internal}",
            now_ms=1,
            reply_to="m-open",
        ) is True
    assert gate.calls == []


def test_raw_service_default_is_fail_closed_not_absent() -> None:
    """review item 3: one default, and it is an unbound (holding) slot."""

    import tempfile
    from pathlib import Path

    from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport

    with tempfile.TemporaryDirectory() as directory:
        service = InboxService(
            Path(directory) / "inbox.sqlite3", MemoryDeliveryTransport()
        )
        try:
            assert isinstance(service._link_admission, LinkAdmission)
            assert service._link_admission.bound is False
            # Ingress stays open (the wiring may self-heal) ...
            assert service._link_admission.ingress_refusal(
                sender=A, recipient=B, intent="request"
            ) is None
            # ... but dispatch holds, so there is no permissive second default.
            assert service._link_admission.dispatch_admitted(
                sender=A,
                recipient=B,
                intent="request",
                message_id="m-unbound",
                now_ms=1,
                reply_to=None,
            ) is False
        finally:
            service.close()


def test_real_composition_passes_one_link_admission_to_both_owners(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """review item 9 / T17: the real startup wiring uses one shared slot.

    This runs ``DaemonApplication._start_runtime`` with the composition's
    collaborators replaced by recording doubles (the probe pattern the WP4
    acceptance suggests), then asserts the custody coordinator's
    ``service_options['link_admission']`` and the runtime bridge's
    ``link_admission`` are the SAME object and the app's one slot.
    """

    from pathlib import Path

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.application.netendpoints import endpoints as transport_owner
    from hyprial.daemon.impl.application.wiring import transport as wiring
    from tests.daemon.progress import test_progress_end_to_end as probe

    captured: dict[str, object] = {}

    class RecordingCoordinator(probe._CompositionInboxCoordinator):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["service_options"] = kwargs.get("service_options")
            super().__init__(*args, **kwargs)

    class RecordingRuntime(probe._CompositionRuntime):
        def __init__(self, **kwargs: object) -> None:
            captured["bridge_link_admission"] = kwargs.get("link_admission")
            super().__init__(**kwargs)

    monkeypatch.setattr(transport_owner, "ZenohTransport", probe._CompositionTransport)
    monkeypatch.setattr(
        transport_owner, "TransportSessionAuthority", lambda session: session
    )
    monkeypatch.setattr(wiring, "LivelinessDirectory", probe._CompositionDirectory)
    monkeypatch.setattr(wiring, "ZenohDeliveryTransport", probe._CompositionDelivery)
    monkeypatch.setattr(
        wiring, "LocalFirstDeliveryTransport", probe._CompositionDelivery
    )
    monkeypatch.setattr(wiring, "LarkReplyBridgeTransport", probe._CompositionBridge)
    monkeypatch.setattr(wiring, "DeliveryCustodyCoordinator", RecordingCoordinator)
    monkeypatch.setattr(
        wiring, "DeliveryCustodyFacade", lambda coordinator, _database: coordinator.inbox
    )
    monkeypatch.setattr(wiring, "AdapterRuntime", probe._CompositionAdapters)
    monkeypatch.setattr(wiring, "HarnessRuntimeActor", probe._CompositionHarnesses)
    monkeypatch.setattr(wiring, "HarnessPortClient", lambda actor, _events: actor)
    monkeypatch.setattr(wiring, "ZenohInboxEndpoint", probe._CompositionEndpoint)
    monkeypatch.setattr(wiring, "DeliveryStatusEndpoint", probe._CloseOnly)
    monkeypatch.setattr(wiring, "ReceiverUserDelivery", probe._CloseOnly)
    monkeypatch.setattr(wiring, "ZenohUserDeliveryEndpoint", probe._CloseOnly)
    monkeypatch.setattr(wiring, "ZenohUserDeliveryTransport", probe._CloseOnly)
    monkeypatch.setattr(wiring, "OrgContextMesh", probe._OrgEndpoint)
    monkeypatch.setattr(wiring, "DaemonEventBridge", RecordingRuntime)
    monkeypatch.setattr(
        DaemonApplication, "_start_lifecycle_manager", lambda self, **_kwargs: None
    )

    base = Path(str(tmp_path))
    app = DaemonApplication(
        state_dir=base / "state",
        socket_path=base / "state" / "daemon.sock",
        node_id=NODE,
        hyprial_home=base / "home",
        owner=OWNER,
        **_deep_module_daemon_dependencies(),
    )
    try:
        app._start_runtime()
        slot = app.link_admission
        assert isinstance(slot, LinkAdmission)
        service_options = captured["service_options"]
        assert isinstance(service_options, dict)
        assert service_options["link_admission"] is slot
        assert captured["bridge_link_admission"] is slot
    finally:
        app._close()
        app.agents.close()


def test_ordinary_agent_markers_never_widen_the_exemption() -> None:
    """review B2: intent=system, turnHookRequest and ``_`` names still gate."""

    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    gate = ScriptedLinkGate(default_admit=False)
    slot.bind(gate)

    for index, sender in enumerate(
        (
            A,
            canonical_agent_uri(OWNER, NODE, "_named-worker"),
        )
    ):
        assert slot.dispatch_admitted(
            sender=sender,
            recipient=B,
            intent="system",
            message_id=f"m{index}",
            now_ms=1,
            reply_to=None,
        ) is False
    assert len(gate.admit_calls()) == 2
    # The reserved mechanism names are the only bypass, on ANY node.
    for index, internal in enumerate(INTERNAL_URIS):
        assert slot.dispatch_admitted(
            sender=internal,
            recipient=B,
            intent="request",
            message_id=f"hook-{index}",
            now_ms=1,
            reply_to=None,
        ) is True
    assert len(gate.admit_calls()) == 2


def test_internal_actors_property_exposes_the_reserved_names() -> None:
    """Rework 2: WP2 reuses the reserved-name set through a read-only property."""

    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    assert slot.internal_actors == frozenset(
        {DISPATCH_SERVICE_ACTOR_NAME, "_turn-hooks"}
    )


def test_gate_property_exposes_the_bound_gate() -> None:
    """Rework 2: WP2 reads the bound gate without touching private state."""

    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    assert slot.gate is None
    gate = ScriptedLinkGate()
    slot.bind(gate)
    assert slot.gate is gate
