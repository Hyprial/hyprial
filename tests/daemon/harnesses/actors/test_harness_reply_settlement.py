"""A harness answer settles once, and one deferred settlement cannot sink its
batch.

Live on 2026-09-24 (codex headless agent ``wangshuo-sprite`` on a Lark
adapter): a reply submit timed out after the reply had actually committed, the
row stayed pending, and every re-run turn answered with DIFFERENT text; the
exception escaped ``_complete_harness_results`` after ``drain_results()`` had
released the whole batch, so the other delivery's answer in the same batch was
discarded and both rows were re-dispatched (a paid model turn every cycle).

WP5-A removed the auto-reply (design §8.1): a completed headless turn now
stamps the row, asks ``settle_hint_for`` and acks or keeps — never replies.
The durability hazard survives as settlement deferral: an authority timeout
must defer exactly its own result, never re-run the turn, and never let the
row be dispatched again.  These drive ``_complete_harness_results`` /
``_dispatch_harness_deliveries`` against the daemon's own inbox
(``DeliveryCustodyFacade`` over a ``DeliveryCustodyCoordinator``, as
application.py builds it) so the timeout is the authority's own.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxAuthorityTimeout,
    InboxMessage,
    MemoryDeliveryTransport,
)
from tests.support.links.gate import permissive_admission_slot

NODE = "node-reply"
AGENT = f"agent:op:{NODE}:sprite"
ASKER = f"agent:op:{NODE}:asker"


class _Transport:
    def close(self) -> None:
        return None


class _ScriptedHarnesses:
    """Results handed out once; one streaming actor that records dispatches."""

    def __init__(self) -> None:
        self.results: list[HarnessResult] = []
        self.dispatched: list[str] = []

    def drain_results(self) -> tuple[HarnessResult, ...]:
        drained, self.results = tuple(self.results), []
        return drained

    def streaming_actors(self) -> tuple[str, ...]:
        return ("sprite",)

    def dispatch(self, actor, delivery) -> bool:  # type: ignore[no-untyped-def]
        self.dispatched.append(delivery.delivery_id)
        return True


class _Logs:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def __call__(self, level: str, component: str, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def named(self, event: str) -> list[dict[str, object]]:
        return [fields for name, fields in self.events if name == event]


class _Events:
    def publish(self, _event: object) -> None:
        pass


@pytest.fixture
def inbox(tmp_path: Path) -> Iterator[DeliveryCustodyFacade]:
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _Events(),
        node_id=NODE,
        # sprite/asker are agent-to-agent; keep this suite about reply
        # settlement with an explicit permissive premise (wp4 T13).
        service_options={"link_admission": permissive_admission_slot()},
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    try:
        yield facade
    finally:
        facade.close()


def _bridge(
    tmp_path: Path, inbox: object
) -> tuple[DaemonEventBridge, _ScriptedHarnesses, _Logs]:
    harnesses = _ScriptedHarnesses()
    logs = _Logs()
    bridge = DaemonEventBridge(
        state_dir=tmp_path,
        node_id=NODE,
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: AGENT,
        logger=logs,
    )
    return bridge, harnesses, logs


def _incoming(message_id: str, text: str = "q") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender=ASKER,
        recipient=AGENT,
        payload=json.dumps({"message": text}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _answer(message_id: str, text: str) -> HarnessResult:
    return HarnessResult(message_id, AGENT, HarnessResultStatus.COMPLETED, output=text)


def _pending_ids(inbox: DeliveryCustodyFacade) -> set[str]:
    return {message.message_id for message in inbox.pending_messages(AGENT)}


def test_a_completed_result_for_a_drained_row_settles_idempotently(
    tmp_path: Path, inbox: DeliveryCustodyFacade
) -> None:
    """The row was acked after the answer; a late duplicate turn still settles."""

    bridge, harnesses, logs = _bridge(tmp_path, inbox)
    original = _incoming("m-dm")
    assert inbox.receive(original).accepted
    assert inbox.ack(AGENT, original.message_id).acknowledged
    harnesses.results.append(_answer("m-dm", "a second answer"))

    bridge._complete_harness_results()  # pending is None -> hint ack -> no-op

    assert _pending_ids(inbox) == set()
    assert [fields["messageId"] for fields in logs.named("harness.settle.deferred")] == []
    bridge._dispatch_harness_deliveries()
    assert harnesses.dispatched == []


def test_one_deferred_settlement_does_not_discard_the_rest_of_its_batch(
    tmp_path: Path, inbox: DeliveryCustodyFacade
) -> None:
    """The DM's stamp timed out; the group answer behind it must still settle."""

    bridge, harnesses, _logs = _bridge(tmp_path, flaky := _TimesOutFirstFetch(inbox))
    dm, group = _incoming("m-dm"), _incoming("m-group")
    assert inbox.receive(dm).accepted
    assert inbox.receive(group).accepted
    harnesses.results.extend([_answer("m-dm", "dm answer"), _answer("m-group", "group answer")])

    bridge._complete_harness_results()

    # Only the DM deferred; the group row kept (no gate) in the same tick.
    assert set(bridge._pending_settle_results) == {"m-dm"}
    assert _pending_ids(inbox) == {"m-dm", "m-group"}

    flaky.fail = False
    bridge._complete_harness_results()  # only the deferred result is retried

    assert bridge._pending_settle_results == {}
    assert _pending_ids(inbox) == {"m-dm", "m-group"}  # keep, no gate
    bridge._dispatch_harness_deliveries()
    assert harnesses.dispatched == []  # settled rows are never re-run


class _TimesOutFirstFetch:
    """The real inbox, except the next fetch_pending times out (stamping nothing)."""

    def __init__(self, inner: DeliveryCustodyFacade) -> None:
        self._inner = inner
        self.fail = True
        self.fetches = 0

    def fetch_pending(self, recipient: str, **kwargs: object) -> object:
        self.fetches += 1
        if self.fail:
            self.fail = False
            raise InboxAuthorityTimeout("fetch_pending did not confirm in time")
        return self._inner.fetch_pending(recipient, **kwargs)  # type: ignore[arg-type]

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)


def test_a_deferred_settlement_retries_without_rerunning_the_turn(
    tmp_path: Path, inbox: DeliveryCustodyFacade
) -> None:
    flaky = _TimesOutFirstFetch(inbox)
    bridge, harnesses, logs = _bridge(tmp_path, flaky)
    assert inbox.receive(_incoming("m-dm")).accepted
    harnesses.results.append(_answer("m-dm", "the answer"))

    bridge._complete_harness_results()  # stamp timed out, result deferred
    assert _pending_ids(inbox) == {"m-dm"}
    assert [fields["messageId"] for fields in logs.named("harness.settle.deferred")] == ["m-dm"]

    # Meanwhile the row is still pending, but it must not be dispatched: the
    # turn produced its answer, a dispatch would run the whole model again.
    bridge._dispatch_harness_deliveries()
    assert harnesses.dispatched == []

    flaky.fail = False
    bridge._complete_harness_results()  # same result, settled once: keep

    assert _pending_ids(inbox) == {"m-dm"}  # keep (no gate), never a reply
    assert flaky.fetches == 2
    bridge._dispatch_harness_deliveries()
    assert harnesses.dispatched == []


def test_an_undeferred_pending_row_is_still_dispatched(
    tmp_path: Path, inbox: DeliveryCustodyFacade
) -> None:
    """Control: the dispatch skip is only for answers being held."""

    bridge, harnesses, _logs = _bridge(tmp_path, inbox)
    assert inbox.receive(_incoming("m-new")).accepted

    bridge._dispatch_harness_deliveries()

    assert harnesses.dispatched == ["m-new"]
