"""R1, R2, R5-R7: keep's recovery, release and non-starvation (wp5.md §7.5).

A kept request stays pending and stamped; settlement resumes after an
interruption without re-running the turn; and the durable inbox state alone
must keep a restart from amplifying the backlog.  R5/R6 need the real WP2
link service (real gate, W accounting, reclaim and abandon thresholds), which
is not merged on this branch: they are strict xfails until it lands.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxAuthorityTimeout,
    InboxMessage,
)
from hyprial.daemon.impl.links.ports import Allow
from hyprial.daemon.impl.runtime import DaemonEventBridge
from tests.support.links.gate import permissive_admission_slot

NODE = "wp5a-keep"
AGENT = f"agent:op:{NODE}:sprite"
SENDER = f"agent:op:{NODE}:asker"


class _Transport:
    def close(self) -> None:
        return None


class _ScriptedHarnesses:
    def __init__(self) -> None:
        self.results: list[HarnessResult] = []
        self.dispatched: list[Any] = []
        self.turns = 0

    def drain_results(self) -> tuple[HarnessResult, ...]:
        drained, self.results = tuple(self.results), []
        return drained

    def reconcile(self) -> int:
        return 0

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        return {}

    def session_refs(self) -> dict[tuple[str, str], str]:
        return {}

    def streaming_actors(self) -> tuple[str, ...]:
        return ("sprite",)

    def dispatch(self, _actor: str, delivery: Any) -> bool:
        self.dispatched.append(delivery)
        self.turns += 1
        return True

    def drain_progress(self) -> tuple[()]:
        return ()

    def drain_failed_events(self) -> tuple[()]:
        return ()

    def status(self) -> tuple[()]:
        return ()


class _Loopback:
    """Delivers straight back into this daemon's own inbox (one node)."""

    def __init__(self) -> None:
        self.service: Any = None

    def bind(self, service: Any) -> None:
        self.service = service

    def is_online(self, recipient: str) -> bool:
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        self.service.receive(message)
        return self.service.has_received(message.message_id)

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


def _desired(path: Path) -> DesiredStateStore:
    desired = path / "desired-state.json"
    Path(f"{desired}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [
                    {
                        "provider": "pi",
                        "name": "sprite",
                        "headless": True,
                        "args": [],
                        "ownership": "managed",
                    }
                ],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    return DesiredStateStore(desired)


def _inbox(database: Path, *, gate: object | None = None) -> DeliveryCustodyFacade:
    transport = _Loopback()
    coordinator = DeliveryCustodyCoordinator(
        database,
        transport,
        type("_Events", (), {"publish": staticmethod(lambda _event: None)})(),
        node_id=NODE,
        service_options={"link_admission": permissive_admission_slot()},
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    transport.bind(facade)
    return facade


def _bridge(
    tmp_path: Path,
    inbox: object,
    harnesses: _ScriptedHarnesses,
    clock: dict[str, int],
    *,
    gate: object | None = None,
) -> DaemonEventBridge:
    slot = None
    if gate is not None:
        slot = LinkAdmission()
        slot.bind(gate)
    return DaemonEventBridge(
        state_dir=tmp_path,
        node_id=NODE,
        desired_state=_desired(tmp_path),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: f"agent:op:{NODE}:{name}",
        clock_ms=lambda: clock["now"],
        link_admission=slot,
    )


def _message(
    message_id: str,
    *,
    intent: str = "request",
    created_at_ms: int = 1,
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender=SENDER,
        recipient=AGENT,
        payload=json.dumps({"message": f"prompt {message_id}"}).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )


def _pending(inbox: DeliveryCustodyFacade) -> set[str]:
    return {item.message_id for item in inbox.pending_messages(AGENT)}


def _fetched(database: Path, message_id: str) -> int | None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT fetched_at_ms FROM inbox WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _expires(database: Path, message_id: str) -> tuple[int | None]:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT expires_at_ms FROM inbox WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row is not None
    return (None if row[0] is None else int(row[0]),)


class _AckGate:
    """A gate that always answers ack (an explicit premise, like wp4 T13)."""

    def admit_send(self, **_kwargs: object) -> Allow:
        return Allow()

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        return True

    def admit_dispatch(self, **_kwargs: object) -> bool:
        return True

    def settle_hint(self, *, actor: str, message_id: str) -> str:
        return "ack"


class _FailsNthFetch:
    """Fails the Nth (1-based) fetch_pending with an authority timeout."""

    def __init__(self, inner: Any, n: int) -> None:
        self._inner = inner
        self._n = n
        self.calls = 0

    def fetch_pending(self, recipient: str, **kwargs: object) -> object:
        self.calls += 1
        if self.calls == self._n:
            raise InboxAuthorityTimeout("fetch_pending did not confirm in time")
        return self._inner.fetch_pending(recipient, **kwargs)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _AckBreaks:
    """The first ack of a given id breaks at ``when`` ('before'/'after' commit)."""

    def __init__(self, inner: Any, *, when: str) -> None:
        self._inner = inner
        self._when = when
        self.broken = False

    def ack(self, recipient: str, message_id: str) -> object:
        if not self.broken:
            self.broken = True
            if self._when == "after":
                self._inner.ack(recipient, message_id)  # the commit survives
            raise InboxAuthorityTimeout("ack did not confirm in time")
        return self._inner.ack(recipient, message_id)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _kept_request(
    tmp_path: Path,
    database: Path,
    harnesses: _ScriptedHarnesses,
    clock: dict[str, int],
    message_id: str,
) -> tuple[DaemonEventBridge, DeliveryCustodyFacade]:
    """Dispatch one request and settle it to keep; caller owns the bridge."""

    inbox = _inbox(database)
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    assert inbox.receive(_message(message_id), now_ms=clock["now"]).accepted
    bridge.start()
    assert bridge.on_timer().harness_deliveries == 1
    harnesses.results.append(
        HarnessResult(message_id, AGENT, HarnessResultStatus.COMPLETED, output="done")
    )
    assert bridge.on_timer().harness_results == 1
    return bridge, inbox


def test_keep_survives_restart_without_redispatch(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    inbox = _inbox(database)
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    bridge.start()
    try:
        for message_id in ("k1", "k2", "k3"):
            assert inbox.receive(_message(message_id), now_ms=clock["now"]).accepted
            assert bridge.on_timer().harness_deliveries == 1
            harnesses.results.append(
                HarnessResult(
                    message_id, AGENT, HarnessResultStatus.COMPLETED, output="done"
                )
            )
            assert bridge.on_timer().harness_results == 1
        assert _pending(inbox) == {"k1", "k2", "k3"}
    finally:
        bridge.stop()
        inbox.close()

    # Rebuild bridge and authority over the same SQLite file.
    harnesses = _ScriptedHarnesses()
    clock = {"now": 9_000}
    inbox = _inbox(database)
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    bridge.start()
    try:
        assert _pending(inbox) == {"k1", "k2", "k3"}
        for message_id in ("k1", "k2", "k3"):
            assert _fetched(database, message_id) is not None

        clock["now"] = 10_000
        assert bridge.on_timer().harness_deliveries == 0  # kept: never re-sent
        assert harnesses.turns == 0

        # A fresh, never-fetched request dispatches normally.
        assert inbox.receive(_message("fresh"), now_ms=clock["now"]).accepted
        clock["now"] = 11_000
        assert bridge.on_timer().harness_deliveries == 1
        assert [item.delivery_id for item in harnesses.dispatched] == ["fresh"]
    finally:
        bridge.stop()
        inbox.close()


@pytest.mark.parametrize("breakpoint", ["stamp", "hint", "ack-confirm"])
def test_settle_interruption_resumes_settlement_only(
    tmp_path: Path, breakpoint: str
) -> None:
    """Each breakpoint defers its own result only; resume never re-runs turn."""

    database = tmp_path / "inbox.sqlite3"
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}

    real_inbox = _inbox(database)
    if breakpoint == "stamp":
        gate = None
        flaky: Any = _FailsNthFetch(real_inbox, 1)
    elif breakpoint == "hint":
        gate = _AckGate()
        flaky = _AckBreaks(real_inbox, when="before")
    else:
        gate = _AckGate()
        flaky = _AckBreaks(real_inbox, when="after")
    bridge = _bridge(tmp_path, flaky, harnesses, clock, gate=gate)
    assert real_inbox.receive(_message("r1")).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.append(
            HarnessResult("r1", AGENT, HarnessResultStatus.COMPLETED, output="done")
        )
        turns_after_dispatch = harnesses.turns
        bridge.on_timer()  # the settlement hits its breakpoint

        if breakpoint == "stamp":
            assert not bridge._pending_settle_results
            assert _fetched(database, "r1") is not None
        else:
            assert set(bridge._pending_settle_results) == {"r1"}
        assert harnesses.turns == turns_after_dispatch  # no turn re-run

        bridge.on_timer()  # resume: the same result, settlement only

        assert bridge._pending_settle_results == {}
        assert harnesses.turns == turns_after_dispatch
        if breakpoint == "stamp":
            # keep: pending and stamped, never re-dispatched.
            assert _fetched(database, "r1") is not None
            assert "r1" in _pending(real_inbox)
            bridge._dispatch_harness_deliveries()
            assert harnesses.turns == turns_after_dispatch
        else:
            # the ack hint was executed (or proven committed): row consumed.
            assert "r1" not in _pending(real_inbox)
    finally:
        bridge.stop()
        real_inbox.close()


@pytest.mark.xfail(strict=True, reason="BLOCKED-on-WP2")
def test_kept_request_holds_the_window_until_explicit_close(
    tmp_path: Path,
) -> None:
    """R5 needs the real WP2 gate: W accounting, reclaim and explicit close.

    A kept request occupies the recipient's W until an explicit reply or ack;
    a fresh send without replyTo opens a new item and closes nothing; an
    unadmitted request is never injected.  None of this is assertable without
    the real link service, so this stays a strict xfail until WP2 lands.
    """

    from hyprial.daemon.impl.links.service import LinkService

    gate = LinkService(...)
    assert gate.settle_hint(actor=AGENT, message_id="k1") == "keep"
    pytest.fail("BLOCKED-on-WP2: real gate assertions pending the link service")


@pytest.mark.xfail(strict=True, reason="BLOCKED-on-WP2")
def test_drain_and_abandon_release_kept_requests(tmp_path: Path) -> None:
    """R6 needs the real gate's reclaim/abandon path (BLOCKED-on-WP2).

    After a reclaim the sender's reply answers LINK_RECLAIMED and the kept
    item releases; the abandon threshold and TTL free pending rows and
    capacity without re-sending the original request.
    """

    from hyprial.daemon.impl.links.service import LinkService

    gate = LinkService(...)
    assert gate.settle_hint(actor=AGENT, message_id="k1") == "keep"
    pytest.fail("BLOCKED-on-WP2: real gate assertions pending the link service")


@pytest.mark.parametrize("gate_answers_ack", [False, True])
def test_startup_sweep_settles_rows_stamped_before_a_crash(
    tmp_path: Path, gate_answers_ack: bool
) -> None:
    """R8 (ruling 74, P4): stamp -> crash -> restart settles by the one hint.

    The first life stamped ``fetched`` (``completed_turn``) and died before
    its ack.  The restart's one-shot sweep asks ``settle_hint_for``: ack rows
    are acked, keep rows are left alone, and the model never runs again.
    """

    database = tmp_path / "inbox.sqlite3"
    clock = {"now": 1_000}

    # First life: three results got as far as the stamp, then a crash.
    first = _inbox(database)
    for message_id, intent in (("r1", "reply"), ("e1", "event"), ("q1", "request")):
        assert first.receive(_message(message_id, intent=intent)).accepted
        first.fetch_pending(
            AGENT, only=frozenset({message_id}), completed_turn=True
        )
    # An unstamped sibling must stay dispatchable, not swept.
    assert first.receive(_message("fresh")).accepted
    assert _pending(first) == {"r1", "e1", "q1", "fresh"}
    first.close()

    harnesses = _ScriptedHarnesses()
    inbox = _inbox(database)
    bridge = _bridge(
        tmp_path, inbox, harnesses, clock, gate=_AckGate() if gate_answers_ack else None
    )
    bridge.start()
    try:
        expected_kept = {"fresh"} if gate_answers_ack else {"q1", "fresh"}
        assert _pending(inbox) == expected_kept
        before = _fetched(database, "q1")
        for _ in range(3):
            clock["now"] += 1_000
            bridge.on_timer()
        # Only the never-fetched row was dispatched; no stamped row re-ran.
        assert {item.delivery_id for item in harnesses.dispatched} == {"fresh"}
        assert _fetched(database, "q1") == before  # keep rows untouched
    finally:
        bridge.stop()
        inbox.close()


def test_expired_kept_row_does_not_block_new_work(tmp_path: Path) -> None:
    """R6's TTL half is real today: expiry sweeps the kept row and frees the FIFO."""

    database = tmp_path / "inbox.sqlite3"
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    bridge, inbox = _kept_request(tmp_path, database, harnesses, clock, "k1")
    try:
        assert _pending(inbox) == {"k1"}

        # Past the row's own TTL deadline, the sweep retires the kept row...
        (deadline,) = _expires(database, "k1")
        assert deadline is not None
        pruned = inbox.prune_inbox(now_ms=int(deadline) + 1)
        assert [item.message_id for item in pruned] == ["k1"]
        assert _pending(inbox) == set()

        # ...and no automatic re-send happens: the row is gone, not queued.
        clock["now"] = int(deadline) + 2
        assert bridge.on_timer().harness_deliveries == 0
        assert harnesses.turns == 1

        # New work is not blocked by the expired row.
        assert inbox.receive(
            _message("after", created_at_ms=int(deadline) + 2),
            now_ms=int(deadline) + 2,
        ).accepted
        assert bridge.on_timer().harness_deliveries == 1
    finally:
        bridge.stop()
        inbox.close()


def test_many_requests_do_not_starve_each_other(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    harnesses = _ScriptedHarnesses()
    clock = {"now": 1_000}
    real_inbox = _inbox(database)
    flaky = _FailsNthFetch(real_inbox, 1)  # the first settlement defers once
    bridge = _bridge(tmp_path, flaky, harnesses, clock)
    for message_id, intent in (
        ("a", "request"),
        ("b", "request"),
        ("c", "reply"),
        ("d", "event"),
    ):
        assert real_inbox.receive(_message(message_id, intent=intent)).accepted

    bridge.start()
    try:
        # One batch carries all four rows.
        assert bridge.on_timer().harness_deliveries == 1
        for message_id in ("a", "b", "c", "d"):
            harnesses.results.append(
                HarnessResult(
                    message_id, AGENT, HarnessResultStatus.COMPLETED, output="x"
                )
            )
        bridge.on_timer()  # fetch retry succeeds; all four results settle

        # c and d are acked (ruling 56) with the rest of the batch.
        assert "c" not in _pending(real_inbox)
        assert "d" not in _pending(real_inbox)
        assert "b" in _pending(real_inbox)  # keep (no gate)
        assert not bridge._pending_settle_results
        assert _pending(real_inbox) == {"a", "b"}

        # The notice never becomes an owed-reply item: it lives outside the
        # inbox request table, so no settlement ever targets it.
        assert real_inbox.receive_system_notice(
            InboxMessage(
                message_id="e",
                conversation_id="c-e",
                sender=SENDER,
                recipient=AGENT,
                payload=json.dumps({"message": "notice"}).encode(),
                intent="system",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=1,
            )
        )
        bridge._dispatch_harness_deliveries()
        bridge.on_timer()
        assert _pending(real_inbox) == {"a", "b"}
        assert bridge._pending_settle_results == {}
    finally:
        bridge.stop()
        real_inbox.close()


def test_the_sweep_is_what_settles_the_stamped_rows(tmp_path: Path, monkeypatch) -> None:
    """Negative control for the E26-11 restart leg and R8's positive test.

    The positive test above ("startup sweep settles rows stamped before a
    crash") only reads as evidence if disabling the sweep changes its
    observable outcome.  Here the sweep is replaced with a no-op and the very
    same stamped reply/event rows must stay pending: if this control ever
    passed, the positive test would be asserting a property that holds with
    the mechanism off, which is exactly what the 2026-10-15 acceptance caught
    in the E2E-026 runner.
    """

    from hyprial.daemon.impl.runtime import results as runtime_results

    database = tmp_path / "inbox.sqlite3"
    clock = {"now": 1_000}
    first = _inbox(database)
    for message_id, intent in (("r1", "reply"), ("e1", "event")):
        assert first.receive(_message(message_id, intent=intent)).accepted
        first.fetch_pending(AGENT, only=frozenset({message_id}), completed_turn=True)
    first.close()

    harnesses = _ScriptedHarnesses()
    inbox = _inbox(database)
    bridge = _bridge(tmp_path, inbox, harnesses, clock, gate=_AckGate())
    monkeypatch.setattr(
        runtime_results._BridgeResultsMixin,
        "_settle_stranded_rows",
        lambda self, harnesses: 0,
    )
    bridge.start()
    try:
        # With the sweep disabled the ack-answerable rows survive, so the
        # positive assertion ({"r1","e1"} gone) would be red.  The request is
        # kept either way; the reply/event rows are the discriminating part.
        assert _pending(inbox) == {"r1", "e1"}
        assert harnesses.dispatched == []
    finally:
        bridge.stop()
        inbox.close()
