from __future__ import annotations

from .phases import shared_org_space

def counts(ports, node: str) -> tuple[int, int]:
    """`(outbox, custody)` for one node: the two places a held message can be."""

    value = ports.exec_json(node, "ps")
    outbox = value.get("outboxCount")
    custody = value.get("custodyCount")
    if not isinstance(outbox, int) or not isinstance(custody, int):
        # Deliberate: contract violations are classified as RESULT FAIL by the
        # runner's AssertionError boundary.
        raise AssertionError(  # noqa: TRY004
            f"{node} ps does not report both hold counts: {value}"
        )
    return outbox, custody

def mailbox_custody(ports, recipient_node: str) -> None:
    """Phase 7: a mailbox takes the message, and records its own verdict.

    Design section 4 (decision G1): with a mailbox present the mailbox
    **takes** an undeliverable message -- ownership transfer, not a copy --
    and without one the sending daemon keeps it.  Phases 2-5 cover the second
    half; nothing covered the first, because no daemon ever set `as_mailbox`.

    Two messages, because one message cannot end both ways:

      7a  ownership transfer itself: the recipient is offline, the mailbox
          takes custody, and the sender is left holding NOTHING
      7b  `fetched` through the mailbox: the recipient returns, the mailbox
          delivers what it was holding, and the mailbox's own record appears
          in the sender's holder set
      7c  `expired` written by a holder that is neither sender nor recipient,
          on the mailbox's own TTL, and pulled back by the sender across
          containers with `holder` naming the mailbox.  This is the case
          PR #93 could not reach without a mailbox node.

    Order matters and is not the narrative order.  `custody_ttl_ms` is the
    same knob as the hold TTL, so the mailbox is also counting down on the
    message it is supposed to deliver: 7b has to complete (including the
    recipient's whole container restart) before that clock runs out, while
    7c's message simply waits out the TTL with nothing else pending.
    """

    node_a = ports.NODES[0]
    print(
        f"RESULT info starting {ports.NODE_D} (mailbox; hold TTL {ports.MAILBOX_HOLD_TTL_MS}ms)"
    )
    ports.run_container(
        ports.NODE_D,
        extra_env={
            "HYPRIAL_AS_MAILBOX": "1",
            "HYPRIAL_HOLD_TTL_MS": str(ports.MAILBOX_HOLD_TTL_MS),
        },
    )
    ports.wait_daemon_ready(ports.NODE_D)
    ports.register_driver(ports.NODE_D)
    ports.join_org_space(ports.NODE_D, shared_org_space(ports), actor=ports.driver_uri(ports.NODE_D))
    ports.wait_for_target(ports.NODE_D, node_a, timeout=90.0)
    ports.wait_for_target(node_a, ports.NODE_D, timeout=90.0)
    ports.assert_managed_endpoints(ports.NODE_D, ports.exec_json(ports.NODE_D, "ps"))
    ports.wait_for_mailbox(node_a, ports.NODE_D)
    print(f"RESULT ok phase7 {node_a} sees {ports.NODE_D} advertise the mailbox role")

    # 7c's clock starts first so its TTL runs while 7a and 7b work.  Addressed
    # to the actor nobody registers ON AN ONLINE NODE: it can never be
    # delivered, so the mailbox's own TTL is the only way it can end -- and
    # because the recipient's node answers status queries (empty, named
    # frame), the #519 projection keeps D's eviction a certain `expired`
    # instead of downgrading it to `unconfirmed`.  node_b is back online and
    # answering by the time 7c's verdict is pulled (7b restarted it).
    expiring_id = ports.send_one(
        "phase7c",
        node_a,
        ports.DRIVER,
        ports.ANSWERED_GHOST_ACTOR,
        f"CONTRACT_E2E_MAILBOX_TTL_{int(ports.time.time())}",
    )
    ports.assert_holds("phase7c", node_a, outbox=0, custody=0)
    ports.assert_holds("phase7c", ports.NODE_D, outbox=0, custody=1)

    # 7a: the recipient goes offline for real, and the mailbox takes the
    # message instead of the sender keeping it.
    print(f"RESULT info stopping {recipient_node} (mailbox custody phase)")
    ports.sh(["docker", "stop", recipient_node], timeout=30.0)
    ports.wait_for_target(node_a, recipient_node, absent=True, timeout=90.0)
    held_id = ports.send_one(
        "phase7a",
        node_a,
        ports.DRIVER,
        ports.driver_uri(recipient_node),
        f"CONTRACT_E2E_MAILBOX_{int(ports.time.time())}",
    )
    # Ownership transfer, asserted from both ends.  The sender holds the
    # message in NEITHER of the two places a message can be held, and the
    # mailbox reports itself as the holder.
    #
    # What this does NOT claim: that only one holder exists at every instant.
    # `submit` commits the outbox row, transfers custody, then deletes the
    # row, so a window exists where both rows are on disk, and no external
    # observer can sample inside it.  What is checkable is that the transfer
    # settles with exactly one holder, and that is what is asserted.
    ports.assert_holds("phase7a", node_a, outbox=0, custody=0, timeout=60.0)
    ports.assert_holds("phase7a", ports.NODE_D, outbox=0, custody=2)
    # A held message reads `pending` with a hold deadline on whoever holds it.
    on_mailbox = ports.delivery_status(ports.NODE_D, ports.driver_uri(node_a), held_id)
    if on_mailbox.get("state") != "pending" or not isinstance(
        on_mailbox.get("holdExpiresAtMs"), int
    ):
        raise AssertionError(
            f"phase7a the mailbox holds the message but does not read as its "
            f"holder: {on_mailbox}"
        )
    # And the sender, having given it away, has no verdict to report.  Note
    # this is `unknown`, not `pending`: `held_expiry_ms` is a local-only
    # lookup, so a sender cannot tell "a reachable mailbox is holding it"
    # from "nobody has anything".  Asserted as observed behaviour, not as
    # desirable -- see the scenario document's "Not covered" list.
    from_sender = ports.delivery_status(node_a, ports.driver_uri(node_a), held_id)
    if from_sender.get("state") != "unknown":
        raise AssertionError(
            f"phase7a sender still claims a verdict for a message it gave "
            f"away: {from_sender}"
        )
    print(
        f"RESULT ok phase7a mailbox took custody, sender holds nothing "
        f"(sender outbox/custody={ports.counts(node_a)}, mailbox={ports.counts(ports.NODE_D)})"
    )

    # 7b: the same custody, delivered instead of evicted.  Runs before 7c's
    # wait so the recipient's restart happens well inside the mailbox's TTL.
    print(f"RESULT info starting {recipient_node} (mailbox should deliver)")
    ports.sh(["docker", "start", recipient_node], timeout=30.0)
    ports.wait_daemon_ready(recipient_node)
    ports.wait_for_target(ports.NODE_D, recipient_node, timeout=90.0)
    ports.wait_for(
        lambda: ports.pending_ids(ports.exec_json(recipient_node, "ps")),
        lambda ids: held_id in ids,
        timeout=90.0,
        what=f"phase7b {ports.NODE_D} delivers the custody it was holding",
    )
    ack = ports.exec_json(recipient_node, "message", "ack", "--from", ports.driver_uri(recipient_node), held_id)
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"phase7b acknowledgement failed: {ack}")
    # Custody released -- asserted semantically, not by counting rows: 7c's
    # message is racing its own TTL on this same mailbox, so how many rows are
    # in custody right now is timing.  What is timing-independent is that D no
    # longer reports itself as THIS message's holder, because it has written a
    # terminal record for it instead.
    # D writes the terminal record in the SAME transaction that deletes the
    # custody row, so "D reports a verdict for this message" is exactly
    # equivalent to "D no longer holds it" -- and unlike a row count it says
    # so about THIS message.
    ports.wait_for_terminal_state(
        ports.NODE_D,
        ports.driver_uri(node_a),
        held_id,
        "fetched",
        timeout=60.0,
        what=f"phase7b {ports.NODE_D} releases the custody it delivered",
    )
    delivered = ports.wait_for_terminal_state(
        node_a,
        ports.driver_uri(node_a),
        held_id,
        "fetched",
        timeout=90.0,
        what=f"phase7b {node_a} pulls the fetched verdict for its custody message",
        # The recipient observed possession first-hand, so it wins the merge
        # over the mailbox's receipt mirror -- the mailbox belongs in the
        # holder set, not in the verdict.  Asserting `holder == mailbox` here
        # would require ranking hearsay above first-hand evidence, which is
        # the defect 6a fixes.
        expected_holder=recipient_node,
        required_mesh_holders=(ports.NODE_D, recipient_node),
    )
    ports.assert_terminal_record(
        "phase7b",
        delivered,
        sender_actor=ports.driver_uri(node_a),
        message_id=held_id,
        expected_state="fetched",
        expected_holders=(recipient_node,),
        expected_reason="ACK_RECEIVED",
    )
    ports.assert_no_known_loss("phase7b", delivered)
    print(
        f"RESULT ok phase7b mailbox delivered its custody; verdict merged from "
        f"holders {ports.mesh_holders(delivered)} {ports.format_diagnostics(delivered)}"
    )

    # 7c: the eviction verdict, written by a holder that is neither the sender
    # nor the recipient, and pulled back by the sender across containers.
    expired = ports.wait_for_terminal_state(
        node_a,
        ports.driver_uri(node_a),
        expiring_id,
        "expired",
        # Generous on purpose, and NOT just TTL + slack.  A custody row is
        # only re-examined when `next_attempt_ms <= now`, and every tick that
        # finds the recipient offline pushes that out along the retry ladder
        # (1s, 5s, 30s, 120s, ...).  With a 60s TTL the row's next visit after
        # the deadline lands around t+156s, so the eviction is bounded by the
        # ladder, not by the TTL.  Sizing this off the TTL alone would make
        # the phase fail on a rule it is not testing.
        timeout=ports.MAILBOX_HOLD_TTL_MS / 1000.0 + 180.0,
        what=f"phase7c {node_a} pulls the mailbox's expired verdict",
        expected_holder=ports.NODE_D,
        required_mesh_holders=(ports.NODE_D,),
    )
    ports.assert_terminal_record(
        "phase7c",
        expired,
        sender_actor=ports.driver_uri(node_a),
        message_id=expiring_id,
        expected_state="expired",
        # Only the mailbox ever held it after the transfer, so any other
        # holder would mean the sender kept a copy -- i.e. custody replicated
        # instead of transferring.
        expected_holders=(ports.NODE_D,),
        expected_reason="TTL_EXPIRED",
    )
    ports.assert_no_known_loss("phase7c", expired)
    expired_diagnostics = ports.diagnostics_of("phase7c", expired)
    if expired_diagnostics.get("localRecords") != 0:
        raise AssertionError(
            f"phase7c the sender answered from its own ledger, so the message "
            f"was never really handed over: {expired_diagnostics}"
        )
    print(
        f"RESULT ok phase7c non-sender holder recorded expired and the sender "
        f"pulled it across containers holder={ports.NODE_D} "
        f"{ports.format_diagnostics(expired)}"
    )

def assert_holds(ports, 
    label: str, node: str, *, outbox: int, custody: int, timeout: float = 30.0
) -> None:
    """Wait for the hold counts to settle, then pin them exactly.

    Polling rather than sampling once: custody transfer is several steps
    (commit outbox row, hand over, delete row) and a single sample can land
    mid-transfer.  The assertion itself stays exact.
    """

    ports.wait_for(
        lambda: ports.counts(node),
        lambda value: value == (outbox, custody),
        timeout=timeout,
        what=f"{label} {node} settles at outbox/custody {(outbox, custody)}",
    )

def wait_for_mailbox(ports, node: str, mailbox: str, *, timeout: float = 90.0) -> None:
    """Wait until `node` can actually see the mailbox's liveliness token.

    Without this the send races the token: a sender that has not seen a
    mailbox yet keeps the message in its own outbox, which is correct
    behaviour and the wrong phase.
    """

    ports.wait_for(
        lambda: ports.exec_json(node, "ps"),
        lambda value: mailbox in (value.get("mailboxes") or []),
        timeout=timeout,
        what=f"{node} sees mailbox {mailbox}",
    )
