"""WP0: the send-link vocabulary every later package codes against.

Pins the names, not behaviour: codes exist and are distinct, hold reasons
are not transient errors, the frame carries ``summary`` additively, the
hook registry accepts the link events, and the gate protocols are
runtime-checkable so composition can assert a real implementation.
"""

from __future__ import annotations

import json

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame,
    encode_delivery_frame,
)
from hyprial.daemon.impl.links import (
    EVENT_LINK_MINTED,
    EVENT_LINK_RECLAIMED,
    HOLD_REASONS,
    SUMMARY_MAX_BYTES,
    Allow,
    Hold,
    LinkGate,
    Mint,
)
from hyprial.kernel import HookEvent, ipc_errors
from hyprial.kernel.impl.hooks.events import EVENT_NAMES

import pytest


def test_link_codes_exist_and_are_distinct() -> None:
    codes = [
        ipc_errors.SUMMARY_REQUIRED,
        ipc_errors.LINK_PEER_FULL,
        ipc_errors.LINK_SELF_FULL,
        ipc_errors.LINK_WINDOW_FULL,
        ipc_errors.LINK_UNKNOWN,
        ipc_errors.LINK_RECLAIMED,
        ipc_errors.LINK_MINTING,
    ]
    assert len(set(codes)) == len(codes)
    assert all(code == code.upper() for code in codes)


def test_hold_reasons_are_holds_not_transient_errors() -> None:
    assert HOLD_REASONS == {
        ipc_errors.LINK_MINTING,
        ipc_errors.LINK_PEER_FULL,
        ipc_errors.LINK_SELF_FULL,
        ipc_errors.LINK_WINDOW_FULL,
    }
    for code in HOLD_REASONS:
        assert code not in ipc_errors.TRANSIENT_CODE_CLASSES
    with pytest.raises(ValueError):
        Hold(reason=ipc_errors.LINK_UNKNOWN, peer="agent:a:m:b")
    assert isinstance(Allow(), Allow)
    assert Mint(peer="agent:a:m:b").peer == "agent:a:m:b"


def _message(**overrides: object) -> InboxMessage:
    base = dict(
        message_id="m1",
        conversation_id="c1",
        sender="agent:o:m:a",
        recipient="agent:o:m:b",
        payload=b"body",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    base.update(overrides)
    return InboxMessage(**base)  # type: ignore[arg-type]


def test_frame_carries_summary_additively() -> None:
    plain = _message()
    assert plain.summary is None
    assert "summary" not in json.loads(encode_delivery_frame(plain))
    assert decode_delivery_frame(encode_delivery_frame(plain)).summary is None

    stamped = _message(summary="please review the WP0 seams")
    frame = json.loads(encode_delivery_frame(stamped))
    assert frame["summary"] == "please review the WP0 seams"
    decoded = decode_delivery_frame(encode_delivery_frame(stamped))
    assert decoded.summary == stamped.summary
    assert decoded.payload == b"body"


def test_summary_budget_is_bytes_not_characters() -> None:
    assert SUMMARY_MAX_BYTES == 512
    # 171 CJK characters are 513 UTF-8 bytes: the budget must be measured
    # in bytes, so this string is over it although it is short in characters.
    assert len(("摘" * 171).encode("utf-8")) > SUMMARY_MAX_BYTES


def test_link_events_are_registered_hook_names() -> None:
    assert {EVENT_LINK_MINTED, EVENT_LINK_RECLAIMED} <= EVENT_NAMES
    event = HookEvent(
        event=EVENT_LINK_MINTED,
        kind="observe",
        emitted_at_ms=1,
        seq=1,
        source="links",
        actor="agent:o:m:a",
        payload={"peer": "agent:o:m:b"},
    )
    assert event.event == "link.minted"


def test_links_package_init_imports_only_ports() -> None:
    """inbox imports ``links.ports`` through the package; if ``__init__``
    ever imports service/store (WP2), inbox <-> links becomes a cycle."""
    import ast
    from pathlib import Path

    import hyprial.daemon.impl.links as links

    tree = ast.parse(Path(links.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported == {"hyprial.daemon.impl.links.ports"}, imported


def test_link_gate_is_runtime_checkable() -> None:
    class Gate:
        def admit_send(self, *, sender, target, intent, reply_to, now_ms):
            return Allow()

        def sender_known(self, *, recipient, sender):
            return True

        def admit_dispatch(
            self, *, recipient, sender, message_id, now_ms, intent="request", reply_to=None
        ):
            return True

        def settle_hint(self, *, actor, message_id):
            return "keep"

    assert isinstance(Gate(), LinkGate)
    assert not isinstance(object(), LinkGate)
