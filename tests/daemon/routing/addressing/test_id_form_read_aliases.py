"""UUID addressing lane A: rows at a name URI are not an id-minted agent's.

Inbox rows queued at ``agent:<owner>:<node>:<actor>`` are not backfilled,
and since the owner decision of 2026-11-19 (a reused name is a new agent)
an agent minted under its ``a.`` id reads only its own address: a name-URI
row is not listed, reported or woken for, so a later agent with the same
name never drains mail addressed to an earlier one.  The consumer plumbing
still reads several keys when it is given them (headless batching below).
The desired-state interactive session normalization that migrates session
actors runs after the identity registry (and the identity migration it
runs) is open.
"""

from __future__ import annotations

from tests.support.runtime.cell_delivery import AdmittingCellDelivery

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.actors.agents_registry import (
    _AgentsRegistryMixin,
)
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.desired_state import InteractiveSession
from hyprial.identity import AgentRegistry
from hyprial.identity.impl.agents import addressing
from hyprial.kernel import canonical_agent_uri
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _Presence,
    _close_application,
    _isolated_application,
)
from tests.daemon.routing.addressing.test_id_form_presence import (
    NAME_URI,
    NODE,
    OWNER,
    id_form as id_form,
)


def _application(tmp_path: Path, request):
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        network_isolated=True,
    )
    request.addfinalizer(lambda: _close_application(app))
    return app


@pytest.fixture
def flipped(tmp_path: Path, request, id_form):
    app = _application(tmp_path, request)
    inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
    )
    app._inbox = inbox  # type: ignore[assignment]
    app._harnesses = SimpleNamespace(status=lambda: ())  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    agent = app.agents.create("worker")
    id_uri = canonical_agent_uri(OWNER, NODE, agent.agent_id)
    assert agent.uri == id_uri
    pre_flip = InboxMessage(
        message_id="pre-flip",
        conversation_id="conversation-pre-flip",
        sender="channel:lark:test",
        recipient=NAME_URI,
        payload=json.dumps({"message": "queued before the flip"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    assert inbox.receive(pre_flip).accepted
    return app, id_uri


@pytest.mark.parametrize("spelling", ["worker", "id"])
def test_a_name_uri_row_is_not_listed_for_the_id_minted_agent(flipped, spelling) -> None:
    app, id_uri = flipped
    actor = id_uri if spelling == "id" else spelling

    assert app.handle("message.pending.list", {"actor": actor})["messages"] == []


def test_consumer_read_keys_are_the_address_alone(flipped) -> None:
    app, id_uri = flipped

    assert app._consumer_read_keys(id_uri) == (id_uri,)
    assert app._consumer_read_keys(NAME_URI) == (NAME_URI,)
    assert app._agent_address("worker") == id_uri


def test_delivery_status_does_not_read_the_name_uri(flipped) -> None:
    app, id_uri = flipped
    asked: list[str] = []

    def records(key, *, message_id=None):
        asked.append(key)
        return ()

    app._inbox = SimpleNamespace(  # type: ignore[assignment]
        delivery_status_records=records
    )
    app._transport = None

    app.handle("message.status", {"actor": "worker"})
    assert asked == [id_uri, "worker"]


def test_dormant_wake_ignores_work_queued_at_the_name_uri(flipped) -> None:
    app, id_uri = flipped
    asked: list[str] = []

    class _Queue:
        def has_pending_work(self, recipient: str) -> bool:
            asked.append(recipient)
            return recipient == NAME_URI

    app._inbox = _Queue()  # type: ignore[assignment]

    assert app._pending_restore_work(app.agents.require("worker")) is False
    assert asked == [id_uri, "worker"]


def test_switch_off_reads_no_extra_key(
    tmp_path: Path, request, monkeypatch
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    app = _application(tmp_path, request)
    app.agents.create("worker")
    asked: list[str] = []

    class _Queue:
        def has_pending_work(self, recipient: str) -> bool:
            asked.append(recipient)
            return False

    app._inbox = _Queue()  # type: ignore[assignment]

    assert app._pending_restore_work(app.agents.require("worker")) is False
    assert asked == [NAME_URI, "worker"]


def test_session_normalization_runs_after_the_identity_registry_opens(
    tmp_path: Path, request, id_form, monkeypatch
) -> None:
    seed = _application(tmp_path, request)
    id_uri = seed.agents.create("worker").uri
    seed.desired_state.register_interactive(
        InteractiveSession(
            actor=NAME_URI,
            cwd="/work",
            command=("claude",),
            source="claude-channel",
            session_ref="pre-flip-session",
        )
    )
    _close_application(seed)
    order: list[str] = []
    opened = AgentRegistry.__init__
    normalize = _AgentsRegistryMixin._normalize_persisted_interactive_sessions

    def open_registry(self, *args, **kwargs):
        order.append("identity-registry-open")
        opened(self, *args, **kwargs)

    def normalize_sessions(self):
        order.append("interactive-session-normalize")
        normalize(self)

    monkeypatch.setattr(AgentRegistry, "__init__", open_registry)
    monkeypatch.setattr(
        _AgentsRegistryMixin,
        "_normalize_persisted_interactive_sessions",
        normalize_sessions,
    )
    app = _application(tmp_path, request)
    app._startup_prepare_state()

    assert order.index("identity-registry-open") < order.index(
        "interactive-session-normalize"
    )
    (session,) = app.desired_state.load().interactive_sessions
    assert (session.actor, session.session_ref) == (id_uri, "pre-flip-session")


@pytest.mark.parametrize("fetched", [False, True])
def test_session_fetch_does_not_drain_the_name_uri_row(
    flipped, monkeypatch, fetched: bool
) -> None:
    app, id_uri = flipped
    monkeypatch.setattr(
        app, "_fence_live_principal_session", lambda actor, _params: actor
    )
    session = {"actor": "worker", "sessionRef": "session-1"}
    fetch = {**session, **({"fetched": True} if fetched else {})}

    assert app.handle("message.pending.list", fetch)["messages"] == []
    assert app._message_consumer_keys(session, actor=id_uri) == (id_uri,)
    assert app._inbox.pending_messages(NAME_URI)


def _history(app, id_uri: str, **page) -> dict:
    return app.handle(
        "message.history",
        {
            "from": f"user:{OWNER}",
            "onBehalfOf": id_uri,
            "conversationId": "conversation-pre-flip",
            **page,
        },
    )


def _entry_ids(page: dict) -> list[str]:
    return [entry["messageId"] for entry in page["entries"]]


def test_history_reads_only_the_agents_own_address(flipped) -> None:
    app, id_uri = flipped
    post_flip = InboxMessage(
        message_id="post-flip",
        conversation_id="conversation-pre-flip",
        sender="channel:lark:test",
        recipient=id_uri,
        payload=json.dumps({"message": "queued after the flip"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=2,
    )
    assert app._inbox.receive(post_flip).accepted

    assert _entry_ids(_history(app, id_uri)) == ["post-flip"]


def test_reply_reminders_skip_a_request_queued_at_the_name_uri(flipped) -> None:
    from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
        InboxReplyReminders,
    )

    app, id_uri = flipped
    window = {"fetched_since_ms": None, "fetched_until_ms": 10, "since_inclusive": True}
    reminders = InboxReplyReminders(
        app._inbox, node_id=NODE, clock_ms=lambda: 10,
        read_keys=app._consumer_read_keys,
    )

    assert reminders.unreplied_requests(id_uri, delivery_ids={"pre-flip"}, **window) == ()


def test_reply_reminders_list_requests_in_one_arrival_order_across_read_keys(
    flipped,
) -> None:
    from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
        InboxReplyReminders,
    )

    app, id_uri = flipped
    assert app._inbox.receive(_pre_flip_request("post-flip-1", id_uri, 2)).accepted
    assert app._inbox.receive(_pre_flip_request("post-flip-2", id_uri, 3)).accepted
    reminders = InboxReplyReminders(
        app._inbox, node_id=NODE, clock_ms=lambda: 10,
        read_keys=lambda address: (address, NAME_URI) if address == id_uri else (address,),
    )

    assert reminders.unreplied_requests(
        id_uri,
        delivery_ids={"post-flip-2", "pre-flip", "post-flip-1"},
        fetched_since_ms=None,
        fetched_until_ms=10,
        since_inclusive=True,
    ) == ("pre-flip", "post-flip-1", "post-flip-2")


def _pre_flip_request(message_id: str, recipient: str, created_at_ms: int) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"conversation-{message_id}",
        sender=f"agent:{OWNER}:{NODE}:requester",
        recipient=recipient,
        payload=json.dumps({"message": "please do the thing"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )


HEADLESS_ID_URI = f"agent:{OWNER}:{NODE}:a.bl5ksbh3pghtmvnfo5peihbyke"


@pytest.fixture
def headless(tmp_path: Path, *, actor_host):
    """A real inbox and runtime pulling for the id-minted headless ``worker``."""

    from hyprial.daemon.impl import DaemonEventBridge
    from hyprial.daemon.impl.desired_state import DesiredStateStore
    from hyprial.kernel import HarnessLaunchSpec
    from tests.identity.providers.lifecycle.availability.support import (
        FakeHarnessFarm,
        _NullTransport,
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    inbox = InboxService(
        state_dir / "inbox.sqlite3", MemoryDeliveryTransport(), node_id=NODE,
    )
    desired = DesiredStateStore(state_dir / "desired-state.json")
    desired.upsert_harness(HarnessLaunchSpec(harness="pi", name="worker", headless=True))
    deliveries: list[object] = []

    class _Harnesses(FakeHarnessFarm):
        def dispatch(self, envelope):
            accepted = super().dispatch(envelope)
            if accepted:
                deliveries.append(envelope)
            return accepted

    harnesses = _Harnesses(actors=("worker",))
    runtime = DaemonEventBridge(
        state_dir=state_dir, node_id=NODE, desired_state=desired,
        transport=_NullTransport(), inbox=inbox, harnesses=harnesses,
        harness_actor_uri=lambda _name: HEADLESS_ID_URI,
        harness_read_keys=lambda _name: (HEADLESS_ID_URI, NAME_URI),
        recipient_read_keys=lambda address: (
            (HEADLESS_ID_URI, NAME_URI) if address == HEADLESS_ID_URI else (address,)
        ),
        official_actor_uri=lambda address: (
            HEADLESS_ID_URI if address == NAME_URI else address
        ),
        clock_ms=lambda: 1_000, actor_host=actor_host,
        cell_delivery=AdmittingCellDelivery(),
    )
    try:
        runtime.start()
        yield inbox, harnesses, runtime, deliveries
    finally:
        runtime.stop()
        inbox.close()


def test_headless_runtime_batches_a_row_queued_at_the_id_uri(headless) -> None:
    inbox, _harnesses, runtime, deliveries = headless
    assert inbox.receive(_pre_flip_request("post-flip", HEADLESS_ID_URI, 2)).accepted

    assert runtime.on_timer().harness_deliveries == 1
    (delivery,) = deliveries
    assert delivery.message_ids == ("post-flip",)


def test_headless_runtime_claims_rows_under_every_consumer_read_key(headless) -> None:
    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus

    inbox, harnesses, runtime, deliveries = headless
    assert inbox.receive(_pre_flip_request("pre-flip", NAME_URI, 1)).accepted
    assert inbox.receive(_pre_flip_request("post-flip", HEADLESS_ID_URI, 2)).accepted

    assert runtime.on_timer().harness_deliveries == 1
    (delivery,) = deliveries
    assert delivery.message_ids == ("pre-flip", "post-flip")
    assert runtime.on_timer().harness_deliveries == 0
    assert runtime._batch_claims == {"worker": ("pre-flip", "post-flip")}  # noqa: SLF001

    harnesses.results.append(
        HarnessResult(
            delivery.delivery_id, delivery.recipient, HarnessResultStatus.COMPLETED,
            message_ids=delivery.message_ids,
        )
    )
    runtime.on_timer()
    assert runtime.on_timer().harness_deliveries == 0
    assert len(deliveries) == 1
    assert inbox.ack(NAME_URI, "pre-flip").acknowledged
    assert inbox.ack(HEADLESS_ID_URI, "post-flip").acknowledged
    assert not inbox.pending_messages(NAME_URI)


def test_started_runtime_bridge_reads_only_the_official_address(
    tmp_path: Path, monkeypatch, id_form
) -> None:
    from hyprial.daemon.impl.application.wiring import transport as wiring
    from tests.support.runtime.cell_delivery.composition_probe import _probe
    from tests.daemon.progress.test_progress_end_to_end import _CompositionRuntime

    app, _captured, _links = _probe(monkeypatch, tmp_path)
    wired: dict[str, object] = {}

    class _Bridge(_CompositionRuntime):
        def __init__(self, **kwargs: object) -> None:
            wired.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(wiring, "DaemonEventBridge", _Bridge)
    try:
        agent = app.agents.create("worker")
        app._start_runtime()
        read_keys = wired["harness_read_keys"]
        assert callable(read_keys)
        assert read_keys("worker") == (agent.uri,)
        assert read_keys("squire") == (f"agent:{app.owner}:{app.node_id}:squire",)
        recipient_keys = wired["recipient_read_keys"]
        assert callable(recipient_keys)
        assert recipient_keys(agent.uri) == (agent.uri,)
        name_uri = f"agent:{app.owner}:{app.node_id}:worker"
        assert recipient_keys(name_uri) == (name_uri,)
    finally:
        app._close()
        app.agents.close()


def test_started_runtime_bridge_maps_a_live_name_uri_to_the_official_address(
    tmp_path: Path, monkeypatch, id_form
) -> None:
    from hyprial.daemon.impl.application.wiring import transport as wiring
    from tests.support.runtime.cell_delivery.composition_probe import _probe
    from tests.daemon.progress.test_progress_end_to_end import _CompositionRuntime

    app, _captured, _links = _probe(monkeypatch, tmp_path)
    wired: dict[str, object] = {}

    class _Bridge(_CompositionRuntime):
        def __init__(self, **kwargs: object) -> None:
            wired.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(wiring, "DaemonEventBridge", _Bridge)
    try:
        agent = app.agents.create("worker")
        app._start_runtime()
        official = wired["official_actor_uri"]
        assert callable(official)
        assert official(f"agent:{app.owner}:{app.node_id}:worker") == agent.uri
        assert official(agent.uri) == agent.uri
        squire = f"agent:{app.owner}:{app.node_id}:squire"
        assert official(squire) == squire
    finally:
        app._close()
        app.agents.close()


def _queued_message(message_id: str, recipient: str, created_at_ms: int) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"conversation-{message_id}",
        sender=f"agent:{OWNER}:{NODE}:requester",
        recipient=recipient,
        payload=json.dumps({"message": "for your information"}).encode(),
        intent="message",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [(NAME_URI, HEADLESS_ID_URI), (HEADLESS_ID_URI, NAME_URI)],
    ids=["name-first", "id-first"],
)
def test_headless_runtime_settles_each_batch_member_under_its_own_key(
    headless, first: str, second: str
) -> None:
    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus

    inbox, harnesses, runtime, deliveries = headless
    assert inbox.receive(_queued_message("m1", first, 1)).accepted
    assert inbox.receive(_queued_message("m2", second, 2)).accepted

    assert runtime.on_timer().harness_deliveries == 1
    (delivery,) = deliveries
    assert delivery.message_ids == ("m1", "m2")
    assert delivery.recipient == first
    harnesses.results.append(
        HarnessResult(
            delivery.delivery_id, delivery.recipient, HarnessResultStatus.COMPLETED,
            message_ids=delivery.message_ids,
        )
    )
    runtime.on_timer()

    assert inbox.pending_messages(NAME_URI) == ()
    assert inbox.pending_messages(HEADLESS_ID_URI) == ()
    for _tick in range(3):
        assert runtime.on_timer().harness_deliveries == 0
    assert len(deliveries) == 1
    assert runtime._batch_claims == {}  # noqa: SLF001


def test_claimed_batch_result_settles_each_member_under_its_own_key(headless) -> None:
    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus

    inbox, _harnesses, runtime, _deliveries = headless
    assert inbox.receive(_queued_message("m1", NAME_URI, 1)).accepted
    assert inbox.receive(_queued_message("m2", HEADLESS_ID_URI, 2)).accepted
    result = HarnessResult(
        "m1", NAME_URI, HarnessResultStatus.COMPLETED, message_ids=("m1", "m2"),
    )

    assert runtime._settle_claimed_result_business(result) is True  # noqa: SLF001
    assert inbox.pending_messages(NAME_URI) == ()
    assert inbox.pending_messages(HEADLESS_ID_URI) == ()


def _id_first_batch_result() -> object:
    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus

    return HarnessResult(
        "m1", HEADLESS_ID_URI, HarnessResultStatus.COMPLETED, message_ids=("m1", "m2"),
    )


@pytest.mark.parametrize("path", ["claimed", "drained", "dispatched"])
def test_id_first_batch_settles_the_name_uri_member_without_a_running_worker(
    headless, path: str
) -> None:
    inbox, harnesses, runtime, deliveries = headless
    assert inbox.receive(_queued_message("m1", HEADLESS_ID_URI, 1)).accepted
    assert inbox.receive(_queued_message("m2", NAME_URI, 2)).accepted
    if path == "dispatched":
        assert runtime.on_timer().harness_deliveries == 1
        assert deliveries[0].message_ids == ("m1", "m2")
    harnesses.actors = ()

    if path == "claimed":
        assert runtime._settle_claimed_result_business(_id_first_batch_result()) is True  # noqa: SLF001
    else:
        harnesses.results.append(_id_first_batch_result())
        runtime._complete_harness_results()  # noqa: SLF001

    assert inbox.pending_messages(NAME_URI) == ()
    assert inbox.pending_messages(HEADLESS_ID_URI) == ()
    assert runtime._pending_batch_members == {}  # noqa: SLF001


def test_claimed_batch_retry_rereads_each_member_home(headless) -> None:
    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus

    inbox, harnesses, runtime, _deliveries = headless
    assert inbox.receive(_queued_message("m2", NAME_URI, 2)).accepted
    harnesses.actors = ()
    runtime._pending_batch_members["m1"] = {  # noqa: SLF001
        "m2": HarnessResult("m2", HEADLESS_ID_URI, HarnessResultStatus.COMPLETED),
    }

    assert runtime._settle_claimed_result_business(_id_first_batch_result()) is True  # noqa: SLF001
    assert inbox.pending_messages(NAME_URI) == ()


def test_drained_batch_result_survives_an_inbox_read_failure(headless, monkeypatch) -> None:
    from hyprial.daemon.impl.inbox import InboxAuthorityTimeout

    inbox, harnesses, runtime, _deliveries = headless
    assert inbox.receive(_queued_message("m1", HEADLESS_ID_URI, 1)).accepted
    assert inbox.receive(_queued_message("m2", NAME_URI, 2)).accepted
    harnesses.results.append(_id_first_batch_result())
    read = inbox.pending_messages

    def unavailable(key: str):
        raise InboxAuthorityTimeout("inbox actor busy")

    monkeypatch.setattr(inbox, "pending_messages", unavailable)
    runtime._complete_harness_results()  # noqa: SLF001
    monkeypatch.setattr(inbox, "pending_messages", read)
    assert harnesses.results == []
    assert read(NAME_URI) and read(HEADLESS_ID_URI)

    runtime._complete_harness_results()  # noqa: SLF001
    assert read(NAME_URI) == ()
    assert read(HEADLESS_ID_URI) == ()


@pytest.mark.parametrize("mechanism", ["turn-hook", "python_worker"])
def test_mechanism_answer_to_a_pre_flip_row_speaks_from_the_official_address(
    headless, monkeypatch, mechanism: str
) -> None:
    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus

    inbox, _harnesses, runtime, _deliveries = headless
    original = _pre_flip_request("pre-flip", NAME_URI, 1)
    assert inbox.receive(original).accepted
    submitted: list[InboxMessage] = []
    submit = inbox.submit

    def recording_submit(message: InboxMessage):
        submitted.append(message)
        return submit(message)

    monkeypatch.setattr(inbox, "submit", recording_submit)
    result = HarnessResult(
        "pre-flip", NAME_URI, HarnessResultStatus.COMPLETED, output="answered"
    )
    answer = (
        runtime._answer_hook_request  # noqa: SLF001
        if mechanism == "turn-hook"
        else runtime._answer_mechanism_result  # noqa: SLF001
    )

    assert answer(original, result) is True

    assert inbox.pending_messages(NAME_URI) == ()
    (reply,) = submitted
    assert reply.sender == HEADLESS_ID_URI
    assert reply.recipient == original.sender
    body = json.loads(reply.payload)
    assert body["replyTo"] == "pre-flip"
    assert body["origin"] == {
        "via": "daemon", "mechanism": mechanism, "onBehalfOf": HEADLESS_ID_URI,
    }
