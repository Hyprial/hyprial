from __future__ import annotations

from . import presence, tailcat

#: Org space per node, remembered for the life of the runner process so a
#: later phase on a different container can join the same directory.  The
#: ports facade is reconstructed on every call, so the cache cannot live on
#: it; a runner is one process and one scenario.
_ORG_SPACES: dict[str, str] = {}

def peer_of(ports, node: str) -> str:
    """The other two-machine node: endpoints are symmetric per pair."""

    return ports.NODES[1] if node == ports.NODES[0] else ports.NODES[0]

def owner_of_node(ports, node: str) -> str:
    """One container's owner, read back from its own `ps` report."""

    info = ports.exec_json(node, "ps").get("daemon", {})
    owner = str(info.get("owner") or "").strip() if isinstance(info, dict) else ""
    if not owner:
        raise AssertionError(f"{node} did not report an owner")
    return owner

def create_org_space(ports, node: str, *, prefix: str, actor: str) -> str:
    """Create one org space for the cross-container phases.

    T1b removed the link-engine readiness wait: agent-to-agent sends no
    longer consult a gate, so the org exists as soon as creation answers.
    """

    name = f"group-{prefix}-{int(ports.time.time())}"
    created = ports.exec_json(node, "org", "create", name, check=False)
    space_id = created.get("space_id", created.get("spaceId"))
    if not isinstance(space_id, str) or not space_id:
        raise AssertionError(f"{node} org create returned no space id: {created}")
    if created.get("tailnetInitialized") is not True or created.get("devicePublished") is not True:
        raise AssertionError(f"{node} org creation did not initialize and publish its real device: {created}")
    ports.exec_json(node, "org", "acl", "grant", name, actor)
    _ORG_SPACES[node] = space_id
    ports._ORG_NAMES[space_id] = name
    return space_id

def invite_org_owner(ports, node: str, space_id: str, owner: str) -> None:
    """Invite one owner into a space without a user-facing notification."""

    return tailcat.invite_owner(ports, node, space_id, owner)

def join_org_space(ports, node: str, space_id: str, *, actor: str) -> None:
    """Join a space another node created, retrying until it composes.

    The invite replicates asynchronously, so early joins legitimately fail;
    the budget -- not the first failure -- decides.
    """

    tailcat.join_org(ports, node, space_id, actor=actor)
    _ORG_SPACES[node] = space_id

def shared_org_space(ports) -> str:
    """The org space the primary pair created, for later containers to join."""

    for node in ports.NODES:
        space = _ORG_SPACES.get(node)
        if space is not None:
            return space
    raise AssertionError("no org space has been created for this scenario yet")

def assert_delivered_once(ports, 
    phase: str,
    sender: str,
    receiver: str,
    receiver_actor: str,
    message_id: str,
) -> None:
    """The recovery floor: not lost, not duplicated, outbox drains, ack works."""

    def receiver_pending() -> list[object]:
        return ports.pending_ids(ports.exec_json(receiver, "ps"))

    ports.wait_for(
        receiver_pending,
        lambda ids: message_id in ids,
        timeout=90.0,
        what=f"{phase} delivery after recovery",
    )
    copies = receiver_pending().count(message_id)
    if copies != 1:
        raise AssertionError(
            f"{phase} receiver holds {copies} copies of {message_id} (dedup broken)"
        )

    def sender_outbox() -> int:
        return ports.outbox_count(ports.exec_json(sender, "ps"))

    ports.wait_for(
        sender_outbox,
        lambda count: count == 0,
        timeout=90.0,
        what=f"{phase} sender outbox drained after receipt",
    )
    ack = ports.exec_json(receiver, "message", "ack", "--from", receiver_actor, message_id)
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"{phase} acknowledgement failed: {ack}")
    # The receipt path is complete; no retry may resurface a second copy.
    ports.time.sleep(2.0)
    if message_id in receiver_pending():
        raise AssertionError(f"{phase} duplicate delivery resurfaced after ack")


def queue_while_unreachable(ports, 
    phase: str, sender: str, sender_actor: str, receiver_actor: str, text: str
) -> str:
    """Send while the receiver cannot answer; assert durable queueing."""

    outbound = ports.exec_json(
        sender, "message", "send", "--from", sender_actor, "--to", receiver_actor,
        "--summary", text, text,
        timeout=60.0,
    )
    message_id = outbound.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise AssertionError(f"{phase} send returned no messageId: {outbound}")
    deliveries = outbound.get("deliveries", [])
    queued = (
        bool(deliveries[0].get("queued"))
        if isinstance(deliveries, list) and deliveries and isinstance(deliveries[0], dict)
        else False
    )
    if not queued:
        raise AssertionError(f"{phase} send was not queued: {outbound}")

    def sender_outbox() -> int:
        return ports.outbox_count(ports.exec_json(sender, "ps"))

    ports.wait_for(
        sender_outbox, lambda count: count >= 1, timeout=30.0,
        what=f"{phase} sender outbox",
    )
    return message_id

def offline_queue(ports, 
    sender: str, sender_actor: str, receiver: str, receiver_actor: str
) -> str:
    """Stop the receiver, queue a message on the sender, then deliver on restart.

    Returns the message id so phase 6 can ask, much later and after the
    sender has itself been restarted, what finally happened to it.
    """

    print(f"RESULT info stopping {receiver} (offline phase)")
    ports.sh(["docker", "stop", receiver], timeout=30.0)
    text = f"CONTRACT_E2E_OFFLINE_{int(ports.time.time())}"
    outbound = ports.exec_json(
        sender, "message", "send", "--from", sender_actor, "--to", receiver_actor,
        "--summary", text, text
    )
    message_id = outbound.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise AssertionError(f"offline send returned no messageId: {outbound}")
    print(
        "RESULT info offline send "
        f"messageId={message_id} "
        f"deliveries={ports.json.dumps(outbound.get('deliveries', []), ensure_ascii=False)}"
    )
    deliveries = outbound.get("deliveries", [])
    queued = (
        bool(deliveries[0].get("queued"))
        if isinstance(deliveries, list) and deliveries and isinstance(deliveries[0], dict)
        else False
    )
    if not queued:
        raise AssertionError(f"offline send was not queued: {outbound}")

    def sender_outbox() -> int:
        return ports.outbox_count(ports.exec_json(sender, "ps"))

    ports.wait_for(sender_outbox, lambda count: count >= 1, timeout=30.0, what="sender outbox")

    print(
        "RESULT info sender outbox while receiver is down: "
        f"{ports.json.dumps(ports.exec_json(sender, 'message', 'outbox', 'list'), ensure_ascii=False)}"
    )

    print(f"RESULT info starting {receiver} (delivery should follow)")
    ports.sh(["docker", "start", receiver], timeout=30.0)
    ports.wait_daemon_ready(receiver)
    # docker start re-runs the entrypoint, which re-exports the launcher
    # environment; pin that the endpoints are still effective after the
    # restart, not merely that delivery eventually succeeded.
    ports.assert_managed_endpoints(receiver, ports.exec_json(receiver, "ps"))
    print(
        f"RESULT ok phase2 {receiver} endpoints active after restart "
        "(launcher env re-read by entrypoint)"
    )

    def receiver_pending() -> list[object]:
        return ports.pending_ids(ports.exec_json(receiver, "ps"))

    ports.wait_for(
        receiver_pending,
        lambda ids: message_id in ids,
        timeout=90.0,
        what="offline message delivered after receiver restart",
    )
    ports.wait_for(
        sender_outbox,
        lambda count: count == 0,
        timeout=90.0,
        what="sender outbox drained after receipt",
    )
    ack = ports.exec_json(receiver, "message", "ack", "--from", receiver_actor, message_id)
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"offline acknowledgement failed: {ack}")
    print("RESULT ok offline queueing delivered offline message and drained outbox")
    return message_id

def delivery_terminal_state(ports, offline_message_id: str) -> None:
    """Phase 6: `fetched` and `expired` are durable, and pullable off-node.

    Read the phase names literally.  6a and 6b are the sender asking about its
    own message, and the sender mirrors `fetched` into its own ledger the
    moment a receipt confirms (`InboxService._attempt_direct`), so they prove
    the record exists and survives -- they do NOT prove the cross-container
    query leg.  6c and 6d do: the querying container provably holds no record
    of its own, so any verdict it reports crossed the network.
    """

    node_a, node_b = ports.NODES
    print(
        f"RESULT info starting {ports.NODE_C} (delivery-status phase; "
        f"hold TTL {ports.HOLD_TTL_MS}ms)"
    )
    ports.run_container(
        ports.NODE_C,
        extra_env={"HYPRIAL_HOLD_TTL_MS": str(ports.HOLD_TTL_MS)},
    )
    ports._capture_docker_state("c-launch")
    ports.wait_daemon_ready(ports.NODE_C)
    ports.register_driver(ports.NODE_C)
    # Hard cut: this container sends agent-to-agent too (the two ghosts), so
    # it must join the same org space the primary pair created.
    join_org_space(
        ports,
        ports.NODE_C,
        shared_org_space(ports),
        actor=ports.driver_uri(ports.NODE_C),
    )
    ports.wait_for_target(ports.NODE_C, node_a, timeout=90.0)
    ports.wait_for_target(node_a, ports.NODE_C, timeout=90.0)
    ports.assert_managed_endpoints(ports.NODE_C, ports.exec_json(ports.NODE_C, "ps"))

    # Start both eviction clocks first so the TTL waits overlap the
    # `fetched` assertions instead of being added to them.  Two ghosts, two
    # response-health cells: 6d's recipient node exists and answers, while
    # 6e's never does. Both are held and evicted by C alone.
    answered_ghost_id = ports.send_one(
        "phase6",
        ports.NODE_C,
        ports.DRIVER,
        ports.ANSWERED_GHOST_ACTOR,
        f"CONTRACT_E2E_GHOST_ANSWERED_{int(ports.time.time())}",
    )
    absent_ghost_id = ports.send_one(
        "phase6",
        ports.NODE_C,
        ports.DRIVER,
        ports.GHOST_ACTOR,
        f"CONTRACT_E2E_GHOST_ABSENT_{int(ports.time.time())}",
    )
    # Deliberately raced against the TTL: these must run inside HOLD_TTL_MS of
    # the sends above.  Margin is a few seconds of `docker exec` against 25s,
    # but if either assertion ever goes red on a loaded host, suspect the
    # clock before suspecting the semantics -- everything below is untimed.
    for ghost in (answered_ghost_id, absent_ghost_id):
        held = ports.delivery_status(ports.NODE_C, ports.driver_uri(ports.NODE_C), ghost)
        if held.get("state") != "pending" or not isinstance(
            held.get("holdExpiresAtMs"), int
        ):
            raise AssertionError(
                f"phase6 a held message must read as pending with a hold deadline, "
                f"not as a terminal verdict: {held}"
            )
    print("RESULT ok phase6 undelivered messages read pending on their holder")

    # 6a: `fetched` after an ordinary cross-container delivery.
    text = f"CONTRACT_E2E_STATUS_{int(ports.time.time())}"
    message_id = ports.send_one("phase6a", node_a, ports.DRIVER, ports.driver_uri(node_b), text)
    ports.wait_for(
        lambda: ports.pending_ids(ports.exec_json(node_b, "ps")),
        lambda ids: message_id in ids,
        timeout=45.0,
        what=f"phase6a {node_b} pending request",
    )
    ack = ports.exec_json(node_b, "message", "ack", "--from", ports.DRIVER, message_id)
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"phase6a acknowledgement failed: {ack}")
    online = ports.wait_for_terminal_state(
        node_a,
        ports.driver_uri(node_a),
        message_id,
        "fetched",
        timeout=60.0,
        what=f"phase6a {node_a} pulls the fetched verdict",
        # The recipient node's own ACK_RECEIVED must win over the sender's
        # receipt mirror. It used to lose: precedence ranked
        # `fetched` records earliest-first, and `submit` stamped the mirror
        # from a clock read taken BEFORE the blocking delivery, so the
        # hearsay copy was always the earlier of the two.
        expected_holder=node_b,
        required_mesh_holders=(node_b,),
    )
    record = ports.assert_terminal_record(
        "phase6a",
        online,
        sender_actor=ports.driver_uri(node_a),
        message_id=message_id,
        expected_state="fetched",
        # A delivered message ends with TWO fetched records: the recipient's
        # ACK_RECEIVED (inbox/service.py) and the sender's receipt mirror.
        # Precedence now ranks them by which node
        # observed the outcome, not by which clock ran ahead, so the winner
        # is the recipient every time and can be pinned.
        expected_holders=(node_b,),
        expected_reason="ACK_RECEIVED",
    )
    ports.assert_no_known_loss("phase6a", online)
    print(
        f"RESULT ok phase6a sender pulled fetched "
        f"holder={record.get('holder')} reason={record.get('reason')} "
        f"recordedAtMs={record.get('recordedAtMs')} "
        f"{ports.format_diagnostics(online)}"
    )

    # 6b: the same question about the phase-2 message, asked long after the
    # fact and across the sender's own phase-3 restart.
    offline = ports.wait_for_terminal_state(
        node_a,
        ports.driver_uri(node_a),
        offline_message_id,
        "fetched",
        timeout=60.0,
        what=f"phase6b {node_a} pulls the offline-queued message's verdict",
        expected_holder=node_b,
        required_mesh_holders=(node_b,),
    )
    record = ports.assert_terminal_record(
        "phase6b",
        offline,
        sender_actor=ports.driver_uri(node_a),
        message_id=offline_message_id,
        expected_state="fetched",
        expected_holders=(node_b,),
        expected_reason="ACK_RECEIVED",
    )
    ports.assert_no_known_loss("phase6b", offline)
    print(
        f"RESULT ok phase6b offline-queued message's verdict survived a "
        f"sender restart holder={record.get('holder')} "
        f"reason={record.get('reason')} recordedAtMs={record.get('recordedAtMs')} "
        f"{ports.format_diagnostics(offline)}"
    )

    # 6c: the cross-container proof for `fetched`.  C never held this message
    # and never saw the sender or the recipient touch it, so it has no local
    # record to answer from -- the verdict can only have come over the mesh.
    crossed = ports.wait_for_terminal_state(
        ports.NODE_C,
        ports.driver_uri(node_a),
        message_id,
        "fetched",
        timeout=60.0,
        what=f"phase6c {ports.NODE_C} pulls a verdict it never recorded",
        expected_holder=node_b,
        # BOTH holders, and this is the assertion the cross-host defect could
        # not pass.  A and B each hold a record for this message; zenoh's
        # default reply consolidation keys replies by key expression, and
        # every holder replies under the key that was queried, so the two
        # collapsed to one -- chosen by arrival race, with nothing reported
        # missing.  Measured before the fix: C received A's record and not
        # B's, `meshHolders=['<a>']`, `undecodable=0 noKnownLoss=True`.
        required_mesh_holders=(node_a, node_b),
    )
    record = ports.assert_terminal_record(
        "phase6c",
        crossed,
        sender_actor=ports.driver_uri(node_a),
        message_id=message_id,
        expected_state="fetched",
        expected_holders=(node_b,),
        expected_reason="ACK_RECEIVED",
    )
    crossed_diagnostics = ports.diagnostics_of("phase6c", crossed)
    if crossed_diagnostics.get("localRecords") != 0:
        raise AssertionError(
            f"phase6c {ports.NODE_C} answered from its own ledger, so this proves "
            f"nothing about the cross-host leg: {crossed_diagnostics}"
        )
    if sorted(ports.mesh_holders(crossed)) != sorted([node_a, node_b]):
        raise AssertionError(
            f"phase6c the verdict came from an incomplete holder set: "
            f"{crossed_diagnostics}"
        )
    ports.assert_no_known_loss("phase6c", crossed)
    print(
        f"RESULT ok phase6c fetched verdict pulled across containers by a node "
        f"that never held it holder={record.get('holder')} "
        f"reason={record.get('reason')} recordedAtMs={record.get('recordedAtMs')} "
        f"{ports.format_diagnostics(crossed)}"
    )

    # 6d: `expired`.  The holder evicts on its own TTL, with no operator
    # command and nothing to fetch it, and the verdict is then pulled both
    # locally and from a container that has no record of the message at all.
    # The recipient is the never-registered ghost on node_b: undeliverable,
    # but its node exists and answers. The eviction remains expired regardless
    # of recipient-holder response health.
    local_expiry = ports.wait_for_terminal_state(
        ports.NODE_C,
        ports.driver_uri(ports.NODE_C),
        answered_ghost_id,
        "expired",
        timeout=ports.HOLD_TTL_MS / 1000.0 + 90.0,
        what=f"phase6d {ports.NODE_C} evicts the undeliverable message on its own TTL",
    )
    ports.assert_terminal_record(
        "phase6d",
        local_expiry,
        sender_actor=ports.driver_uri(ports.NODE_C),
        message_id=answered_ghost_id,
        expected_state="expired",
        expected_holders=(ports.NODE_C,),
        expected_reason="TTL_EXPIRED",
    )
    ports.assert_no_known_loss("phase6d", local_expiry)
    # Holder response remains a diagnostic fact, independent of the verdict.
    if node_b not in ports.mesh_responders(local_expiry):
        raise AssertionError(
            f"phase6d the recipient holder {node_b} never answered, so the "
            f"certainty of this verdict is unproven: "
            f"{ports.diagnostics_of('phase6d', local_expiry)}"
        )
    remote_expiry = ports.wait_for_terminal_state(
        node_a,
        ports.driver_uri(ports.NODE_C),
        answered_ghost_id,
        "expired",
        timeout=60.0,
        what=f"phase6d {node_a} pulls the expired verdict from {ports.NODE_C}",
    )
    ports.assert_terminal_record(
        "phase6d",
        remote_expiry,
        sender_actor=ports.driver_uri(ports.NODE_C),
        message_id=answered_ghost_id,
        expected_state="expired",
        # Only C ever held it, so a holder other than C would mean the record
        # was invented rather than pulled.
        expected_holders=(ports.NODE_C,),
        expected_reason="TTL_EXPIRED",
    )
    ports.assert_no_known_loss("phase6d", remote_expiry)
    if node_b not in ports.mesh_responders(remote_expiry):
        raise AssertionError(
            f"phase6d the cross-boundary pull rests on the recipient holder "
            f"answering, and {node_b} did not: "
            f"{ports.diagnostics_of('phase6d', remote_expiry)}"
        )
    remote_diagnostics = ports.diagnostics_of("phase6d", remote_expiry)
    if remote_diagnostics.get("localRecords") != 0:
        raise AssertionError(
            f"phase6d {node_a} answered from its own ledger, so this proves "
            f"nothing about the cross-host leg: {remote_diagnostics}"
        )
    print(
        f"RESULT ok phase6d TTL eviction recorded expired and the verdict "
        f"pulled across containers responders={ports.mesh_responders(remote_expiry)} "
        f"{ports.format_diagnostics(remote_expiry)}"
    )

    # 6e: same holder (C), same TTL eviction, but the recipient's node never
    # exists and therefore never answers. The terminal outcome remains
    # expired; the absent holder is reported only in diagnostics.
    local_expiry = ports.wait_for_terminal_state(
        ports.NODE_C,
        ports.driver_uri(ports.NODE_C),
        absent_ghost_id,
        "expired",
        timeout=ports.HOLD_TTL_MS / 1000.0 + 90.0,
        what=f"phase6e {ports.NODE_C} reports the absent-recipient TTL expiry",
    )
    ports.assert_terminal_record(
        "phase6e",
        local_expiry,
        sender_actor=ports.driver_uri(ports.NODE_C),
        message_id=absent_ghost_id,
        expected_state="expired",
        expected_holders=(ports.NODE_C,),
        expected_reason="TTL_EXPIRED",
    )
    ports.assert_no_known_loss("phase6e", local_expiry)
    remote_expiry = ports.wait_for_terminal_state(
        node_a,
        ports.driver_uri(ports.NODE_C),
        absent_ghost_id,
        "expired",
        timeout=60.0,
        what=f"phase6e {node_a} pulls the TTL verdict from {ports.NODE_C}",
    )
    ports.assert_terminal_record(
        "phase6e",
        remote_expiry,
        sender_actor=ports.driver_uri(ports.NODE_C),
        message_id=absent_ghost_id,
        expected_state="expired",
        expected_holders=(ports.NODE_C,),
        expected_reason="TTL_EXPIRED",
    )
    ports.assert_no_known_loss("phase6e", remote_expiry)
    expiry_diagnostics = ports.diagnostics_of("phase6e", remote_expiry)
    missing = expiry_diagnostics.get("missingRecipientHolders")
    if not isinstance(missing, list) or ports.GHOST_ACTOR.split(":")[2] not in missing:
        raise AssertionError(
            f"phase6e the absent recipient node must be named as missing, so "
            f"operators can tell WHICH holder did not answer: {missing}"
        )
    print(
        f"RESULT ok phase6e absentRecipientExpired=true "
        f"reason=TTL_EXPIRED holder={ports.NODE_C} "
        f"missingRecipientHolders={missing} "
        f"{ports.format_diagnostics(remote_expiry)}"
    )


def mesh_reconnect(ports, 
    phase: str, sender: str, sender_actor: str, receiver: str, receiver_actor: str
) -> None:
    """`docker network disconnect` drops the link while both containers keep
    running: real network flapping, not a process stop.  The mesh must
    rebuild on `connect` and durable messages must catch up."""

    print(f"RESULT info disconnecting {receiver} from {ports.NETWORK} (mesh-heal phase)")
    ports.sh(["docker", "network", "disconnect", ports.NETWORK, receiver], timeout=30.0)
    try:
        message_id = ports.queue_while_unreachable(
            phase,
            sender,
            sender_actor,
            receiver_actor,
            f"CONTRACT_E2E_PARTITION_{int(ports.time.time())}",
        )
        # The partition is real: nothing may leak across while disconnected.
        ports.time.sleep(3.0)
        if ports.outbox_count(ports.exec_json(sender, "ps")) < 1:
            raise AssertionError(
                f"{phase} outbox drained while {receiver} was disconnected"
            )
    finally:
        ports.sh(["docker", "network", "connect", ports.NETWORK, receiver], timeout=30.0)
    # Presence must heal in both directions before delivery can resume.
    ports.wait_for_target(sender, receiver, timeout=90.0)
    ports.wait_for_target(receiver, sender, timeout=90.0)
    ports.assert_delivered_once(phase, sender, receiver, receiver_actor, message_id)
    # And a fresh roundtrip proves the mesh is fully healthy, not just able
    # to flush a backlog.
    ports.roundtrip(phase, receiver, receiver_actor, sender, sender_actor, f"CONTRACT_E2E_REQUEST_{phase}")
    print(f"RESULT ok {phase} mesh rebuilt after partition and backlog delivered")

def suspend_resume(ports, 
    phase: str, sender: str, sender_actor: str, receiver: str, receiver_actor: str
) -> None:
    """`docker pause` freezes the process but keeps its network namespace: the
    laptop-lid-close shape.  The peer looks connected yet never answers,
    which stop/start cannot reproduce."""

    print(f"RESULT info pausing {receiver} (suspend/resume phase; TCP stays up)")
    ports.sh(["docker", "pause", receiver], timeout=30.0)
    try:
        message_id = ports.queue_while_unreachable(
            phase,
            sender,
            sender_actor,
            receiver_actor,
            f"CONTRACT_E2E_SUSPEND_{int(ports.time.time())}",
        )
        # The frozen peer must receive nothing for the whole suspension.
        ports.time.sleep(3.0)
        if ports.outbox_count(ports.exec_json(sender, "ps")) < 1:
            raise AssertionError(
                f"{phase} outbox drained while {receiver} was paused"
            )
    finally:
        ports.sh(["docker", "unpause", receiver], timeout=30.0)
    ports.wait_daemon_ready(receiver)
    ports.assert_delivered_once(phase, sender, receiver, receiver_actor, message_id)
    print(f"RESULT ok {phase} suspend/resume delivered without loss or duplication")



def pending_ids(ports, value: dict[str, object]) -> list[object]:
    pending = value.get("pendingMessages", [])
    return (
        [item.get("messageId") for item in pending if isinstance(item, dict)]
        if isinstance(pending, list)
        else []
    )

def zenoh_of(ports, value: dict[str, object]) -> dict[str, object]:
    zenoh = value.get("zenoh")
    if not isinstance(zenoh, dict):
        # Deliberate: contract violations are classified as RESULT FAIL by the
        # runner's AssertionError boundary.
        raise AssertionError(f"ps result is missing the zenoh endpoints: {value}")  # noqa: TRY004
    return zenoh

def driver_uri(ports, node: str) -> str:
    """Return the created identity even while its container is stopped."""

    if node not in ports._DRIVERS:
        raise AssertionError(f"{node} driver has not been registered")
    return ports._DRIVERS[node]

def advertised_target(ports, node: str, peer_actor: str) -> str:
    """The exact `targetUri` a peer advertises for `peer_actor` on `node`."""

    value = ports.exec_json(node, "message", "targets")
    targets = value.get("targets", [])
    if not isinstance(targets, list):
        raise AssertionError(f"{node} targets is not an array: {value}")  # noqa: TRY004
    for item in targets:
        if isinstance(item, dict) and item.get("actor") == peer_actor:
            uri = item.get("targetUri")
            if isinstance(uri, str) and uri:
                return uri
    raise AssertionError(f"{node} does not advertise {peer_actor}: {value}")

def roundtrip(ports, 
    phase: str,
    sender: str,
    sender_actor: str,
    receiver: str,
    receiver_actor: str,
    text: str,
    *,
    receiver_target: str | None = None,
) -> None:
    outbound = ports.exec_json(
        sender,
        "message",
        "send",
        "--from",
        sender_actor,
        "--to",
        receiver_target or receiver_actor,
        "--summary",
        text,
        text,
    )
    message_id = outbound.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise AssertionError(f"{phase} send returned no messageId: {outbound}")
    # Whatever address form was used, the daemon must persist exactly one
    # recipient form: the bare actor name.
    deliveries = outbound.get("deliveries", [])
    if not (
        isinstance(deliveries, list)
        and deliveries
        and isinstance(deliveries[0], dict)
        and deliveries[0].get("target") == receiver_actor
    ):
        raise AssertionError(
            f"{phase} send did not normalize the recipient to {receiver_actor}: "
            f"{outbound}"
        )

    def b_pending() -> dict[str, object]:
        return ports.exec_json(receiver, "ps")

    ports.wait_for(
        b_pending,
        lambda value: ports.pending_ids(value) == [message_id],
        timeout=45.0,
        what=f"{phase} {receiver} pending request",
    )
    ack = ports.exec_json(receiver, "message", "ack", "--from", receiver_actor, message_id)
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"{phase} receiver acknowledgement failed: {ack}")

    reply = ports.exec_json(
        receiver, "message", "send", "--from", receiver_actor, "--to", sender_actor,
        "--summary", f"{text}:reply", f"{text}:reply"
    )
    reply_id = reply.get("messageId")
    if not isinstance(reply_id, str) or not reply_id:
        raise AssertionError(f"{phase} reply returned no messageId: {reply}")

    def a_pending() -> dict[str, object]:
        return ports.exec_json(sender, "ps")

    ports.wait_for(
        a_pending,
        lambda value: ports.pending_ids(value) == [reply_id],
        timeout=45.0,
        what=f"{phase} {sender} pending reply",
    )
    ack = ports.exec_json(sender, "message", "ack", "--from", sender_actor, reply_id)
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"{phase} sender acknowledgement failed: {ack}")
    print(f"RESULT ok {phase} roundtrip {text}")

def register_driver(ports, node: str) -> None:
    created = ports.exec_json(node, "agent", "create", "--name", ports.DRIVER)
    record = created.get("agent")
    uri = record.get("uri") if isinstance(record, dict) else None
    owner = owner_of_node(ports, node)
    if not isinstance(uri, str) or not uri.startswith(f"agent:{owner}:{node}:a."):
        raise AssertionError(f"{node} driver did not publish an id address: {created}")
    ports._OWNERS[node] = owner
    ports._DRIVERS[node] = uri

def wait_for_target(ports, 
    node: str, peer: str, *, absent: bool = False, timeout: float = 45.0
) -> None:
    return presence.wait_for_target(ports, node, peer, absent=absent, timeout=timeout)

def outbox_count(ports, value: dict[str, object]) -> int:
    count = value.get("outboxCount", 0)
    if not isinstance(count, int):
        # Deliberate: contract violations are classified as RESULT FAIL by the
        # runner's AssertionError boundary.
        raise AssertionError(f"ps result has an invalid outboxCount: {value}")  # noqa: TRY004
    return count

def wait_daemon_ready(ports, node: str) -> None:
    def probe() -> dict[str, object]:
        value = ports.exec_json(node, "ps")
        address = ports.sh(
            ["docker", "exec", node, "test", "-s", "/data/home/state/tailcat/address"],
            timeout=30.0, check=False,
        )
        return value if address.returncode == 0 else {}

    ports.wait_for(
        probe,
        lambda value: isinstance(value.get("daemon"), dict)
        and value.get("daemon", {}).get("running") is True,
        timeout=60.0,
        what=f"{node} daemon ready",
    )

def assert_terminal_record(ports, 
    label: str,
    value: dict[str, object],
    *,
    sender_actor: str,
    message_id: str,
    expected_state: str,
    expected_holders: tuple[str, ...],
    expected_reason: str | None = None,
) -> dict[str, object]:
    """Assert the querier really holds the verdict, not merely that it replied.

    `state` on its own would be a weak assertion: `pending` and `unknown` are
    perfectly successful, non-error answers, and they are exactly what a
    broken pull returns.  So the record body has to be there too, and it has
    to name the same message, the same sender, and a holder that could
    plausibly have observed the outcome.
    """

    state = value.get("state")
    if state != expected_state:
        raise AssertionError(
            f"{label}: state is {state!r}, expected {expected_state!r}: {value}"
        )
    records = value.get("records")
    if not isinstance(records, list) or len(records) != 1:
        raise AssertionError(
            f"{label}: expected exactly one terminal record, got: {value}"
        )
    record = records[0]
    if not isinstance(record, dict):
        raise AssertionError(f"{label}: terminal record is not an object: {value}")  # noqa: TRY004
    for field, expected in (
        ("messageId", message_id),
        ("sender", sender_actor),
        ("state", expected_state),
    ):
        if record.get(field) != expected:
            raise AssertionError(
                f"{label}: record {field}={record.get(field)!r} != {expected!r}: "
                f"{record}"
            )
    holder = record.get("holder")
    if not isinstance(holder, str) or holder not in expected_holders:
        raise AssertionError(
            f"{label}: holder {holder!r} is not one of {expected_holders}: {record}"
        )
    if expected_reason is not None and record.get("reason") != expected_reason:
        raise AssertionError(
            f"{label}: reason {record.get('reason')!r} != {expected_reason!r}: "
            f"{record}"
        )
    recorded_at = record.get("recordedAtMs")
    if not isinstance(recorded_at, int) or recorded_at <= 0:
        raise AssertionError(f"{label}: record carries no recording time: {record}")
    return record
