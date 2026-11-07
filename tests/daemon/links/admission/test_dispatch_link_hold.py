"""WP4 §2.8 / review B1, B2, B5: headless dispatch under the link gate."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.kernel import HarnessLaunchSpec, canonical_agent_uri
from tests.support.links.gate import BudgetedLinkGate, ScriptedLinkGate
from tests.support.runtime._owned_applications import owned_applications as owned_applications

NODE = "node-dispatch"
OWNER = "op"
ME = canonical_agent_uri(OWNER, NODE, "bob")
PEER = canonical_agent_uri(OWNER, NODE, "alice")
CAROL = canonical_agent_uri(OWNER, NODE, "carol")
OWNER_USER = f"user:{OWNER}"


class _Transport:
    def close(self) -> None:
        return None


class _Harnesses:
    def __init__(self) -> None:
        self.dispatched: list[str] = []
        #: One entry per dispatch: the members that delivery claims (a batch
        #: coalesces several rows into one model turn).
        self.batches: list[tuple[str, ...]] = []
        self.results: list[object] = []

    def drain_results(self) -> tuple[object, ...]:
        drained, self.results = tuple(self.results), []
        return drained

    def streaming_actors(self) -> tuple[str, ...]:
        return ("bob",)

    def dispatch(self, actor: str, delivery: object) -> bool:  # type: ignore[no-untyped-def]
        self.dispatched.append(delivery.delivery_id)  # type: ignore[attr-defined]
        self.batches.append(tuple(getattr(delivery, "message_ids", ()) or ()))
        return True


class _Logs:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def __call__(self, level: str, component: str, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def log(self, level: str, event: str, **fields: object) -> None:
        self(level, "daemon", event, **fields)

    def named(self, event: str) -> list[dict[str, object]]:
        return [fields for name, fields in self.events if name == event]


class _Events:
    def publish(self, _event: object) -> None:
        pass


def _message(
    message_id: str,
    *,
    sender: str = PEER,
    recipient: str = ME,
    intent: str = "request",
    hook_request: bool = False,
    reply_to: str | None = None,
) -> InboxMessage:
    body: dict[str, object] = {"message": "q"}
    if hook_request:
        body["turnHookRequest"] = True
    if reply_to is not None:
        body["replyTo"] = reply_to
    return _body_message(
        message_id,
        body,
        sender=sender,
        recipient=recipient,
        intent=intent,
    )


def _message_with_expiry(message_id: str, expires_at_ms: int) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender=PEER,
        recipient=ME,
        payload=json.dumps({"message": "q"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        expires_at_ms=expires_at_ms,
    )


def _body_message(
    message_id: str,
    body: dict[str, object],
    *,
    sender: str = PEER,
    recipient: str = ME,
    intent: str = "request",
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender=sender,
        recipient=recipient,
        payload=json.dumps(body).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


class _Assembly:
    def __init__(
        self,
        base: Path,
        *,
        slot: LinkAdmission | None,
        logs: _Logs,
        clock_ms: object | None = None,
    ) -> None:
        self._base = base
        self._slot = slot
        self._logs = logs
        self._clock_ms = clock_ms
        self._open()

    def _open(self) -> None:
        base = self._base
        logs = self._logs
        transport = MemoryDeliveryTransport()
        options: dict[str, object] = {"logger": logs}
        if self._slot is not None:
            options["link_admission"] = self._slot
        database = base / "inbox.sqlite3"
        coordinator = DeliveryCustodyCoordinator(
            database,
            transport,
            _Events(),
            node_id=NODE,
            service_options=options,
        )
        self.facade = DeliveryCustodyFacade(coordinator, database)
        self.transport = transport
        harnesses = _Harnesses()
        self.harnesses = harnesses
        desired = DesiredStateStore(base / "desired-state.json")
        # Batch admission needs a declared model carrier. Unknown kinds now
        # deliberately receive scalar summaries, never full-body batches.
        desired.upsert_harness(HarnessLaunchSpec(harness="pi", name="bob", headless=True))
        self.bridge = DaemonEventBridge(
            state_dir=base,
            node_id=NODE,
            desired_state=desired,
            transport=_Transport(),  # type: ignore[arg-type]
            inbox=self.facade,  # type: ignore[arg-type]
            harnesses=harnesses,  # type: ignore[arg-type]
            harness_actor_uri=lambda name: ME,
            logger=logs,
            clock_ms=self._clock_ms,  # type: ignore[arg-type]
        )

    def restart(self) -> None:
        """Rebuild the daemon over the same database and the same slot."""

        self.facade.close()
        self._open()

    def close(self) -> None:
        self.facade.close()


class _LogRecord:
    """Adapter so the actor-injected slot can use the same recorder."""

    def __init__(self, logs: _Logs) -> None:
        self._logs = logs

    def log(self, level: str, event: str, **fields: object) -> None:
        self._logs(level, "daemon", event, **fields)


def _slot(logs: _Logs, gate: ScriptedLinkGate) -> LinkAdmission:
    slot = LinkAdmission(logger=logs)
    slot.bind(gate)
    return slot


def test_held_message_stays_then_releases(tmp_path: Path) -> None:
    logs = _Logs()
    gate = ScriptedLinkGate(
        known={PEER}, admit={"m-held": False}, default_admit=True
    )
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(_message("m-held")).accepted

        for _ in range(3):
            assembly.bridge._dispatch_harness_deliveries()

        assert assembly.harnesses.dispatched == []
        assert [
            item.message_id for item in assembly.facade.pending_messages(ME)
        ] == ["m-held"]
        assert "m-held" not in assembly.bridge._inflight
        assert "m-held" not in assembly.bridge._attempt_generation
        assert len(gate.admit_calls()) == 3
        # Held means neither refused at ingress nor fetched, and no hook ran.
        assert assembly.facade.has_received("m-held") is True
        assert assembly.facade.has_fetched("m-held") is False
        held_record = assembly.facade.delivery_status_for_recipient(ME, "m-held")
        assert held_record is None or held_record.state.value != "refused"

        gate.admit["m-held"] = True
        assembly.bridge._dispatch_harness_deliveries()
        assert assembly.harnesses.dispatched == ["m-held"]
        admitted = logs.named("dispatch.link_admitted")
        assert len(admitted) == 1 and "heldMs" in admitted[0]
        assert len(logs.named("dispatch.link_held")) == 1
    finally:
        assembly.close()


def test_headless_gate_calls_run_on_the_inbox_actor_thread(tmp_path: Path) -> None:
    """review B3.4: the headless path must not call the gate on the tick thread."""

    logs = _Logs()
    gate = ScriptedLinkGate(known={PEER}, default_admit=True)
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(_message("m-thread")).accepted
        assembly.bridge._dispatch_harness_deliveries()
        threads = set(gate.call_threads("sender_known")) | set(
            gate.call_threads("admit_dispatch")
        )
        assert threads, "the gate was never asked"
        assert threading.get_ident() not in threads
        assert len(threads) == 1, threads
    finally:
        assembly.close()


def test_concurrent_entry_points_share_one_admission_budget(tmp_path: Path) -> None:
    """review B3.5: real threads on list/wait/headless share one W budget.

    A single budget unit is offered to three concurrent actor commands; the
    same id never consumes it twice and two distinct ids never both take it.
    """

    logs = _Logs()
    gate = BudgetedLinkGate(1, known={PEER})
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(_message("m-c1")).accepted
        assert assembly.facade.receive(_message("m-c2")).accepted

        results: list[tuple[str, ...]] = []
        errors: list[BaseException] = []

        def list_entry() -> None:
            try:
                results.append(
                    tuple(
                        item.message_id
                        for item in assembly.facade.admitted_pending(ME, now_ms=1)
                    )
                )
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)

        def wait_entry() -> None:
            try:
                results.append(
                    tuple(
                        item.message_id
                        for item in assembly.facade.admitted_dispatchable(ME, now_ms=1)
                    )
                )
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        def headless_entry() -> None:
            try:
                assembly.bridge._dispatch_harness_deliveries()
                results.append(tuple(assembly.harnesses.dispatched))
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [
            threading.Thread(target=target)
            for target in (list_entry, wait_entry, headless_entry)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        admitted_ids = {
            message_id
            for batch in results
            for message_id in batch
        }
        assert admitted_ids == {"m-c1"} or admitted_ids == {"m-c2"}
        assert gate.remaining == 0
    finally:
        assembly.close()


class _Clock:
    def __init__(self, now_ms: int = 1) -> None:
        self.now_ms = now_ms

    def ms(self) -> int:
        return self.now_ms


def test_budget_is_shared_across_different_senders(tmp_path: Path) -> None:
    """review B5: W is a recipient budget, not a per-sender allowance."""

    logs = _Logs()
    gate = BudgetedLinkGate(1, known={PEER, CAROL})
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(_message("m-peer", sender=PEER)).accepted
        assert assembly.facade.receive(_message("m-carol", sender=CAROL)).accepted
        assert assembly.facade.admitted_pending(ME, now_ms=1)

        admitted = [
            item.message_id
            for item in assembly.facade.admitted_pending(ME, now_ms=2)
        ]
        assert len(admitted) == 1
        assert gate.remaining == 0
        # One sender's second row cannot take the unit the other already took.
        assert len(assembly.facade.admitted_pending(ME, now_ms=3)) == 1
    finally:
        assembly.close()


def test_window_shrink_and_release_re_evaluates(tmp_path: Path) -> None:
    """review B5: repeated evaluation is stable, a release admits the next."""

    logs = _Logs()
    gate = ScriptedLinkGate(
        known={PEER}, admit={"m1": True, "m2": False}, default_admit=False
    )
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(_message("m1")).accepted
        assert assembly.facade.receive(_message("m2")).accepted

        first = [
            item.message_id
            for item in assembly.facade.admitted_pending(ME, now_ms=1)
        ]
        assert first == ["m1"]
        for now in (2, 3):
            assert [
                item.message_id
                for item in assembly.facade.admitted_pending(ME, now_ms=now)
            ] == ["m1"]

        # W shrinks (m1 no longer admitted) and m2's reference became visible.
        gate.admit["m1"] = False
        gate.admit["m2"] = True
        assert [
            item.message_id
            for item in assembly.facade.admitted_pending(ME, now_ms=4)
        ] == ["m2"]
    finally:
        assembly.close()


def test_reclaimed_row_is_not_revived(tmp_path: Path) -> None:
    """review B5: a reclaimed peer's residual row never comes back."""

    logs = _Logs()
    gate = ScriptedLinkGate(known={PEER}, default_admit=True)
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(_message("m-drain")).accepted
        # WP2 drains the reclaimed link through the inbox authority face.
        assert assembly.facade.ack(ME, "m-drain").acknowledged is True

        assert assembly.facade.admitted_pending(ME, now_ms=1) == ()
        assembly.bridge._dispatch_harness_deliveries()
        assert assembly.harnesses.dispatched == []
        assert assembly.facade.pending_messages(ME) == ()
        assert assembly.facade.is_acknowledged("m-drain") is True
    finally:
        assembly.close()


def test_waiting_row_expires_without_dispatch_or_resend(tmp_path: Path) -> None:
    """review B5: waiting has an upper bound; expiry never resends the body."""

    clock = _Clock(now_ms=1_000)
    logs = _Logs()
    gate = ScriptedLinkGate(known={PEER}, default_admit=False)
    assembly = _Assembly(
        tmp_path, slot=_slot(logs, gate), logs=logs, clock_ms=clock.ms
    )
    try:
        expiry = 1_500
        assert assembly.facade.receive(
            _message_with_expiry("m-ttl", expiry), now_ms=1_000
        ).accepted
        assert assembly.facade.pending_messages(ME, now_ms=expiry - 1) != ()

        clock.now_ms = expiry - 1
        assembly.bridge._dispatch_harness_deliveries()
        assert assembly.harnesses.dispatched == []
        assert assembly.facade.has_fetched("m-ttl") is False

        clock.now_ms = expiry + 1
        assembly.bridge._dispatch_harness_deliveries()
        assert assembly.harnesses.dispatched == []

        pruned = assembly.facade.prune_inbox(now_ms=expiry + 1)
        assert [item.message_id for item in pruned] == ["m-ttl"]
        assert assembly.facade.pending_messages(ME, now_ms=expiry + 1) == ()
    finally:
        assembly.close()


def test_offer_coordinator_head_is_held_without_blocking_others(
    tmp_path: Path,
) -> None:
    """review B5: the production offer coordinator does not block on a held head."""

    messages = {
        message_id: _message(message_id, sender=sender)
        for message_id, sender in (
            ("m-held", PEER),
            ("m-owner", OWNER_USER),
            ("m-open", CAROL),
        )
    }

    class Harnesses:
        def __init__(self) -> None:
            self.dispatched: list[str] = []

        def streaming_actors(self) -> tuple[str, ...]:
            return ("bob",)

        def dispatch(self, _actor: str, delivery: object) -> bool:  # type: ignore[no-untyped-def]
            self.dispatched.append(delivery.delivery_id)  # type: ignore[attr-defined]
            return True

    class Inbox:
        def pending_messages(self, _recipient: str) -> tuple[InboxMessage, ...]:
            return tuple(messages.values())

        def admitted_dispatchable(
            self, _recipient: str, *, now_ms: int, exclude: frozenset[str]
        ) -> tuple[InboxMessage, ...]:
            del now_ms
            return tuple(
                message
                for message in messages.values()
                if message.message_id != "m-held"
                and message.message_id not in exclude
            )

        def refresh_hold(self, _message_id: str, **_kwargs: object) -> bool:
            return True

        def system_notices(self, _recipient: str) -> tuple[InboxMessage, ...]:
            return ()

        def dismiss_system_notice(self, _message_id: str) -> bool:
            return True

    harnesses = Harnesses()
    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id=NODE,
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=Inbox(),  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: ME,
        logger=_Logs(),
        actor_mode=True,
    )
    bridge.start()
    try:
        bridge._dispatch_harness_deliveries()
        assert bridge._dispatch_offers is not None
        projection = bridge._dispatch_offers.projection()
        assert projection.pending + projection.completed >= 1
        assert "m-held" not in bridge._inflight
        assert "m-held" not in bridge._attempt_generation
        assert "m-held" not in harnesses.dispatched
    finally:
        bridge.stop()


def test_one_held_sender_does_not_block_others_or_owner(tmp_path: Path) -> None:
    logs = _Logs()
    gate = ScriptedLinkGate(
        known={PEER, CAROL},
        admit={"m-a": False, "m-h": False},
        default_admit=True,
    )
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(_message("m-a", sender=PEER)).accepted
        assert assembly.facade.receive(
            _message("m-b", sender=OWNER_USER)
        ).accepted
        assert assembly.facade.receive(
            _message("m-c", sender=CAROL)
        ).accepted
        assert assembly.facade.receive(
            _message("m-h", sender=PEER, hook_request=True)
        ).accepted

        assembly.bridge._dispatch_harness_deliveries()

        # One coalesced batch carries the owner's and Carol's admissible rows;
        # the held peer rows (m-a, and the hook request m-h) stay durable.
        assert assembly.harnesses.dispatched == ["m-b"]
        assert assembly.harnesses.batches == [("m-b", "m-c")]
    finally:
        assembly.close()


def test_held_row_survives_restart_and_releases(tmp_path: Path) -> None:
    logs = _Logs()
    gate = ScriptedLinkGate(known={PEER}, default_admit=False)
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(_message("m-restart")).accepted

        # A partition or stale copy leaves the row durable; restarting the
        # daemon must not drop it, mint, or mark it refused.
        assembly.restart()
        assert [
            item.message_id
            for item in assembly.facade.pending_messages(ME)
        ] == ["m-restart"]
        restart_record = assembly.facade.delivery_status_for_recipient(
            ME, "m-restart"
        )
        assert restart_record is None or restart_record.state.value != "refused"
        assert assembly.facade.has_fetched("m-restart") is False

        # The peer's reference becomes visible: the SAME row releases without
        # a resend, and it is dispatched exactly once.
        gate.admit["m-restart"] = True
        assembly.bridge._dispatch_harness_deliveries()
        assert assembly.harnesses.dispatched == ["m-restart"]
    finally:
        assembly.close()


def test_unbound_holds_agent_mail_only(tmp_path: Path) -> None:
    logs = _Logs()
    assembly = _Assembly(tmp_path, slot=None, logs=logs)
    try:
        assert assembly.facade.receive(_message("m-agent")).accepted
        assert assembly.facade.receive(
            _message("m-owner", sender=OWNER_USER)
        ).accepted
        assembly.bridge._dispatch_harness_deliveries()
        assert assembly.harnesses.dispatched == ["m-owner"]
        assert logs.named("link.gate_unbound")
    finally:
        assembly.close()

    # A governed reply is gated too (review B1): with a denying gate it stays.
    logs2 = _Logs()
    gate = ScriptedLinkGate(known={PEER}, default_admit=False)
    assembly2 = _Assembly(tmp_path / "reply", slot=_slot(logs2, gate), logs=logs2)
    try:
        assert assembly2.facade.receive(
            _message("m-reply", intent="reply", reply_to="m-agent")
        ).accepted
        assembly2.bridge._dispatch_harness_deliveries()
        assert assembly2.harnesses.dispatched == []
        reply_call = gate.admit_calls()[-1]
        assert reply_call["intent"] == "reply"
        assert reply_call["reply_to"] == "m-agent"
    finally:
        assembly2.close()


@pytest.mark.parametrize(
    ("label", "body"),
    [
        # no replyTo at all
        ("missing", {}),
        # replyTo of the wrong type, and an empty string
        ("wrong-type", {"replyTo": 7}),
        ("empty", {"replyTo": ""}),
        # associations a real gate would reject: another peer's request, a
        # closed correlation, a reclaimed link (review B1.5).  WP4 only
        # forwards them; the scripted gate denies and the row must stay.
        ("other-peer", {"replyTo": "m-other-peer"}),
        ("closed", {"replyTo": "m-closed"}),
        ("reclaimed", {"replyTo": "m-reclaimed"}),
    ],
)
def test_reply_boundary_never_exposes_denied_rows(
    tmp_path: Path, label: str, body: dict[str, object]
) -> None:
    logs = _Logs()
    message_id = f"m-{label}"
    gate = ScriptedLinkGate(known={PEER}, default_admit=False)
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(
            _body_message(message_id, body, intent="reply")
        ).accepted

        assembly.bridge._dispatch_harness_deliveries()

        assert assembly.harnesses.dispatched == []
        assert [
            item.message_id for item in assembly.facade.pending_messages(ME)
        ] == [message_id]
        assert assembly.facade.has_fetched(message_id) is False
        call = gate.admit_calls()[-1]
        assert call["intent"] == "reply"
        expected = body.get("replyTo")
        assert call["reply_to"] == (expected if isinstance(expected, str) and expected else None)
    finally:
        assembly.close()


def test_valid_reply_passes_headless_while_w_is_full(tmp_path: Path) -> None:
    """review B1.5: a valid reply is admitted even when W has no room."""

    logs = _Logs()
    gate = ScriptedLinkGate(
        known={PEER}, admit={"m-valid-reply": True}, default_admit=False
    )
    assembly = _Assembly(tmp_path, slot=_slot(logs, gate), logs=logs)
    try:
        assert assembly.facade.receive(
            _message("m-valid-reply", intent="reply", reply_to="m-open")
        ).accepted
        assert assembly.facade.receive(_message("m-new-request")).accepted

        assembly.bridge._dispatch_harness_deliveries()

        assert assembly.harnesses.dispatched == ["m-valid-reply"]
        reply_calls = [
            call
            for call in gate.admit_calls()
            if call["message_id"] == "m-valid-reply"
        ]
        assert reply_calls and reply_calls[-1]["reply_to"] == "m-open"
    finally:
        assembly.close()
