"""message.send and message.reply: authoring sends with idempotent delivery per target kind."""

from __future__ import annotations
import json
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_reply_adapter,
)
from hyprial.kernel import DaemonRequestError, ipc_errors, reply_message_id
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxAuthorityTimeout,
    InboxAuthorityUnavailable,
    InboxMessage,
)
from hyprial.daemon.impl.inbox.contracts.summary import check_summary
from hyprial.daemon.impl.squire import (
    UserDeliveryRequest,
    UserDeliveryTarget,
    is_user_target,
)
from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.links.ports import HOLD_REASON_KEY
from hyprial.daemon.impl.alias_resolver import AliasSurface
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
    normalize_agent_recipient,
)
from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    CHANNEL_URI_PREFIX,
    TARGET_KIND_AGENT,
    TARGET_KIND_HOST,
)
from hyprial.daemon.impl.network.route_delivery  import (
    RouteDeliveryError,
    is_route_target,
    parse_route_resources,
)
from hyprial.daemon.impl.path_authz import (
    PathPolicy,
    operator_policy,
    session_attachment_policy,
)
from hyprial.daemon.impl.application.messaging.delivery.sends import (
    _require_deliverable_send_target,
)
from hyprial.daemon.impl.application.messaging.inbox_surface.agent_bot import (
    AgentBotSendSupport,
)
from hyprial.daemon.impl.application.messaging.inbox_surface.link_send import (
    _LinkSendMixin,
)
from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
    open_requests_by_sender,
)
from hyprial.daemon.impl.application.messaging.visibility import (
    _message_origin,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_string_param,
    _required_string,
)


class _MessageSendMixin(_LinkSendMixin, AgentBotSendSupport):
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ipc_message_send(self, method, params, _trusted_message_origin: str | None = None) -> Any:
        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=_trusted_message_origin,
        )
        # Validated before any side effect: a refused send does not enter the
        # outbox, register a conversation or reach the dispatch gate.
        summary = check_summary(params.get("summary"), required=True)
        # README ruling 39: an explicit reply association must name a message
        # id, so a non-string or empty ``replyTo`` is refused before the
        # payload is built (a malformed id could never close a request).
        reply_to = params.get("replyTo")
        if reply_to is not None and (
            not isinstance(reply_to, str) or not reply_to
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "replyTo must be a non-empty string naming the request this "
                "message answers; no message was queued or sent",
            )
        source = caller.sender
        alias_surface = {
            "adapter": AliasSurface.ADAPTER,
            "daemon": AliasSurface.DAEMON,
            "operator": AliasSurface.CLI,
            "session": AliasSurface.MCP,
        }[caller.via]
        self._queue_agent_activity(caller.subject)
        targets = params.get("to")
        if not isinstance(targets, list) or not targets:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "to must be a non-empty array"
            )
        requested_targets = tuple(
            self._resolve_and_validate_input_alias(
                normalize_agent_recipient(_required_string(target, "to item")),
                surface=alias_surface,
            )
            for target in targets
        )
        if caller.via == "session":
            # The authenticated caller carries session-fence evidence.
            # Authorize the same frozen canonical targets that delivery
            # will use; resolving aliases again could select another actor.
            # Keep #956's authenticated caller intact for message origin.
            visibility_caller = self._visibility_identity(source)
            self._enforce_send_to(visibility_caller, requested_targets)
            self._enforce_channel(visibility_caller, requested_targets)
        raw_resources = params.get("resourcePaths")
        if isinstance(raw_resources, list) and raw_resources:
            if params.get("replyTo") is not None:
                raise DaemonRequestError(
                    ipc_errors.ROUTE_RESOURCE_REPLY_UNSUPPORTED,
                    "attachments on replyTo/harness_reply paths are not "
                    "implemented; no text or attachment was sent",
                )
            unsupported = [
                target
                for target in requested_targets
                if not is_route_target(target)
            ]
            if unsupported:
                raise DaemonRequestError(
                    ipc_errors.ROUTE_RESOURCES_UNSUPPORTED,
                    "attachments are supported only for "
                    "route:<adapter>:<route> targets in this build; no "
                    "message was queued or sent",
                    {"unsupportedTargets": unsupported},
                )
        try:
            resources = parse_route_resources(
                raw_resources,
                self._attachment_policy(caller, _trusted_message_origin)
                if isinstance(raw_resources, list) and raw_resources
                else None,
            )
        except RouteDeliveryError as error:
            raise DaemonRequestError(error.code, str(error), error.data) from error
        text = _required_string(params.get("message"), "message")
        reply_to = self._link_reply_to_param(params.get("replyTo"))
        metadata = params.get("providerMetadata")
        sender_open_id = (
            metadata.get("senderId")
            if isinstance(metadata, dict)
            and isinstance(metadata.get("senderId"), str)
            else None
        )
        # Where this message came from, in the reporting adapter's words.  The
        # adapter already knows and says so -- Lark parses ``chat_type``
        # off the event and sends it in "providerMetadata" -- but only
        # ``senderId`` was ever read, so the value arrived and was dropped
        # before storage.  A reader could not then tell a group from a
        # direct message: Feishu uses the same ``oc_`` prefix for both, so
        # nothing else in the row distinguishes them.
        origin = _message_origin(metadata) or {}
        origin["via"] = caller.via
        if caller.on_behalf_of is not None:
            origin["onBehalfOf"] = caller.on_behalf_of
        conversation = str(params.get("conversationId") or uuid4())
        operation_id = str(params.get("idempotencyKey") or uuid4())
        # Gate condition (a) (spec-dispatch-gate-classifier-2026-09-04):
        # only a send that OPENS its conversation can dispatch; later
        # sends under the same conversationId are answers/updates inside
        # it.  Keyed by the establishing operation id so an idempotent
        # replay of the opening send still counts as opening, while a
        # different send reusing the id does not.
        diagnostics = self._dispatch_diagnostics
        if diagnostics is None:
            with self._dispatch_without_pac_lock:
                establisher = self._dispatch_gate_conversations.get(conversation)
                conversation_is_new = (
                    establisher is None or establisher == operation_id
                )
                if establisher is None:
                    self._dispatch_gate_conversations[conversation] = operation_id
        else:
            try:
                conversation_is_new = diagnostics.open_conversation(
                    conversation, operation_id
                )
            except TimeoutError as error:
                raise ipc_errors.DaemonUnavailableError(
                    "dispatch diagnostic authority is overloaded",
                ) from error
        deliveries: list[JsonObject] = []
        # Optional advisory read (WP7 §2.5): which requests from these
        # targets are still unanswered.  It never blocks the send -- a
        # reminder read is not part of the delivery decision -- and a
        # failing reminder authority is reported, not raised.
        open_requests: dict[str, tuple[str, ...]] = {}
        if reply_to is None:
            request_senders = tuple(
                target
                for target in requested_targets
                if not is_route_target(target)
            )
            if request_senders:
                try:
                    open_requests = open_requests_by_sender(
                        self._inbox,
                        recipient=source,
                        senders=request_senders,
                    )
                except (
                    InboxAuthorityUnavailable,
                    InboxAuthorityTimeout,
                ) as error:
                    self._log(
                        "warn",
                        "daemon",
                        "send.open_requests_unavailable",
                        sender=source,
                        error=type(error).__name__,
                    )
        for index, target in enumerate(requested_targets):
            _require_deliverable_send_target(target)
            if is_user_target(target):
                target_key = f"{operation_id}:{target}"
                message_id = str(
                    uuid5(NAMESPACE_URL, f"hyprial:send:{target_key}:{index}")
                )
                # user: targets are never link-governed, but ruling 53 still
                # measures the sender's W through the same gate call.
                link_plan = self._link_user_plan(
                    sender=source,
                    target=target,
                    intent="reply" if reply_to else "request",
                    reply_to=reply_to,
                    message_id=message_id,
                )
                # A recognised replay (``link_plan.replay``) skipped the gate
                # and books no second ledger row, but it still goes through
                # delivery: ``user_delivery`` owns the durable duplicate
                # result (original ``nativeMessageId``), delivers for real
                # when the first send never did, and serialises a retry
                # behind a first send that is still running (§2.4 step 2).
                if link_plan.ledger_open is not None:
                    self._link_user_open(link_plan.ledger_open)
                try:
                    user_target = UserDeliveryTarget.parse(target)
                except ValueError as error:
                    # §2.4 step 6: a failed send releases its ledger row in
                    # the same request instead of leaking it to the TTL.
                    self._link_user_release(source, message_id)
                    raise DaemonRequestError(
                        ipc_errors.INVALID_ARGUMENT, str(error)
                    ) from error
                try:
                    agent_bot = self._deliver_user_via_agent_bot(
                        caller=caller,
                        target=target,
                        owner=user_target.owner,
                        text=text,
                        target_key=target_key,
                        message_id=message_id,
                        conversation_id=conversation,
                        trusted_origin=_trusted_message_origin,
                    )
                except Exception:
                    self._link_user_release(source, message_id)
                    raise
                if agent_bot is not None:
                    deliveries.append(agent_bot)
                    if agent_bot["accepted"]:
                        if not agent_bot.get("duplicate"):
                            self._link_user_sent(
                                sender=source,
                                target=target,
                                message_id=message_id,
                                reply_to=reply_to,
                            )
                        self._log(
                            "info",
                            "daemon",
                            "send.received",
                            messageId=message_id,
                            correlationId=message_id,
                            node="daemon-send",
                            conversationId=conversation,
                            sender=source,
                            target=target,
                            nativeMessageId=agent_bot["nativeMessageId"],
                        )
                    else:
                        self._link_user_release(source, message_id)
                    continue
                if self._user_delivery is None:
                    self._link_user_release(source, message_id)
                    raise DaemonRequestError(
                        ipc_errors.USER_DELIVERY_UNAVAILABLE,
                        "user delivery transport is unavailable",
                    )
                try:
                    outcome = self._user_delivery.deliver(
                        UserDeliveryRequest(
                            message_id=message_id,
                            idempotency_key=target_key,
                            owner=user_target.owner,
                            sender=source,
                            message=text,
                            conversation_id=conversation,
                        )
                    )
                except Exception:
                    self._link_user_release(source, message_id)
                    raise
                if not outcome.accepted:
                    self._link_user_release(source, message_id)
                    raise DaemonRequestError(
                        outcome.code or ipc_errors.USER_DELIVERY_FAILED,
                        outcome.message or "user delivery failed",
                        {"target": target, "messageId": outcome.message_id},
                    )
                deliveries.append(
                    {
                        "target": target,
                        "messageId": outcome.message_id,
                        "accepted": True,
                        "queued": False,
                        "receiverAdapter": True,
                        **(
                            {"nativeMessageId": outcome.native_message_id}
                            if outcome.native_message_id is not None
                            else {}
                        ),
                        **({"duplicate": True} if outcome.duplicate else {}),
                    }
                )
                if not outcome.duplicate:
                    # A duplicate result means an earlier send already
                    # delivered and notified ``submitted`` (B6).
                    self._link_user_sent(
                        sender=source,
                        target=target,
                        message_id=message_id,
                        reply_to=reply_to,
                    )
                self._log(
                    "info",
                    "daemon",
                    "send.received",
                    messageId=outcome.message_id,
                    correlationId=outcome.message_id,
                    node="daemon-send",
                    conversationId=conversation,
                    sender=source,
                    target=target,
                    **(
                        {"nativeMessageId": outcome.native_message_id}
                        if outcome.native_message_id is not None
                        else {}
                    ),
                )
                continue
            if is_route_target(target):
                deliveries.extend(
                    self._deliver_route_target(
                        target,
                        text=text,
                        sender=source,
                        conversation=conversation,
                        operation_id=operation_id,
                        index=index,
                        resources=resources,
                    )
                )
                continue
            if classify_target_identity(target) != TARGET_KIND_AGENT:
                # Unreachable: _require_deliverable_send_target has
                # already rejected every non-deliverable shape.  Kept as
                # a fail-closed guard in case a future address form is
                # added to the classifier without a delivery branch here.
                raise DaemonRequestError(
                    ipc_errors.UNSUPPORTED_TARGET,
                    f"target {target!r} uses an address scheme this build "
                    "cannot deliver; deliverable forms are "
                    "agent:<owner>:<machine>:<agent>, user:<owner>, "
                    "and route:<adapter>:<route>",
                    {"target": target},
                )
            # Agent deliveries must survive the proxy's reconnect retry:
            # StatelessDaemonProxy replays the same operation id after an
            # "accepted, then the socket closed" ambiguity, expecting the
            # daemon to deduplicate.  User and route targets already derive
            # an idempotent id from the operation; agent targets minted a
            # fresh uuid4 with no idempotency key, so a replayed send
            # enqueued a second distinct probe; the pump then dispatched both
            # and each probe drew its own reply — the observed "two replies to
            # one probe".  Derive the id the same
            # way so a replay dedups at the recipient instead.
            agent_key = f"{operation_id}:{target}:{index}"
            message_id = str(uuid5(NAMESPACE_URL, f"hyprial:send:{agent_key}"))
            replay = self._link_replay_delivery(message_id, target=target)
            if replay is not None:
                deliveries.append(replay)
                continue
            entity = self.agents.get(target)
            dispatch_gate(
                target=target,
                capabilities=entity.capabilities if entity is not None else {},
                role=params.get("role"),
                first_output_eta=_optional_string_param(params.get("first_output_eta"), "first_output_eta"),
                accepted_text=text,
                human_gates_declared="human_gates" in params,
                emit=self._log,
                source="message.send",
            )
            link_plan = self._link_send_plan(
                sender=source,
                target=target,
                intent="reply" if reply_to else "request",
                reply_to=reply_to,
                message_id=message_id,
            )
            message = InboxMessage(
                message_id=message_id,
                conversation_id=conversation,
                sender=source,
                recipient=target,
                payload=json.dumps(
                    {
                        "message": text,
                        "topic": params.get("topic"),
                        # Ruling 39: the reply association belongs in the
                        # body of a message.send reply too, so the receiver
                        # can close the request's ledger row.
                        **({"replyTo": reply_to} if reply_to else {}),
                        # Absent when the adapter reports no origin: rows
                        # written before this change and messages from an
                        # adapter with no such concept simply lack the key.
                        "origin": origin,
                        # The explicit request this message answers, saved in
                        # the payload so the link gate and the link's RTT can
                        # see the association end to end (review blocking R2).
                        **(
                            {"replyTo": params.get("replyTo")}
                            if params.get("replyTo") is not None
                            else {}
                        ),
                    },
                    separators=(",", ":"),
                ).encode(),
                intent="reply" if reply_to else "request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                idempotency_key=agent_key,
                created_at_ms=time.time_ns() // 1_000_000,
                summary=summary,
            )
            self._log(
                "info",
                "daemon",
                "send.received",
                messageId=message.message_id,
                correlationId=message.message_id,
                node="daemon-send",
                conversationId=conversation,
                sender=source,
                target=target,
                **(
                    {"senderOpenId": sender_open_id}
                    if sender_open_id is not None
                    else {}
                ),
            )
            result = self._inbox.submit(message, **link_plan.submit_kwargs())
            if result.accepted:
                self._wake_dormant_agent(target, reason="pending-work")
                self._link_note_submitted(
                    sender=source,
                    target=target,
                    message_id=message.message_id,
                    reply_to=reply_to,
                    hold_reason=link_plan.hold_reason,
                )
            # A3 dispatch gate (design-dispatch-always-pac §三②,
            # narrowed by spec-dispatch-gate-classifier-2026-09-04):
            # classify an accepted coordinator→worker send — a dispatch
            # (counted) or a conversation (observable, uncounted).
            # Record-only by spec (先记不拦): nothing here blocks or
            # alters the delivery that just happened.  Only an accepted
            # submission dispatched anything; refusals count nothing.
            if (
                result.accepted
                and self._is_dispatch_candidate(
                    sender=source, recipient=target, intent=message.intent
                )
            ):
                self._classify_dispatch_send(
                    message, conversation_is_new=conversation_is_new
                )
            unresolved = result.queued and self._is_unresolved_local_identity(
                target
            )
            if unresolved:
                # The incident shape: a canonical URI naming THIS node
                # that nothing here claims can never leave the durable
                # queue.  Say so on the wire and in the log instead of
                # the historic accepted/queued silence.
                self._log(
                    "warn",
                    "daemon",
                    "send.target_unresolved",
                    messageId=message.message_id,
                    correlationId=message.message_id,
                    node="daemon-send",
                    conversationId=conversation,
                    sender=source,
                    target=target,
                )
            code = result.code or (
                ipc_errors.TARGET_UNRESOLVED if unresolved else None
            )
            deliveries.append(
                {
                    "target": target,
                    "messageId": message.message_id,
                    "accepted": result.accepted,
                    "queued": result.queued,
                    **(link_plan.delivery_fields() if result.accepted else {}),
                    **(
                        {"openRequestsFrom": list(open_requests[target])}
                        if open_requests.get(target)
                        else {}
                    ),
                    **(
                        {"outcomeKnown": False}
                        if result.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
                        else {}
                    ),
                    **({"code": code} if code else {}),
                    # queued=True means neither direct delivery nor a
                    # custody mailbox accepted the message; surface why
                    # instead of letting ok:true mask a silent deferral.
                    **(
                        {"reason": "target-not-visible"}
                        if result.queued
                        and result.code != ipc_errors.SUBMIT_OUTCOME_UNKNOWN
                        else {}
                    ),
                }
            )
        merged_requests: list[str] = []
        for target in requested_targets:
            for message_id in open_requests.get(target, ()):
                if message_id not in merged_requests:
                    merged_requests.append(message_id)
        return {
            "ok": all(item["accepted"] for item in deliveries),
            "operationId": operation_id,
            "conversationId": conversation,
            "deliveries": deliveries,
            **(
                {"openRequestsFrom": merged_requests} if merged_requests else {}
            ),
            **(
                {"replyPathUnavailable": True}
                if self._reply_path_unavailable(source)
                else {}
            ),
            **(
                {"messageId": deliveries[0]["messageId"]}
                if len(deliveries) == 1
                else {}
            ),
            # Single-target sends lift the hold to the top level so a
            # carrier can read ``queued``/``holdReason`` without walking
            # the deliveries array (WP2 §2.4 step 7, WP6 ``_lift_hold``).
            **(
                {"queued": True, HOLD_REASON_KEY: deliveries[0][HOLD_REASON_KEY]}
                if len(deliveries) == 1 and HOLD_REASON_KEY in deliveries[0]
                else {}
            ),
        }

    def _attachment_policy(
        self, caller: Any, trusted_origin: str | None
    ) -> PathPolicy | None:
        """Which local files this caller may attach (path_authz rule).

        ``via == "daemon"`` covers two callers: a genuine in-process call
        (no trusted IPC origin) and, while sender authentication only
        observes, an unauthenticated socket claim.  Only the first is
        trusted; the second may attach nothing.
        """

        if caller.via == "operator" or (
            caller.via == "daemon" and trusted_origin in (None, "daemon")
        ):
            return operator_policy(self.hyprial_home, state_dir=self.state_dir)
        if caller.via != "session":
            return None
        name = self.agents.local_actor(caller.subject)
        workspace = (
            self.hyprial_home / "agents" / name / "workspace"
            if name is not None
            else None
        )
        return session_attachment_policy(
            self.hyprial_home,
            actor_name=name,
            workspace=workspace,
            cwd=self._registered_session_cwd(caller.subject),
            state_dir=self.state_dir,
        )

    def _registered_session_cwd(self, actor: str) -> str | None:
        """The cwd the daemon registered for this actor's session or worker."""

        for session in self._agent_session_domains.session.read_sessions():
            if session.actor == actor:
                return session.cwd
        for spec in self.desired_state.load().harnesses:
            if spec.harness == "lark":
                continue
            if self._canonical_harness_uri(spec.name, spec) == actor:
                return spec.cwd
        return None

    def _ipc_message_reply(self, method, params, _trusted_message_origin: str | None = None) -> Any:
        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=_trusted_message_origin,
        )
        # Validated before any side effect: a refused reply does not ack the
        # original request.
        summary = check_summary(params.get("summary"), required=True)
        actor = caller.subject
        self._queue_agent_activity(actor)
        raw_resources = params.get("resourcePaths")
        if raw_resources is not None and (
            not isinstance(raw_resources, list) or raw_resources
        ):
            raise DaemonRequestError(
                ipc_errors.MESSAGE_REPLY_RESOURCES_UNSUPPORTED,
                "harness_reply attachments are not implemented; the "
                "original pending message was not acknowledged and no "
                "reply was queued",
            )
        message_id = _required_string(params.get("messageId"), "messageId")
        text = _required_string(params.get("message"), "message")
        original_key = actor
        original = None
        original_is_pending = True
        consumer_keys = self._message_consumer_keys(params, actor=actor)
        for key in consumer_keys:
            original = next(
                (
                    item
                    for item in self._inbox.pending_messages(key)
                    if item.message_id == message_id
                ),
                None,
            )
            if original is not None:
                original_key = key
                break
        if original is None:
            # Background reply settlement acknowledges the inbound row,
            # removing it from pending_messages before a caller's next
            # retry.  Join only an existing deterministic reply receipt;
            # the retained inbound row supplies the original route and
            # fences the receipt to this actor's consumer key.  A later
            # submit below checks the receipt's command digest, including
            # the reply text, without repeating native I/O.
            reply_id = reply_message_id(message_id)
            receipt_reader = getattr(
                self._inbox, "reply_submission_result", None
            )
            original_reader = getattr(
                self._inbox, "harness_failure_original", None
            )
            existing = (
                receipt_reader(reply_id) if callable(receipt_reader) else None
            )
            retained = (
                original_reader(message_id) if callable(original_reader) else None
            )
            if (
                existing is None
                or retained is None
                or retained.recipient not in consumer_keys
            ):
                # B5: let the gate address the code.  A drained id answers
                # LINK_RECLAIMED; everything else falls through to the
                # historic MESSAGE_REPLY_UNAVAILABLE here.
                self._link_reply_unavailable(
                    sender=caller.sender,
                    target=(
                        retained.sender if retained is not None else caller.sender
                    ),
                    message_id=message_id,
                )
                raise DaemonRequestError(
                    ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                    "pending message was not found",
                )
            original = retained
            original_key = retained.recipient
            original_is_pending = False
        adapter_name = lark_reply_adapter(original.sender)
        if (
            original_is_pending
            and original.sender.startswith(
                (CHANNEL_URI_PREFIX, ADAPTER_URI_PREFIX)
            )
            and adapter_name is None
        ):
            raise DaemonRequestError(
                ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                "the pending message came from a channel actor without a "
                "reply bridge",
            )
        if (
            original_is_pending
            and adapter_name is not None
            and self._adapters is not None
            and adapter_name not in self._lark_gateway_names()
        ):
            raise DaemonRequestError(
                ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                f"Lark reply adapter is not configured: {adapter_name}",
            )
        reply_origin: JsonObject = {"via": caller.via}
        if caller.on_behalf_of is not None:
            reply_origin["onBehalfOf"] = caller.on_behalf_of
        reply_body: JsonObject = {"message": text, "origin": reply_origin}
        # The explicit association is stored for every reply, not only the
        # adapter bridge: the gate and the link RTT read it back (R2).
        reply_body["replyTo"] = message_id
        # A dispatcher that sent with a bare --from leaves a bare sender
        # on the pending message; queued under that spelling the reply is
        # an outbox island no mailbox drains (it expires silently). Resolve
        # the recipient with the same pipeline message.send applies to its
        # targets; channel:/user: and other scheme-carrying senders pass
        # through untouched, and an ambiguous alias fails loudly here.
        recipient = self._resolve_agent_alias(
            normalize_agent_recipient(original.sender)
        )
        # Reply recipients that are node addresses hit the same silent
        # hole message.send now rejects; refuse loudly and leave the
        # original message pending instead.  channel:/user: senders pass
        # through: their bridges own delivery.
        if classify_target_identity(recipient) == TARGET_KIND_HOST:
            raise DaemonRequestError(
                ipc_errors.TARGET_IS_NODE,
                f"reply recipient {recipient!r} is a node address, not "
                "an agent: nodes are announced on the network but do "
                "not receive actor messages",
                {"target": recipient},
            )
        # No TARGET_UNRESOLVED refusal here anymore: a canonical URI
        # whose machine segment is this node is delivered into the
        # local durable inbox by the local-first transport whether or
        # not a connector currently claims it, and the read side
        # canonicalizes the same way — the row is drainable, so the
        # refusal would be a false rejection.
        # Ruling 55: a reply to an agent carries its association too.  The
        # receiver's dispatch correlation and its outbound ledger close both
        # read it, and previously only adapter replies wrote it.
        if classify_target_identity(recipient) == TARGET_KIND_AGENT:
            reply_body["replyTo"] = message_id
        # WP2 §2.4 message.reply: ask the gate before anything is
        # acknowledged.  A Hold keeps the question pending; an exception
        # (LINK_RECLAIMED, MESSAGE_REPLY_UNAVAILABLE, INVALID_ARGUMENT)
        # propagates untouched.
        reply_plan = self._link_reply_plan(
            sender=caller.sender, target=recipient, message_id=message_id
        )
        reply = InboxMessage(
            # Retrying the same harness_reply must address the same outbox
            # row.  Old rows use the same reply:<inbound-id> key but had a
            # random message id; the transport accepts both shapes.
            message_id=reply_message_id(message_id),
            conversation_id=original.conversation_id,
            sender=caller.sender,
            recipient=recipient,
            payload=json.dumps(reply_body, separators=(",", ":")).encode(),
            intent="reply",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            idempotency_key=f"reply:{message_id}",
            created_at_ms=time.time_ns() // 1_000_000,
            summary=summary,
        )
        try:
            submitted = self._inbox.submit(
                reply,
                link_hold=reply_plan.hold_reason,
                link_reply_close=reply_plan.hold_reason is None,
            )
        except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
            if isinstance(error, InboxAuthorityUnavailable) and str(error).startswith(
                "SUBMISSION_RECEIPT_CONFLICT"
            ):
                receipt_reader = getattr(
                    self._inbox, "reply_submission_result", None
                )
                existing = None
                if callable(receipt_reader):
                    try:
                        existing = receipt_reader(reply.message_id)
                    except Exception as read_error:  # noqa: BLE001
                        self._log(
                            "warn",
                            "daemon",
                            "harness.reply.receipt_read_failed",
                            messageId=message_id,
                            replyMessageId=reply.message_id,
                            failure=type(read_error).__name__,
                        )
                settled = existing is not None and not existing.queued
                replied = bool(
                    settled and existing is not None and existing.accepted
                )
                acknowledged = False
                if replied:
                    acknowledged = (
                        True
                        if not original_is_pending
                        else self._inbox.ack(
                            original_key, message_id
                        ).acknowledged
                    )
                return {
                    "ok": False,
                    "messageId": message_id,
                    "replyMessageId": reply.message_id,
                    "replied": replied,
                    "acknowledged": acknowledged,
                    "queued": existing.queued if existing is not None else True,
                    "code": "REPLY_ALREADY_SUBMITTED",
                    "retryable": False,
                    **({"settled": settled} if existing is not None else {}),
                }
            if isinstance(error, InboxAuthorityUnavailable) and (
                "CORRELATION_IN_FLIGHT" not in str(error)
            ):
                raise
            # Ambiguous external completion is not failure and must not
            # ACK the inbound question.  The deterministic reply message
            # id/correlation lets the caller retry and join the same
            # durable receipt without another native send.
            return {
                "ok": False,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": False,
                "acknowledged": False,
                "queued": True,
                "code": "REPLY_SETTLEMENT_PENDING",
                "retryable": True,
                "hint": "the reply is queued; retrying returns its state and never sends different text",
            }
        # A Lark bridge reply is not complete merely because its outbox
        # row was durably admitted.  ``queued`` means the native reply did
        # not succeed yet; the background outbox owns the single native
        # attempt and will settle the same stable receipt.  Keep the
        # inbound message pending and hand the caller the deterministic
        # reply id so a retry joins that receipt instead of triggering a
        # second native send or losing the user's question to an early ACK.
        if submitted.accepted and adapter_name is not None and submitted.queued:
            return {
                "ok": False,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": False,
                "acknowledged": False,
                "queued": True,
                "code": "REPLY_SETTLEMENT_PENDING",
                "retryable": True,
                "hint": "the reply is queued; retrying returns its state and never sends different text",
            }
        if reply_plan.hold_reason is not None:
            if not submitted.accepted:
                return {
                    "ok": False,
                    "messageId": message_id,
                    "replyMessageId": reply.message_id,
                    "replied": False,
                    "acknowledged": False,
                    "queued": submitted.queued,
                    "code": submitted.code or "REPLY_DELIVERY_PENDING",
                    HOLD_REASON_KEY: reply_plan.hold_reason,
                    **(
                        {"detail": reply_plan.detail}
                        if reply_plan.detail
                        else {}
                    ),
                }
            self._link_note_submitted(
                sender=caller.sender,
                target=recipient,
                message_id=reply.message_id,
                reply_to=message_id,
                hold_reason=reply_plan.hold_reason,
            )
            # A held reply must not acknowledge the original: it stays
            # pending until the link releases the reply (§3.6).
            return {
                "ok": True,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": False,
                "acknowledged": False,
                "queued": True,
                HOLD_REASON_KEY: reply_plan.hold_reason,
                **({"detail": reply_plan.detail} if reply_plan.detail else {}),
            }
        if not submitted.accepted:
            return {
                "ok": False,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": False,
                "acknowledged": False,
                "queued": submitted.queued,
                "code": submitted.code or "REPLY_DELIVERY_PENDING",
            }
        self._link_note_submitted(
            sender=caller.sender,
            target=recipient,
            message_id=reply.message_id,
            reply_to=message_id,
            hold_reason=None,
        )
        if not original_is_pending:
            return {
                "ok": True,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": True,
                "acknowledged": True,
                "queued": submitted.queued,
            }
        acknowledged = self._inbox.ack(original_key, message_id)
        return {
            "ok": acknowledged.acknowledged,
            "messageId": message_id,
            "replyMessageId": reply.message_id,
            "replied": True,
            "acknowledged": acknowledged.acknowledged,
            "queued": submitted.queued,
            **({"code": acknowledged.code} if acknowledged.code else {}),
        }
