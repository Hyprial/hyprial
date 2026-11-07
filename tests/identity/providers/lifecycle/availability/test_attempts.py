"""provider_availability_contract: attempts scenarios."""

from __future__ import annotations
import json
import os
from dataclasses import replace
from pathlib import Path
import pytest
from hyprial.daemon import AttemptIdentity, unavailable_notice
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl import DaemonEventBridge
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent

from .support import (
    FakeHarnessFarm,
    LoudInbox,
    OWNER,
    PROXY,
    REQUESTER,
    WORKER,
    _NullTransport,
    _notices_to,
    _proxy_runtime,
    _reply,
    _request,
    _runtime,
    _uri,
)


def test_pc2_failed_attempt_reaches_the_sender_with_this_attempts_evidence(
    tmp_path: Path,
) -> None:
    """The main case: quota-class failure, owner distinct from sender.

    Mutation contract (main): removing the ``self._loud_harness_failure(...)``
    call leaves the owner/quota observer path green and makes exactly this
    test red on the sender-side assertion.
    """

    inbox = LoudInbox()
    inbox.add(_request("req-1"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()  # dispatch: the attempt clock starts here
        assert farm.deliveries == [("squire", "req-1")]

        now["v"] += 1_500
        farm.results.append(
            HarnessResult(
                delivery_id="req-1",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="quota exhausted",
                failure_code="PROVIDER_USAGE_LIMIT",
            )
        )
        runtime.on_timer()
    finally:
        runtime.stop()

    notices = _notices_to(inbox, REQUESTER)
    assert len(notices) == 1, "the waiting sender was not told about the failure"
    body = notices[0]
    envelope = next(
        message
        for message in inbox.sent
        if message.recipient == REQUESTER and b"provider-unavailable" in message.payload
    )
    assert body["notification"] == "provider-unavailable"
    assert body["failureCode"] == "PROVIDER_USAGE_LIMIT"
    assert body["deliveryId"] == "req-1"
    assert body["attemptId"] == "req-1#1"
    assert body["worker"] == WORKER
    assert body["harness"] == "pi"
    assert body["nonce"], "notice carries no value unique to this attempt"
    assert "PROVIDER_USAGE_LIMIT" in body["message"]
    assert "req-1" in body["message"]
    # The owner is a different actor and the new channel is addressed to the
    # sender: the notice must never be routed owner-ward (or vice versa).
    assert body["requester"] == REQUESTER
    assert envelope.sender == WORKER
    assert REQUESTER != OWNER
    assert all(message.recipient != OWNER for message in inbox.sent)
    # It is a report, not the business result: the failure is durably settled
    # and no message carrying model output was fabricated for the sender.
    assert inbox.settlements["req-1"].terminal is True
    assert all(b"notification" in message.payload for message in inbox.sent)


def test_pc2_rate_limited_failure_is_one_terminal_notice_not_one_per_attempt(
    tmp_path: Path,
) -> None:
    """§8.2: a 429 settles the delivery once and tells the sender once.

    The failure text carries no explicit ``failure_code``, so the daemon must
    classify it: a rate limit is permanent for the current message, terminal
    on the first attempt, and distinct from the quota class that blocks the
    agent.  A transient classification would keep the delivery visible for
    redelivery and would make the same request reach the provider again.
    """

    inbox = LoudInbox()
    inbox.add(_request("req-429"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()
        assert farm.deliveries == [("squire", "req-429")]

        now["v"] += 1_500
        farm.results.append(
            HarnessResult(
                delivery_id="req-429",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error='429: {"type":"rate_limit_error","message":"slow down"}',
            )
        )
        runtime.on_timer()
        # Long past any backoff: a retryable classification dispatches again.
        now["v"] += 30 * 60 * 1000
        runtime.on_timer()
    finally:
        runtime.stop()

    settlement = inbox.settlements["req-429"]
    assert settlement.failure_code == "PROVIDER_RATE_LIMITED"
    assert settlement.terminal is True
    assert settlement.attempts == 1

    notices = _notices_to(inbox, REQUESTER)
    assert len(notices) == 1, notices
    assert notices[0]["failureCode"] == "PROVIDER_RATE_LIMITED"
    assert notices[0]["attemptId"] == "req-429#1"
    assert farm.deliveries == [("squire", "req-429")]


def test_pc2_second_attempt_has_its_own_notice_and_key(tmp_path: Path) -> None:
    """One delivery id can host two attempts; the second needs its own loud.

    Migration-relevant: the plan's negative control was a 710s second attempt
    on the same delivery id; an once-key that only held the delivery id would
    swallow it.
    """

    inbox = LoudInbox()
    inbox.add(_request("req-2"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()
        farm.results.append(
            HarnessResult(
                delivery_id="req-2",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="connection reset",
                failure_code="PROVIDER_CONNECTION",
            )
        )
        runtime.on_timer()
        # Retry: the same delivery is handed over again as a new attempt.
        farm.enqueued.discard("req-2")
        farm.results.append(
            HarnessResult(
                delivery_id="req-2",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="connection reset again",
                failure_code="PROVIDER_CONNECTION",
            )
        )
        runtime.on_timer()
    finally:
        runtime.stop()

    notices = _notices_to(inbox, REQUESTER)
    assert [n["attemptId"] for n in notices] == ["req-2#1", "req-2#2"]
    assert notices[0]["nonce"] != notices[1]["nonce"]


def test_pc2_notice_is_idempotent_per_attempt(tmp_path: Path) -> None:
    """A replay of the same failure is the same notice, not a second one."""

    inbox = LoudInbox()
    original = _request("req-3")
    identity = AttemptIdentity(
        delivery_id="req-3",
        conversation_id="conv-req-3",
        sender=REQUESTER,
        worker=WORKER,
        harness="pi",
        generation=1,
        observed_at_ms=2_000,
    )
    first = unavailable_notice(
        identity, failure_code="PROVIDER_USAGE_LIMIT", terminal=True, attempts=1,
        max_attempts=3,
    )
    second = unavailable_notice(
        identity, failure_code="PROVIDER_USAGE_LIMIT", terminal=True, attempts=1,
        max_attempts=3,
    )
    assert first.message_id == second.message_id
    assert first.idempotency_key == second.idempotency_key
    inbox.submit(first)
    inbox.submit(second)
    assert len(inbox.sent) == 1
    assert original.message_id != first.message_id


def test_pc2_notice_survives_a_fetched_or_settled_original(tmp_path: Path) -> None:
    """A row consumed before the failure lands still gets a report.

    ``pending_messages`` excludes fetched/terminal rows; the settlement
    tombstone is the authoritative fallback, and the failure must not become
    silent just because the obvious lookup missed.
    """

    inbox = LoudInbox()
    inbox.add(_request("req-4"))
    inbox.consumed.add("req-4")
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        farm.results.append(
            HarnessResult(
                delivery_id="req-4",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="socket closed",
                failure_code="PROVIDER_CONNECTION",
            )
        )
        runtime.on_timer()
    finally:
        runtime.stop()

    notices = _notices_to(inbox, REQUESTER)
    assert len(notices) == 1
    assert notices[0]["deliveryId"] == "req-4"


def test_pc2_refused_submission_is_retried_and_never_claimed_delivered(
    tmp_path: Path,
) -> None:
    """A notice the inbox refuses is held, retried, and never reported sent."""

    inbox = LoudInbox()
    inbox.add(_request("req-5"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    logged: list[tuple[str, str]] = []
    runtime = _runtime(tmp_path, inbox, farm, now)
    runtime._logger = lambda level, component, event, **fields: logged.append((level, event))
    try:
        inbox.refuse_submit = True
        runtime.on_timer()
        farm.results.append(
            HarnessResult(
                delivery_id="req-5",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="quota exhausted",
                failure_code="PROVIDER_USAGE_LIMIT",
            )
        )
        summary = runtime.on_timer()
        assert _notices_to(inbox, REQUESTER) == []
        assert ("error", "availability_loud.delivery_failed") in logged
        assert summary.availability_notices == 0

        inbox.refuse_submit = False
        runtime.on_timer()
        assert len(_notices_to(inbox, REQUESTER)) == 1
        assert ("warn", "availability_loud.sent") in logged
    finally:
        runtime.stop()


def test_pc2_no_event_stall_is_reported_and_progress_resets_the_clock(
    tmp_path: Path,
) -> None:
    """The stuck class has no event, so the clock must be the trigger.

    Mutation contract: deleting the ``_report_stalled_deliveries`` phase (or
    its deadline comparison) makes this red; making process liveness reset the
    clock instead of correlated progress also makes it red.
    """

    inbox = LoudInbox()
    inbox.add(_request("req-6", created_at_ms=1_000))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()  # dispatch: clock starts now

        # pi's budget is 480s; before it elapses nothing may be reported.
        now["v"] += 400_000
        assert runtime.on_timer().availability_notices == 0
        assert _notices_to(inbox, REQUESTER) == []

        # Correlated progress (the same signal that slides the dispatch hold)
        # resets the clock -- liveness alone would not.
        farm.progress.append(
            ProgressEvent(
                delivery_id="req-6",
                conversation_id="conv-req-6",
                actor=WORKER,
                harness="pi",
                phase="tool-call",
                seq=1,
                emitted_at_ms=now["v"],
                summary="running the tool",
            )
        )
        runtime.on_timer()
        now["v"] += 300_000
        assert runtime.on_timer().availability_notices == 0

        # Now go past the budget with no further progress: report once.
        now["v"] += 200_000
        summary = runtime.on_timer()
        assert summary.availability_notices == 1
        notices = _notices_to(inbox, REQUESTER)
        assert len(notices) == 1
        assert notices[0]["notification"] == "provider-no-progress"
        assert notices[0]["budgetMs"] == 480_000
        assert notices[0]["attemptId"] == "req-6#1"
        assert "原因待确认" in notices[0]["message"]
        # The turn is not killed and no second report is emitted.
        assert farm.interrupts == []
        assert runtime.on_timer().availability_notices == 0
        assert len(_notices_to(inbox, REQUESTER)) == 1
    finally:
        runtime.stop()


def test_pc2_reply_dispatches_without_arming_a_silence_attempt(tmp_path: Path) -> None:
    """A reply is delivered, but only the unanswered request can alarm."""

    inbox = LoudInbox()
    inbox.add(_reply("reply-complete"))
    inbox.add(_request("req-silent"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()
        assert farm.deliveries == [
            ("squire", "reply-complete"),
            ("squire", "req-silent"),
        ]

        now["v"] += 480_001
        summary = runtime.on_timer()

        assert summary.availability_notices == 1
        notices = _notices_to(inbox, REQUESTER)
        assert len(notices) == 1
        assert notices[0]["notification"] == "provider-no-progress"
        assert notices[0]["deliveryId"] == "req-silent"
        assert runtime.on_timer().availability_notices == 0
    finally:
        runtime.stop()


def test_pc2_acknowledged_request_settles_silence_attempt(tmp_path: Path) -> None:
    """An answered long-lived turn must not leave a later silence alarm."""

    inbox = LoudInbox()
    inbox.add(_request("req-answered"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()
        assert set(runtime._inflight) == {"req-answered"}

        # Native reply settles by acknowledging the inbound delivery while
        # the long-lived worker may continue running unrelated work.
        assert inbox.ack(WORKER, "req-answered").acknowledged
        now["v"] += 500_000

        assert runtime.on_timer().availability_notices == 0
        assert _notices_to(inbox, REQUESTER) == []
        assert runtime._inflight == {}
    finally:
        runtime.stop()


def test_pc2_a_completed_slow_turn_is_not_reported_or_killed(tmp_path: Path) -> None:
    """Healthy slow work must not be mislabelled or interrupted."""

    inbox = LoudInbox()
    inbox.add(_request("req-7"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()
        now["v"] += 480_000
        farm.results.append(
            HarnessResult(
                delivery_id="req-7",
                recipient=WORKER,
                status=HarnessResultStatus.COMPLETED,
                output="done",
            )
        )
        runtime.on_timer()
        now["v"] += 480_000
        runtime.on_timer()
    finally:
        runtime.stop()

    assert _notices_to(inbox, REQUESTER) == []
    assert farm.interrupts == []


@pytest.mark.parametrize(
    "status",
    [HarnessResultStatus.FAILED, HarnessResultStatus.INTERRUPTED],
)
def test_pc2_a_terminal_turn_is_not_reported_when_workflow_settlement_defers(
    tmp_path: Path,
    status: HarnessResultStatus,
) -> None:
    """A closed PAC node must not leave its ended turn's silence clock alive."""

    inbox = LoudInbox()
    inbox.add(_request("req-ended"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}

    def defer_workflow_outcome(result: HarnessResult) -> bool:
        raise RuntimeError("workflow node already closed")

    runtime = _runtime(
        tmp_path,
        inbox,
        farm,
        now,
        workflow_outcome=defer_workflow_outcome,
    )
    try:
        runtime.on_timer()
        farm.results.append(
            HarnessResult(
                delivery_id="req-ended",
                recipient=WORKER,
                status=status,
                error="turn ended without an inbox acknowledgement",
            )
        )
        runtime.on_timer()
        assert not inbox.is_acknowledged("req-ended")

        now["v"] += 500_000
        assert runtime.on_timer().availability_notices == 0
        assert _notices_to(inbox, REQUESTER) == []
    finally:
        runtime.stop()


def test_pc2_a_stopped_worker_finishes_its_silence_attempt(tmp_path: Path) -> None:
    inbox = LoudInbox()
    inbox.add(_request("req-stopped"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()
        assert set(runtime._inflight) == {"req-stopped"}

        farm.actors = ()
        now["v"] += 500_000
        assert runtime.on_timer().availability_notices == 0
        assert runtime._inflight == {}
        assert _notices_to(inbox, REQUESTER) == []
    finally:
        runtime.stop()


def test_pc2_a_started_retry_gets_a_fresh_silence_notice(tmp_path: Path) -> None:
    inbox = LoudInbox()
    inbox.add(_request("req-retry"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime = _runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()
        farm.results.append(
            HarnessResult(
                delivery_id="req-retry",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="connection reset",
                failure_code="PROVIDER_CONNECTION",
            )
        )
        runtime.on_timer()
        assert runtime._inflight == {}

        farm.enqueued.discard("req-retry")
        runtime.on_timer()
        assert runtime._inflight["req-retry"].identity.generation == 2

        now["v"] += 500_000
        assert runtime.on_timer().availability_notices == 1
        notices = [
            body
            for body in _notices_to(inbox, REQUESTER)
            if body["notification"] == "provider-no-progress"
        ]
        assert [body["attemptId"] for body in notices] == ["req-retry#2"]
        assert runtime.on_timer().availability_notices == 0
    finally:
        runtime.stop()


def test_pc2_owed_notice_survives_a_restart_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    """A notice that never got submitted before a crash is still owed.

    The crashed run's only trace of the notice is in memory, which dies with
    it; the terminal settlement row does not.  A restart must re-derive the
    exact same deterministic notice from that row -- otherwise this unit has
    replaced one silence with another (the review's one blocking item).
    """

    from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    inbox = InboxService(
        state_dir / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-reg"
    )
    desired = DesiredStateStore(state_dir / "desired-state.json")
    desired.upsert_harness(
        HarnessLaunchSpec(harness="pi", name="squire", headless=True)
    )
    # The crashed run: marker on disk, and the failure it settled durably.
    marker = state_dir / "daemon-run.json"
    marker.write_text('{"schemaVersion":1,"pid":4242}', encoding="utf-8")
    os.utime(marker, (0.0, 0.0))
    now = {"v": 5_000}
    assert inbox.receive(_request("owed-1", created_at_ms=1_000)).accepted
    inbox.settle_harness_failure(
        WORKER,
        "owed-1",
        "PROVIDER_USAGE_LIMIT",
        permanent=True,
        max_attempts=3,
        backoff_ms=(1_000,),
        now_ms=now["v"],
    )

    farm = FakeHarnessFarm()

    def start_and_collect():  # type: ignore[no-untyped-def]
        runtime = DaemonEventBridge(
            state_dir=state_dir,
            node_id="node-reg",
            desired_state=desired,
            transport=_NullTransport(),
            inbox=inbox,
            harnesses=farm,
            harness_actor_uri=_uri,
            clock_ms=lambda: now["v"],
        )
        runtime.start()
        try:
            return [
                item.message
                for item in inbox.outbox_items()
                if b'"notification"' in item.message.payload
            ]
        finally:
            runtime.stop()

    try:
        recovered = start_and_collect()
        assert len(recovered) == 1, "the restart did not re-derive the owed notice"
        body = json.loads(recovered[0].payload)
        assert body["deliveryId"] == "owed-1"
        assert body["notification"] == "provider-unavailable"
        assert body["failureCode"] == "PROVIDER_USAGE_LIMIT"
        assert body["attemptId"] == "owed-1#1"
        assert body["nonce"]
        # The id is the same one the live path would have built: a pure
        # function of durable facts, so a replay cannot become a second notice.
        live = unavailable_notice(
            AttemptIdentity(
                delivery_id="owed-1",
                conversation_id="conv-owed-1",
                sender=REQUESTER,
                worker=WORKER,
                harness=None,
                generation=1,
                observed_at_ms=0,
            ),
            failure_code="PROVIDER_USAGE_LIMIT",
            terminal=True,
            attempts=1,
            max_attempts=3,
        )
        assert recovered[0].message_id == live.message_id

        # A second unclean restart sweeps again and must not duplicate.
        marker.write_text('{"schemaVersion":1,"pid":4243}', encoding="utf-8")
        os.utime(marker, (0.0, 0.0))
        again = start_and_collect()
        assert len(again) == 1
    finally:
        inbox.close()


def test_pc2_undelivered_notice_is_visible_when_the_daemon_shuts_down(
    tmp_path: Path,
) -> None:
    """On a clean stop there is no restart to recover from -- say it out loud."""

    inbox = LoudInbox()
    inbox.add(_request("req-8"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    logged: list[tuple[str, str, dict]] = []
    runtime = _runtime(tmp_path, inbox, farm, now)
    runtime._logger = lambda level, component, event, **fields: logged.append(
        (level, event, fields)
    )
    try:
        inbox.refuse_submit = True
        runtime.on_timer()
        farm.results.append(
            HarnessResult(
                delivery_id="req-8",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="quota exhausted",
                failure_code="PROVIDER_USAGE_LIMIT",
            )
        )
        runtime.on_timer()
        assert _notices_to(inbox, REQUESTER) == []
        runtime.stop()
    finally:
        if runtime._started:
            runtime.stop()

    abandoned = [
        fields for level, event, fields in logged
        if event == "availability_loud.delivery_abandoned"
    ]
    assert abandoned, "a notice lost at shutdown must leave a record"
    assert abandoned[0]["recipient"] == REQUESTER
    assert "req-8" in str(abandoned[0]["idempotencyKey"])


# --------------------------------------------------------------------------- #
# T1 steer: a queued delivery is folded into the running turn before the
# in-channel notice.  Only a failed steer raises the notice.
# --------------------------------------------------------------------------- #


def test_t1_steers_a_queued_delivery_and_notices_after_the_second_window(
    tmp_path: Path,
) -> None:
    inbox = LoudInbox()
    inbox.add(_request("req-steer-1"))
    farm = FakeHarnessFarm()
    farm.steer_returns = True
    now = {"v": 1_000}
    logged: list[tuple[str, str, dict]] = []
    runtime = _runtime(tmp_path, inbox, farm, now)
    runtime._logger = lambda level, component, event, **fields: logged.append(
        (level, event, fields)
    )
    try:
        runtime.on_timer()  # first offer starts the T1 clock
        assert farm.deliveries == [("squire", "req-steer-1")]

        now["v"] += 480_000  # pi T1 budget
        runtime.on_timer()
        assert farm.steers == [("squire", "req-steer-1")]
        # A successful steer is not a notice: the queued text is now in the
        # running turn, so the sender must not be alarmed.
        assert _notices_to(inbox, REQUESTER) == []

        now["v"] += 480_000  # second silence: steer once, then notice
        runtime.on_timer()
    finally:
        runtime.stop()

    notices = _notices_to(inbox, REQUESTER)
    assert [notice["notification"] for notice in notices] == [
        "provider-no-progress"
    ]
    assert len(farm.steers) == 1, "steer must not repeat per tick"
    assert [e for _l, e, _f in logged].count(
        "availability_loud.steer_accepted"
    ) == 1


def test_t1_steer_failure_raises_the_notice_and_is_recorded(
    tmp_path: Path,
) -> None:
    inbox = LoudInbox()
    inbox.add(_request("req-steer-2"))
    farm = FakeHarnessFarm()
    farm.steer_returns = False
    now = {"v": 1_000}
    logged: list[tuple[str, str, dict]] = []
    runtime = _runtime(tmp_path, inbox, farm, now)
    runtime._logger = lambda level, component, event, **fields: logged.append(
        (level, event, fields)
    )
    try:
        runtime.on_timer()
        now["v"] += 480_000
        runtime.on_timer()
    finally:
        runtime.stop()

    assert farm.steers == [("squire", "req-steer-2")]
    assert [notice["notification"] for notice in _notices_to(inbox, REQUESTER)] == [
        "provider-no-progress"
    ]
    failed = [f for _l, e, f in logged if e == "availability_loud.steer_failed"]
    assert failed and failed[0]["deliveryId"] == "req-steer-2"


def test_t1_never_steers_a_user_proxy_relay(tmp_path: Path) -> None:
    """A user-proxy turn is a person's chat leg; the daemon may not fold input
    into it.  The delivery still gets its fail-loud notice."""

    inbox = LoudInbox()
    inbox.add(replace(_request("req-proxy-steer"), recipient=PROXY))
    farm = FakeHarnessFarm(actors=("proxy",))
    now = {"v": 1_000}
    runtime, events = _proxy_runtime(tmp_path, inbox, farm, now)
    try:
        runtime.on_timer()
        assert farm.deliveries == [("proxy", "req-proxy-steer")]
        now["v"] += 2_520_000  # user-proxy has no measured budget: the maximum
        runtime.on_timer()
    finally:
        runtime.stop()

    assert farm.steers == [], "a user-proxy relay must never be steered"
    assert [notice["notification"] for notice in _notices_to(inbox, REQUESTER)] == [
        "provider-no-progress"
    ]
    assert not [e for e, _f in events if e == "availability_loud.steer_accepted"]


def test_t1_budget_is_configurable_with_one_env_override(monkeypatch) -> None:
    from hyprial.daemon.impl.correlation.availability_loud import (
        T1_BUDGET_ENV,
        no_progress_budget_seconds,
    )

    monkeypatch.delenv(T1_BUDGET_ENV, raising=False)
    assert no_progress_budget_seconds("pi") == 480
    assert no_progress_budget_seconds("claude") == 1620
    monkeypatch.setenv(T1_BUDGET_ENV, "60")
    assert no_progress_budget_seconds("pi") == 60
    assert no_progress_budget_seconds("claude") == 60
    monkeypatch.setenv(T1_BUDGET_ENV, "0")
    with pytest.raises(ValueError):
        no_progress_budget_seconds("pi")
    monkeypatch.setenv(T1_BUDGET_ENV, "nonsense")
    with pytest.raises(ValueError):
        no_progress_budget_seconds("pi")
