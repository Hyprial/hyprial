"""K1-K9: the harness-neutral carrier contract (wp5.md §2).

The carrier package owns settlement and turn reporting only.  Model-facing
text is the shared ``inbox-batch-v1`` renderer (``daemon.impl.inbox.batch``),
so this module pins where that authority lives and the two turn IPC methods.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.harnesses.carrier.contract import (
    IPC_SESSION_TURN_ENDED,
    IPC_SESSION_TURN_STARTED,
    CarrierPort,
    IpcCarrierPort,
    SettleOutcome,
    settle_injected,
)
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.batch import (
    BatchRow,
    BatchSelection,
    BatchBounds,
    build_snapshot,
    render_full,
    render_scalar_summary,
)
from hyprial.daemon.impl.inbox.contracts.settlement import settle_hint_for
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from tests.daemon.application.transport.channel.support import (
    _close_application_domains as _close_application_domains,
)

READER = "agent:op:node-1:reader"
DRIVER = "agent:op:node-1:driver"


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "messageId": "m1",
        "from": DRIVER,
        "to": READER,
        "intent": "request",
        "kind": "message",
        "summary": "S",
        "bodyBytes": 15,
        "message": "BODY-SENTINEL-1",
    }
    row.update(overrides)
    return row


def test_the_scalar_summary_path_never_inlines_a_body() -> None:
    text = render_scalar_summary(
        message_id="m1",
        sender=DRIVER,
        recipient=READER,
        intent="request",
        summary="S",
        body_bytes=15,
    )
    assert "summary: S" in text
    assert "bodyBytes: 15" in text
    assert 'harness_read_body("m1")' in text
    # The mechanism/hook scalar path names the body, it never carries it.
    assert "BODY-SENTINEL" not in text


def test_the_batch_renderer_is_the_only_model_text_authority() -> None:
    message = InboxMessage(
        message_id="m1",
        conversation_id="c1",
        sender=DRIVER,
        recipient=READER,
        payload=json.dumps({"message": "full body"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        summary="S",
    )
    selection = BatchSelection(
        actor=READER,
        bounds=BatchBounds(1, 0, 1),
        rows=(
            BatchRow(
                "inbox",
                message,
                1,
                "full body",
                len("full body".encode()),
            ),
        ),
    )
    snapshot = build_snapshot(selection, key=b"k")
    text = render_full(snapshot)
    assert text.startswith("[Harness Network inbox batch")
    assert "--- BEGIN UNTRUSTED MESSAGE DATA ---" in text
    assert "full body" in text
    assert snapshot.items[0].cls == "request"
    assert snapshot.items[0].expected_action == "reply"


class _Port:
    def __init__(self, hints: dict[str, str]):
        self.hints = hints
        self.calls: list[tuple[str, str]] = []
        self.acked: list[str] = []

    def settle_hint(self, message_id: str) -> str:
        self.calls.append(("settle_hint", message_id))
        answer = self.hints.get(message_id, "keep")
        if answer == "raise":
            raise TimeoutError("transport")
        return answer

    def ack(self, message_id: str) -> bool:
        self.calls.append(("ack", message_id))
        self.acked.append(message_id)
        return True

    def turn_started(self, report_id: str, *, delivery_id: str = "") -> None:
        return None

    def turn_ended(self, report_id: str, *, status: str, delivery_id: str = "") -> None:
        return None


def test_settle_injected_obeys_hint_and_is_idempotent() -> None:
    port = _Port({"a": "ack", "b": "keep", "c": "raise"})
    outcome = settle_injected(port, ["a", "b", "c"])
    assert outcome == SettleOutcome(acked=("a",), kept=("b",), deferred=("c",))
    assert port.acked == ["a"]
    assert {method for method, _ in port.calls} <= {"settle_hint", "ack"}

    port.calls.clear()
    again = settle_injected(port, ["a", "b", "c"], already={"a"})
    assert again == SettleOutcome(acked=(), kept=("b",), deferred=("c",))
    assert all(method != "ack" or mid != "a" for method, mid in port.calls)


def test_settle_hint_for_matches_ipc_handler(tmp_path: Path) -> None:
    from tests.daemon.application.transport.channel.support import (
        durable_application,
        official_address,
        register_actor,
    )

    def run(root: Path) -> None:
        app = durable_application(root)
        register_actor(app, "reader")
        reader = official_address(app, "reader")

        def seed(message_id: str, intent: str, *, recipient: str = reader) -> None:
            message = InboxMessage(
                message_id=message_id,
                conversation_id=f"c-{message_id}",
                sender=DRIVER,
                recipient=recipient,
                payload=json.dumps({"message": "B"}).encode(),
                intent=intent,
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=1,
                summary="S",
            )
            assert app._inbox.receive(message).accepted

        def compare(message_id: str) -> None:
            row = next(
                (
                    item
                    for item in app._inbox.pending_messages(reader)
                    if item.message_id == message_id
                ),
                None,
            )
            expected = app.handle(
                "message.settle_hint",
                {
                    "actor": "reader",
                    "sessionRef": "session-1",
                    "messageId": message_id,
                },
            )["hint"]
            actual = settle_hint_for(
                pending=row,
                actor=reader,
                message_id=message_id,
            )
            assert actual == expected, message_id

        seed("req", "request")
        seed("rep", "reply")
        seed("evt", "event")
        seed("acked", "request")
        seed("theirs", "request", recipient=DRIVER)
        app.handle(
            "message.ack",
            {"actor": "reader", "sessionRef": "session-1", "messageId": "acked"},
        )
        for message_id in ("req", "rep", "evt", "acked", "theirs", "unknown"):
            compare(message_id)

    run(tmp_path / "fixed-rule")


def test_settle_hint_for_reply_and_event_ack() -> None:
    for message_id, intent in (("rep", "reply"), ("evt", "event")):
        pending = InboxMessage(
            message_id=message_id,
            conversation_id=f"c-{message_id}",
            sender=DRIVER,
            recipient=READER,
            payload=b"{}",
            intent=intent,
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
        assert (
            settle_hint_for(
                pending=pending, actor=READER, message_id=message_id
            )
            == "ack"
        )


def test_carrier_port_has_no_reply_member() -> None:
    members = {name for name in dir(CarrierPort) if not name.startswith("_")}
    assert members == {"settle_hint", "ack", "turn_started", "turn_ended"}
    assert [name for name in dir(IpcCarrierPort) if "reply" in name.lower()] == []


def test_turn_ipc_names_match_router() -> None:
    assert IPC_SESSION_TURN_STARTED in _IPC_STATS_METHODS
    assert IPC_SESSION_TURN_ENDED in _IPC_STATS_METHODS


def test_harness_result_defaults_to_done_for_streaming_and_turn_carriers() -> None:
    from dataclasses import fields
    import pytest

    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
    from hyprial.daemon.impl.harnesses.streaming.process import SequentialTurnProcess
    from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import (
        TurnDeliveryProjection,
        TurnResultProjection,
    )

    result = HarnessResult("m-1", READER, HarnessResultStatus.COMPLETED, output="ok")
    assert result.route_outcome == "done"
    assert result.route_label is None

    next_result = HarnessResult(
        "m-next",
        READER,
        HarnessResultStatus.COMPLETED,
        route_outcome="next",
        route_label="code",
    )
    assert next_result.route_outcome == "next"
    assert next_result.route_label == "code"

    with pytest.raises(ValueError, match="route_outcome"):
        HarnessResult(
            "m-1",
            READER,
            HarnessResultStatus.COMPLETED,
            route_outcome="invalid",
        )
    for bad_label in (None, "", "   "):
        with pytest.raises(ValueError, match="route_label"):
            HarnessResult(
                "m-1",
                READER,
                HarnessResultStatus.COMPLETED,
                route_outcome="next",
                route_label=bad_label,
            )
    for closed_outcome in ("done", "refused"):
        with pytest.raises(ValueError, match="route_label"):
            HarnessResult(
                "m-1",
                READER,
                HarnessResultStatus.COMPLETED,
                route_outcome=closed_outcome,
                route_label="code",
            )

    assert "route_label" not in SequentialTurnProcess.__dict__
    assert "route_outcome" not in SequentialTurnProcess.__dict__
    assert "route_label" not in {f.name for f in fields(TurnDeliveryProjection)}
    assert "route_outcome" not in {f.name for f in fields(TurnDeliveryProjection)}
    assert "route_label" not in {f.name for f in fields(TurnResultProjection)}
    assert "route_outcome" not in {f.name for f in fields(TurnResultProjection)}


def test_codex_carrier_store_hardens_parent_and_sqlite_file_via_manysys(
    monkeypatch, tmp_path: Path
) -> None:
    import hyprial.daemon.impl.harnesses.carrier.codex_carrier_store as store_module
    from hyprial.kernel import harden_private_path as real_harden

    hardened: list[tuple[Path, int]] = []

    def record_harden(path: Path, *, mode: int) -> None:
        hardened.append((Path(path), mode))
        real_harden(path, mode=mode)

    monkeypatch.setattr(store_module, "harden_private_path", record_harden, raising=False)
    db_path = tmp_path / "nested-carrier" / "carrier.sqlite3"
    store = store_module.CodexCarrierStore(db_path)
    try:
        assert hardened == [(db_path.parent, 0o700), (db_path, 0o600)]
    finally:
        store.close()
