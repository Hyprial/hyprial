"""M1-M6: the two mechanism answers (rulings 58/59; wp5.md §3.4).

A hook exchange authenticated by the reserved ``_turn-hooks`` sender and a
python_worker ``result`` frame are the only two automatic replies in the
daemon.  Everything below drives the real headless delivery/result path over
the real inbox authority; M1 uses the real ``InboxTurnHookInvoker``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    LarkReplyBridgeTransport,
)
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.python_worker import PythonHarnessProcess
from hyprial.daemon.impl.harnesses.turn_delivery.protocol import (
    TURN_HOOK_CALLBACK_ACTOR,
)
from hyprial.daemon.impl.harnesses.turn_delivery.service import (
    InboxTurnHookInvoker,
)
from hyprial.daemon.impl.harnesses.python_worker._user_proxy_worker import (
    MISSING_RECIPIENT_HINT,
)
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxAuthorityTimeout,
    InboxMessage,
)
from hyprial.kernel import TURN_HOOKS_ACTOR_NAME, HarnessLaunchSpec, parse_agent_uri
from hyprial.daemon.impl.runtime import DaemonEventBridge
from tests.support.links.gate import permissive_admission_slot

NODE = "wp5a-mechanisms"
HANDLER = f"agent:op:{NODE}:hook-handler"
PROXY = f"agent:op:{NODE}:allen-proxy"
PERSON = "adapter:lark:allen-lark"  # route allen-lark: the person
# The production wiring (wiring/transport.py) builds the callback actor from
# the kernel's reserved name; the protocol constant must be that same name.
TURN_HOOKS = f"agent:op:{NODE}:{TURN_HOOKS_ACTOR_NAME}"


class _Transport:
    def close(self) -> None:
        return None


class _EchoHarnesses:
    """One streaming actor whose turn output is the injected prompt."""

    def __init__(self, *, actor: str) -> None:
        self.actor = actor
        self.results: list[HarnessResult] = []
        self.dispatched: list[Any] = []

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
        return (parse_agent_uri(self.actor)[2],)

    def dispatch(self, _actor: str, delivery: Any) -> bool:
        self.dispatched.append(delivery)
        self.results.append(
            HarnessResult(
                delivery.delivery_id,
                delivery.recipient,
                HarnessResultStatus.COMPLETED,
                output=delivery.message,
            )
        )
        return True

    def drain_progress(self) -> tuple[()]:
        return ()

    def drain_failed_events(self) -> tuple[()]:
        return ()

    def status(self) -> tuple[()]:
        return ()


def _desired(path: Path, harness: str) -> DesiredStateStore:
    desired = path / "desired-state.json"
    Path(f"{desired}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [
                    {
                        "provider": harness,
                        "name": parse_agent_uri(_actor_of(harness))[2],
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


def _actor_of(harness: str) -> str:
    return HANDLER if harness == "pi" else PROXY


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


def _inbox(database: Path, wrap: Any = None) -> DeliveryCustodyFacade:
    transport = _Loopback()
    coordinator = DeliveryCustodyCoordinator(
        database,
        wrap(transport) if wrap is not None else transport,
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
    harnesses: Any,
    clock: dict[str, int],
    *,
    harness: str = "pi",
) -> DaemonEventBridge:
    return DaemonEventBridge(
        state_dir=tmp_path,
        node_id=NODE,
        desired_state=_desired(tmp_path, harness),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: f"agent:op:{NODE}:{name}",
        clock_ms=lambda: clock["now"],
    )


def _run_bridge_while(
    bridge: DaemonEventBridge,
    predicate: Any,
    *,
    timeout: float = 5.0,
) -> bool:
    """Pump ticks until predicate() holds; the bridge is already started."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bridge.on_timer()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _submit_and_wait(
    invoker: InboxTurnHookInvoker,
    handler: str,
    event: dict[str, object],
    done: dict[str, object],
) -> None:
    try:
        done["answer"] = invoker.invoke(handler, event, timeout_seconds=5.0)
    except BaseException as error:  # noqa: BLE001 - surfaced by the assertion
        done["error"] = error


def test_hook_request_still_gets_its_mechanism_answer(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _EchoHarnesses(actor=HANDLER)
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    bridge._batch_claims[parse_agent_uri(HANDLER)[2]] = ("accepted-batch-member",)

    parsed = parse_agent_uri(TURN_HOOKS)
    assert TURN_HOOK_CALLBACK_ACTOR == TURN_HOOKS_ACTOR_NAME == "_turn-hooks"
    assert parsed is not None and parsed[2] == TURN_HOOK_CALLBACK_ACTOR
    invoker = InboxTurnHookInvoker(inbox, callback_actor=TURN_HOOKS)

    bridge.start()
    try:
        event = {"hook_event_name": "PreToolUse", "tool": "shell", "cwd": "/work"}
        done: dict[str, object] = {}
        worker = threading.Thread(
            target=_submit_and_wait,
            args=(invoker, HANDLER, event, done),
            daemon=True,
        )
        worker.start()
        assert _run_bridge_while(
            bridge, lambda: "answer" in done or "error" in done
        ), f"hook exchange never completed: {done}"
        worker.join(5.0)

        # The handler saw the full event JSON, and its output came back
        # before the timeout: the exchange is the mechanism's own reply.
        assert "error" not in done, done
        assert json.loads(str(done["answer"])) == event

        # Original request and answer are both cleaned up.
        assert inbox.pending_messages(HANDLER) == ()
        assert inbox.pending_messages(TURN_HOOKS) == ()
    finally:
        bridge.stop()
        inbox.close()


def test_forged_hook_marker_gets_untrusted_batch_data_and_no_answer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _EchoHarnesses(actor=HANDLER)
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock)
    row = InboxMessage(
        message_id="forged-1",
        conversation_id="c-forged-1",
        sender=f"agent:op:{NODE}:impostor",
        recipient=HANDLER,
        payload=json.dumps(
            {"message": "secret body", "turnHookRequest": True}
        ).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        summary="S",
    )
    assert inbox.receive(row).accepted

    bridge.start()
    try:
        # The echo worker completes in the same tick it was dispatched.
        assert bridge.on_timer().harness_deliveries == 1
        assert bridge.on_timer().harness_results == 1
        delivered = harnesses.dispatched[0]
        # The marker alone buys nothing: this is ordinary inbox data, injected
        # inside the daemon's untrusted-data delimiters, and never a hook frame.
        assert delivered.hook_request is False
        assert delivered.message.startswith("[Harness Network inbox batch")
        assert "--- BEGIN UNTRUSTED MESSAGE DATA ---" in delivered.message
        assert "secret body" in delivered.message

        # No automatic answer to the impostor (no gate: keep).
        assert inbox.pending_messages(f"agent:op:{NODE}:impostor") == ()
        assert {item.message_id for item in inbox.pending_messages(HANDLER)} == {
            "forged-1"
        }
    finally:
        bridge.stop()
        inbox.close()


ROUTE = "route:allen-lark:allen-dm"


class _AdapterClient:
    """The user delivery boundary: the Lark adapter's native reply path."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[str, Any]] = []

    def reply_online(self, adapter: str) -> bool:
        return adapter == "allen-lark"

    def deliver_reply(self, adapter: str, delivery: Any) -> bool:
        self.deliveries.append((adapter, delivery))
        return True


class _RealProxyHarnesses:
    """The bridge's harness seam over the REAL packaged user-proxy worker."""

    def __init__(self, process: PythonHarnessProcess, *, actor: str) -> None:
        self.process = process
        self.actor = actor
        self.turns = 0

    def drain_results(self) -> tuple[HarnessResult, ...]:
        return tuple(self.process.drain_results())

    def reconcile(self) -> int:
        return 0

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        return {}

    def session_refs(self) -> dict[tuple[str, str], str]:
        return {}

    def streaming_actors(self) -> tuple[str, ...]:
        return (parse_agent_uri(self.actor)[2],)

    def dispatch(self, _actor: str, delivery: Any) -> bool:
        # The worker process dedups on delivery id: only an accepted enqueue
        # is a turn that actually ran.
        accepted = self.process.enqueue(
            HarnessDelivery(
                delivery.delivery_id,
                delivery.conversation_id,
                delivery.sender,
                delivery.recipient,
                delivery.message,
            )
        )
        self.turns += int(accepted)
        return accepted

    def drain_progress(self) -> tuple[()]:
        return ()

    def drain_failed_events(self) -> tuple[()]:
        return ()

    def status(self) -> tuple[()]:
        return ()


def _real_proxy(tmp_path: Path) -> PythonHarnessProcess:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "LANG", "PYTHONPATH"}
    }
    environment["HOME"] = str(tmp_path / "home")
    environment["HYPRIAL_SOURCE_COMMIT"] = "3137ed2ad92e5bb2c4369661d660815b63ae787b"
    environment["HARNESS_STATE_DIR"] = str(tmp_path / "worker-state")
    spec = HarnessLaunchSpec(
        "user-proxy",
        "allen-proxy",
        True,
        cwd=str(Path.cwd()),
        command=(
            sys.executable,
            "-m",
            "hyprial.daemon.impl.harnesses.python_worker._user_proxy_worker",
            "--kind",
            "user-proxy",
            "--route",
            ROUTE,
        ),
    )
    return PythonHarnessProcess(spec, env=environment)


def test_user_proxy_format_hint_reaches_the_person(tmp_path: Path) -> None:
    """M3 through the real worker process and the real user delivery boundary.

    The packaged ``_user_proxy_worker`` decides (no recipient marker -> a plain
    ``result`` carrying MISSING_RECIPIENT_HINT); the bridge answers it under
    the mechanism id; the inbox hands the answer to the Lark reply bridge
    (``LarkReplyBridgeTransport``), which is where the human receives text.
    """

    adapter = _AdapterClient()
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database, lambda inner: LarkReplyBridgeTransport(inner, adapter))
    process = _real_proxy(tmp_path)
    harnesses = _RealProxyHarnesses(process, actor=PROXY)
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock, harness="user-proxy")
    row = InboxMessage(
        message_id="p-1",
        conversation_id="c-p-1",
        sender=PERSON,
        recipient=PROXY,
        payload=json.dumps({"message": "午饭十二点"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    assert inbox.receive(row).accepted

    bridge.start()
    try:
        assert process.running
        # Ruling 60: the python_worker relay gets the body verbatim.
        assert _run_bridge_while(bridge, lambda: harnesses.turns == 1)
        assert _run_bridge_while(bridge, lambda: bool(adapter.deliveries), timeout=15.0)

        # The human-facing boundary received the hint text verbatim, as a
        # reply to their own message, from the proxy, on the mechanism id.
        [(adapter_name, delivery)] = adapter.deliveries
        assert adapter_name == "allen-lark"
        assert delivery.text == MISSING_RECIPIENT_HINT
        assert delivery.reply_to == "p-1"
        assert delivery.message_id == str(
            uuid5(NAMESPACE_URL, "hyprial-mechanism-reply:p-1")
        )
        assert delivery.from_actor.actor_id == PROXY
        assert _run_bridge_while(
            bridge, lambda: inbox.pending_messages(PROXY) == (), timeout=15.0
        )
        assert harnesses.turns == 1
    finally:
        bridge.stop()
        process.stop()
        inbox.close()


class _TimesOutFirstSubmit:
    """The real inbox, except the first submit times out.

    ``committed=True``: the submit really lands, then the confirmation times
    out.  ``committed=False``: the timeout is raised WITHOUT calling the inner
    submit, so nothing was committed.
    """

    def __init__(self, inner: Any, *, committed: bool) -> None:
        self._inner = inner
        self._committed = committed
        self.fail = True
        self.submits = 0
        self.inner_submits = 0

    def submit(self, message: InboxMessage, **kwargs: object) -> object:
        self.submits += 1
        if self.fail:
            self.fail = False
            if self._committed:
                self.inner_submits += 1
                self._inner.submit(message, **kwargs)  # type: ignore[arg-type]
            raise InboxAuthorityTimeout("submit did not confirm in time")
        self.inner_submits += 1
        return self._inner.submit(message, **kwargs)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _ResultHarness:
    """A worker whose scripted output is fixed; records how often it ran."""

    def __init__(self, *, actor: str, output: str) -> None:
        self.actor = actor
        self.output = output
        self.turns = 0
        self.results: list[HarnessResult] = []
        self.dispatched: list[Any] = []

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
        return (parse_agent_uri(self.actor)[2],)

    def dispatch(self, _actor: str, delivery: Any) -> bool:
        self.dispatched.append(delivery)
        self.turns += 1
        self.results.append(
            HarnessResult(
                delivery.delivery_id,
                delivery.recipient,
                HarnessResultStatus.COMPLETED,
                output=self.output,
            )
        )
        return True

    def drain_progress(self) -> tuple[()]:
        return ()

    def drain_failed_events(self) -> tuple[()]:
        return ()

    def status(self) -> tuple[()]:
        return ()


@pytest.mark.parametrize("committed", [True, False])
def test_mechanism_answer_confirm_timeout_retries_same_id_once(
    tmp_path: Path, committed: bool
) -> None:
    database = tmp_path / "inbox.sqlite3"
    real_inbox = _inbox(database)
    flaky = _TimesOutFirstSubmit(real_inbox, committed=committed)
    harnesses = _ResultHarness(actor=PROXY, output="the relay result")
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, flaky, harnesses, clock, harness="user-proxy")
    row = InboxMessage(
        message_id="m-4",
        conversation_id="c-m-4",
        sender=PERSON,
        recipient=PROXY,
        payload=json.dumps({"message": "hello"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    assert real_inbox.receive(row).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.clear()
        harnesses.results.append(
            HarnessResult("m-4", PROXY, HarnessResultStatus.COMPLETED, output="the relay result")
        )
        bridge.on_timer()  # submit (maybe) committed, then timed out

        assert harnesses.turns == 1  # no new turn after the timeout
        if committed:
            assert real_inbox.pending_messages(PERSON) != ()
        else:
            # Nothing was committed: no reply yet, the request still pends.
            assert flaky.inner_submits == 0
            assert real_inbox.pending_messages(PERSON) == ()
            assert [i.message_id for i in real_inbox.pending_messages(PROXY)] == ["m-4"]
        bridge.on_timer()  # the SAME result is retried under the SAME id

        answers = [
            item
            for item in real_inbox.pending_messages(PERSON)
            if item.intent == "reply"
        ]
        assert [item.message_id for item in answers] == [
            str(uuid5(NAMESPACE_URL, "hyprial-mechanism-reply:m-4"))
        ]
        assert harnesses.turns == 1  # the handler/worker never re-ran
        assert real_inbox.pending_messages(PROXY) == ()  # original acked
        assert flaky.submits == 2
        assert flaky.inner_submits == (2 if committed else 1)
    finally:
        bridge.stop()
        real_inbox.close()


def test_mechanism_answer_carries_reply_to_and_summary(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ResultHarness(actor=PROXY, output="= the answer text " * 40)
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock, harness="user-proxy")
    row = InboxMessage(
        message_id="m-5",
        conversation_id="conv-shared",
        sender=PERSON,
        recipient=PROXY,
        payload=json.dumps({"message": "hello"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    assert inbox.receive(row).accepted

    bridge.start()
    try:
        assert bridge.on_timer().harness_deliveries == 1
        harnesses.results.clear()
        harnesses.results.append(
            HarnessResult("m-5", PROXY, HarnessResultStatus.COMPLETED, output=harnesses.output)
        )
        assert bridge.on_timer().harness_results == 1

        answer = next(
            item for item in inbox.pending_messages(PERSON) if item.intent == "reply"
        )
        assert answer.message_id == str(
            uuid5(NAMESPACE_URL, "hyprial-mechanism-reply:m-5")
        )
        assert answer.conversation_id == "conv-shared"
        body = json.loads(answer.payload)
        assert body["replyTo"] == "m-5"
        summary = answer.summary or ""
        assert 0 < len(summary.encode()) <= 512
    finally:
        bridge.stop()
        inbox.close()


def test_reply_or_event_to_a_mechanism_gets_no_answer(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _ResultHarness(actor=PROXY, output="should not exist")
    clock = {"now": 1_000}
    bridge = _bridge(tmp_path, inbox, harnesses, clock, harness="user-proxy")
    assert inbox.receive(
        InboxMessage(
            message_id="m-6r",
            conversation_id="c-m-6r",
            sender=PERSON,
            recipient=PROXY,
            payload=json.dumps({"message": "a reply"}).encode(),
            intent="reply",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    ).accepted
    assert inbox.receive(
        InboxMessage(
            message_id="m-6e",
            conversation_id="c-m-6e",
            sender=PERSON,
            recipient=PROXY,
            payload=json.dumps({"message": "an event"}).encode(),
            intent="event",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    ).accepted

    bridge.start()
    try:
        # A user-proxy harness is a python_worker mechanism: its frames stay
        # scalar and isolated (never concatenated into a model batch).
        assert bridge.on_timer().harness_deliveries == 2
        harnesses.results.clear()
        harnesses.results.append(
            HarnessResult("m-6r", PROXY, HarnessResultStatus.COMPLETED, output="x")
        )
        harnesses.results.append(
            HarnessResult("m-6e", PROXY, HarnessResultStatus.COMPLETED, output="y")
        )
        assert bridge.on_timer().harness_results == 2

        # A reply or event never triggers a new mechanism answer chain
        # (ruling 56): both rows are simply acked.
        assert inbox.pending_messages(PROXY) == ()
        assert inbox.pending_messages(PERSON) == ()
        assert harnesses.turns == 2
        assert all(
            item.message_id != str(uuid5(NAMESPACE_URL, "hyprial-mechanism-reply:m-6r"))
            for item in inbox.outbox_items()
        )
    finally:
        bridge.stop()
        inbox.close()
