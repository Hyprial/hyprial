"""provider_availability_contract: receiver drill scenarios."""

from __future__ import annotations
import json
from pathlib import Path
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl import DaemonEventBridge
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus

from .support import (
    FakeHarnessFarm,
    OWNER,
    REQUESTER,
    WORKER,
    _NullTransport,
    _request,
    _uri,
)


def test_pc2_isolated_receiver_drill_five_notices_no_online_send(tmp_path: Path) -> None:
    """The plan's bounded drill: 5 notices, real durable inbox, no online send.

    Unlike the fake-inbox tests above, this drives the *real* ``InboxService``
    (sqlite on tmp_path) with the offline memory transport and an isolated
    local sender, so each notice is a durable row a receiver can read --
    message id, recipient, body -- rather than an in-process assertion.  It
    covers the four classes plus the fetched/tombstone path, which is the
    five-notice cap the plan allows, and asserts nothing was addressed to any
    other recipient (no online group/DM, no owner, no real device login).
    """

    from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
    from tests.support.links.gate import permissive_admission_slot

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Explicit permissive premise: REQUESTER→WORKER is agent-to-agent through
    # a slot with no real gate yet (review item 3).
    inbox = InboxService(
        state_dir / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id="node-reg",
        link_admission=permissive_admission_slot(),
    )
    desired = DesiredStateStore(state_dir / "desired-state.json")
    desired.upsert_harness(
        HarnessLaunchSpec(harness="pi", name="squire", headless=True)
    )
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
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
    drill_ids = [f"drill-{n}" for n in range(1, 6)]
    for message_id in drill_ids:
        assert inbox.receive(_request(message_id)).accepted

    runtime.start()
    try:
        runtime.on_timer()  # one batch hands all five over; each clock starts here
        assert len(farm.deliveries) == 1

        now["v"] += 1_000
        # 1: quota class (permanent, immediate).  2: transient network class.
        farm.results.append(
            HarnessResult(
                delivery_id="drill-1",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="You've hit your usage limit",
                failure_code="PROVIDER_USAGE_LIMIT",
            )
        )
        farm.results.append(
            HarnessResult(
                delivery_id="drill-2",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="connection reset by peer",
                failure_code="PROVIDER_TRANSIENT_FAILURE",
            )
        )
        # 5: the row is consumed before the failure lands -> tombstone path.
        assert inbox.ack(WORKER, "drill-5").acknowledged
        farm.results.append(
            HarnessResult(
                delivery_id="drill-5",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="socket closed",
                failure_code="PROVIDER_TRANSIENT_FAILURE",
            )
        )
        runtime.on_timer()

        # The transient failure schedules a retry; acknowledge the request so
        # the retry does not itself become a sixth notice (the drill is capped
        # at five, and this is the sender walking away).
        assert inbox.ack(WORKER, "drill-2").acknowledged

        # 3 and 4 never produce an event: the clock must report them.
        now["v"] += 480_000 + 1
        runtime.on_timer()
        outbox = inbox.outbox_items()
    finally:
        runtime.stop()
        inbox.close()

    notices = [
        item.message
        for item in outbox
        if b'"notification"' in item.message.payload
    ]
    assert len(notices) == 5, "drill must produce exactly the five capped notices"
    assert {message.recipient for message in notices} == {REQUESTER}
    bodies = [json.loads(message.payload) for message in notices]
    kinds = [body["notification"] for body in bodies]
    assert kinds.count("provider-unavailable") == 3
    assert kinds.count("provider-no-progress") == 2
    assert {body["deliveryId"] for body in bodies} == set(drill_ids)
    assert all(body["nonce"] for body in bodies)
    assert all(body["requester"] == REQUESTER for body in bodies)
    quota = next(b for b in bodies if b["deliveryId"] == "drill-1")
    assert quota["failureCode"] == "PROVIDER_USAGE_LIMIT"
    assert quota["attemptId"] == "drill-1#1"
    # Nothing was addressed to anyone else: no owner routing, no worker self-
    # relay, and no online send (the memory transport has no peers, so every
    # notice is honestly sitting in the durable outbox).
    assert all(message.recipient == REQUESTER for message in notices)
    assert not [
        message
        for message in notices
        if message.recipient in {OWNER, WORKER}
    ]
