"""message.send: the main authoring flow and the agent-target delivery.

Decision notes:

- T1b (CM-PLAN「摘掉调用」, owner ruling 2026-11-19) removed the send-link
  gate from this boundary: no plan is asked, the submit carries no hold,
  and the result reports no ``holdReason``.  What survives lives in
  ``send_receipts.py`` (replay receipts, the conversation stamp, the
  reply-association checks of Δ2).
- Conversation plan slice P2 split this file as a pure move: the ``user:``
  target branch lives in ``inbox_surface/send_targets`` and
  ``message.reply`` in ``inbox_surface/reply``.
- I4b (owner items Q0-a and Q0-d, 2026-11-23): the ``route:`` target branch
  is deleted.  Attachments (``resourcePaths``) are pushed with the owner's
  copy of a send to this node's own ``user:<owner>``, to the same chats as
  the text; any other target with attachments is ``UNSUPPORTED_TARGET`` and
  attachments with ``replyTo`` are ``INVALID_ARGUMENT``, both before any
  side effect.  The two retired ``ROUTE_RESOURCE*`` codes are gone.
- Conversation plan slice C1 (owner ruling D1/D2): a send without a
  ``conversationId`` belongs to the default conversation of the stored sender
  and the target (``conversations.ids``), no longer to a fresh ``uuid4``.
  Each end is keyed by ``endpoint_key``: an id-form agent by its ``a.`` id
  alone, so the conversation survives a change of its owner or machine
  segment; every other end by its canonical address.
  The id is therefore per target: one send to several targets names one
  default conversation per target, each delivery entry carries its
  ``conversationId``, and the top-level one is the first target's.  An
  explicit id is used verbatim for every target, as before.
- The stored sender is the pair's sender side.  ``onBehalfOf`` does not move
  it: such a send is stored under the operator identity and so is its default
  conversation.
- A replay of one operation derives the same default again, so it names the
  conversation its first attempt was stored in.
- The dispatch gate's "this send opens its conversation" stays what it was
  for a send without an id, namely always true: such a send is keyed by its
  operation, never by the default id, which would make only a pair's first
  send ever count as opening.
- Conversation plan slice UA2 (2026-11-18): every target and the stored
  ``origin.onBehalfOf`` that is a user agent address (any machine's name
  form, or this machine's instance in any spelling by its registry marker;
  UA4) become ``user:<owner>``; such an address as the sender is refused with
  ``SENDER_NOT_AUTHENTICATED`` (UA2b item 7: it never has a session, the
  operator form is ``user:<owner>``; another machine's instance stays
  ``SENDER_UNRESOLVED``), so conversation ids derive from ``user:<owner>``
  (``user_agent.local_target.canonical_user_end``).  ``user:<this owner>`` with the owner's user agent on this
  node takes the ordinary queue block below: the same submit, message id
  formula and conversation ids as an agent target, with ``user:<owner>``
  kept as the envelope target.  Dispatch admission sees the user agent's
  facts (a role is refused), and after an accepted submit, replays
  included, the owner push runs; it never raises (I3 and I7a, owner item
  A-7, ``send_targets.user``).  Every other
  ``user:`` target keeps the push-only ``send_to_user_target``.
- UA2b item 4: a role on a send naming any ``user:`` target is refused for
  all of them before the loop, whether or not this node holds the user
  agent, so no earlier target of the call is submitted.  Agent targets keep
  their per-target gate inside the loop (an earlier target can already be
  submitted when a later one is refused; unchanged).
- Slice GC3: a send whose ``conversationId`` is a group id leaves this flow
  after the caller, summary and ``replyTo`` checks for
  ``inbox_surface.groups.messages.send_to_group`` (one group document
  entry, nothing queued); ``mentions`` without a group id is refused.
"""

from __future__ import annotations
import json
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
from hyprial.kernel import DaemonRequestError, ipc_errors, is_group_id_segment, is_own_user_uri
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxAuthorityTimeout,
    InboxAuthorityUnavailable,
    InboxMessage,
)
from hyprial.daemon.impl.inbox.contracts.summary import check_summary
from hyprial.daemon.impl.adapters.user_delivery import (
    is_user_target,
)
from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.alias_resolver import AliasSurface
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
    normalize_agent_recipient,
)
from hyprial.kernel import (
    TARGET_KIND_AGENT,
    parse_agent_uri,
)
from hyprial.daemon.impl.network.route_delivery  import (
    RouteDeliveryError,
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
from hyprial.daemon.impl.application.messaging.inbox_surface.send_receipts import (
    _SendReceiptsMixin,
)
from hyprial.daemon.impl.application.messaging.inbox_surface.send_targets.user import (
    accepts_owner_copy,
    push_owner_copy,
    push_recorded_user_message,
    send_to_user_target,
)
from hyprial.daemon.impl.user_agent.naming import is_user_agent_target
from hyprial.daemon.impl.user_agent.local_target import (
    USER_TARGET_FACTS,
    canonical_user_end,
    local_user_target,
    user_target_capabilities,
)
from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
    open_requests_by_sender,
)
from hyprial.daemon.impl.application.messaging.visibility import (
    _message_origin,
    report_unparsable_platform_created_at,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_string_param,
    _required_string,
)


def _stamp_conversations(
    deliveries: list[JsonObject], spans: list[tuple[int, str]]
) -> None:
    """Write each target's conversation id onto the entries it appended."""

    ends = [start for start, _ in spans[1:]] + [len(deliveries)]
    for (start, conversation), end in zip(spans, ends, strict=True):
        for entry in deliveries[start:end]:
            entry["conversationId"] = conversation


class _MessageSendMixin(_SendReceiptsMixin):
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _push_owner_copy(self, request: Any) -> Any:
        """Post another owner's message to this node's user agent cell (I7a)."""

        return push_owner_copy(self, request)

    def _accepts_owner_copy(self, owner: str) -> bool:
        """Whether this node accepts another node's hand-off for *owner* (S3c)."""

        return accepts_owner_copy(self, owner)

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
        if is_user_agent_target(self.agents, caller.sender):
            raise self._sender_not_authenticated(caller.sender)
        source = caller.sender
        alias_surface = {
            "adapter": AliasSurface.ADAPTER,
            "daemon": AliasSurface.DAEMON,
            "operator": AliasSurface.CLI,
            "session": AliasSurface.MCP,
        }[caller.via]
        self._queue_agent_activity(caller.subject)
        if is_group_id_segment(str(params.get("conversationId") or "")):
            from .groups.messages import send_to_group

            return send_to_group(self, params, sender=caller.sender, summary=summary)
        if params.get("mentions") is not None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "mentions are only for a group conversation"
            )
        targets = params.get("to")
        if not isinstance(targets, list) or not targets:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "to must be a non-empty array"
            )
        requested_targets = tuple(
            canonical_user_end(
                self._resolve_and_validate_input_alias(
                    normalize_agent_recipient(_required_string(target, "to item")),
                    surface=alias_surface,
                ),
                self.agents,
            )
            for target in targets
        )
        for target in requested_targets:
            _require_deliverable_send_target(target)
        if caller.via == "session":
            # The authenticated caller carries session-fence evidence.
            # Authorize the same frozen canonical targets that delivery
            # will use; resolving aliases again could select another actor.
            # Keep #956's authenticated caller intact for message origin.
            visibility_caller = self._visibility_identity(source)
            self._enforce_send_to(visibility_caller, requested_targets)
        raw_resources = params.get("resourcePaths")
        if isinstance(raw_resources, list) and raw_resources:
            if params.get("replyTo") is not None:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "attachments are not sent on a replyTo/harness_reply path; "
                    "no text or attachment was sent",
                )
            unsupported = [
                target
                for target in requested_targets
                if not (is_user_target(target) and is_own_user_uri(target, self.owner))
            ]
            if unsupported:
                raise DaemonRequestError(
                    ipc_errors.UNSUPPORTED_TARGET,
                    f"attachments are pushed only to user:<owner> of this node, "
                    f"not to {unsupported[0]!r}; no message was queued or sent",
                    {"target": unsupported[0]},
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
        origin = _message_origin(
            metadata,
            on_unparsable=lambda raw: report_unparsable_platform_created_at(
                raw, log=self._log, now_ms=int(time.time() * 1000)
            ),
        ) or {}
        origin["via"] = caller.via
        if caller.on_behalf_of is not None:
            origin["onBehalfOf"] = canonical_user_end(caller.on_behalf_of, self.agents)
        from hyprial.daemon.impl.conversations.ids import (
            default_conversation_id,
            endpoint_key,
        )

        explicit_conversation = (
            str(params["conversationId"]) if params.get("conversationId") else None
        )
        operation_id = str(params.get("idempotencyKey") or uuid4())
        gate_conversation = explicit_conversation or f"operation:{operation_id}"
        # Gate condition (a) (spec-dispatch-gate-classifier-2026-09-04):
        # only a send that OPENS its conversation can dispatch; later
        # sends under the same conversationId are answers/updates inside
        # it.  Keyed by the establishing operation id so an idempotent
        # replay of the opening send still counts as opening, while a
        # different send reusing the id does not.
        diagnostics = self._dispatch_diagnostics
        if diagnostics is None:
            with self._dispatch_without_pac_lock:
                establisher = self._dispatch_gate_conversations.get(gate_conversation)
                conversation_is_new = (
                    establisher is None or establisher == operation_id
                )
                if establisher is None:
                    self._dispatch_gate_conversations[gate_conversation] = operation_id
        else:
            try:
                conversation_is_new = diagnostics.open_conversation(
                    gate_conversation, operation_id
                )
            except TimeoutError as error:
                raise ipc_errors.DaemonUnavailableError(
                    "dispatch diagnostic authority is overloaded",
                ) from error
        deliveries: list[JsonObject] = []
        conversations = tuple(
            explicit_conversation
            or default_conversation_id(endpoint_key(source), endpoint_key(target))
            for target in requested_targets
        )
        # Optional advisory read (WP7 §2.5): which requests from these
        # targets are still unanswered.  It never blocks the send -- a
        # reminder read is not part of the delivery decision -- and a
        # failing reminder authority is reported, not raised.
        open_requests: dict[str, tuple[str, ...]] = {}
        if reply_to is None:
            request_senders = requested_targets
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
        if params.get("role") is not None:
            for target in requested_targets:
                if is_user_target(target):
                    dispatch_gate(target=target, capabilities=USER_TARGET_FACTS,
                                  role=params.get("role"), emit=self._log, source="message.send")
        spans: list[tuple[int, str]] = []
        for index, target in enumerate(requested_targets):
            _require_deliverable_send_target(target)
            conversation = conversations[index]
            spans.append((len(deliveries), conversation))
            user_queued = is_user_target(target) and local_user_target(self, target)
            if is_user_target(target) and not user_queued:
                send_to_user_target(
                    self,
                    deliveries,
                    caller=caller,
                    target=target,
                    index=index,
                    operation_id=operation_id,
                    source=source,
                    text=text,
                    reply_to=reply_to,
                    conversation=conversation,
                    resources=resources,
                )
                continue
            if not user_queued and classify_target_identity(target) != TARGET_KIND_AGENT:
                # Unreachable: _require_deliverable_send_target has
                # already rejected every non-deliverable shape.  Kept as
                # a fail-closed guard in case a future address form is
                # added to the classifier without a delivery branch here.
                raise DaemonRequestError(
                    ipc_errors.UNSUPPORTED_TARGET,
                    f"target {target!r} uses an address scheme this build "
                    "cannot deliver; deliverable forms are "
                    "agent:<owner>:<machine>:<agent> and user:<owner>",
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
            replay = self._replay_delivery(message_id, target=target)
            if replay is not None:
                if user_queued and replay["accepted"]:
                    replay.update(push_recorded_user_message(
                        self, caller=caller, target=target, operation_id=operation_id,
                        message_id=message_id, source=source, text=text,
                        conversation=conversation, resources=resources,
                    ))
                deliveries.append(replay)
                continue
            if user_queued:
                capabilities = user_target_capabilities(self)
            else:
                entity = self.agents.get(target)
                capabilities = entity.capabilities if entity is not None else {}
            dispatch_gate(
                target=target,
                capabilities=capabilities,
                role=params.get("role"),
                first_output_eta=_optional_string_param(params.get("first_output_eta"), "first_output_eta"),
                accepted_text=text,
                human_gates_declared="human_gates" in params,
                emit=self._log,
                source="message.send",
            )
            if reply_to is not None:
                self._validate_reply_association(
                    params,
                    source=source,
                    target=target,
                    reply_to=reply_to,
                    user_queued=user_queued,
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
                    },
                    separators=(",", ":"),
                ).encode(),
                intent="reply" if reply_to else "request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                idempotency_key=agent_key,
                created_at_ms=time.time_ns() // 1_000_000,
                summary=summary,
            )
            thread_id = self._conversation_stamp(source, target)
            if thread_id is not None:
                message = message.with_envelope(thread_id=thread_id)
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
            result = self._inbox.submit(message)
            if result.accepted and not user_queued:
                self._wake_dormant_agent(target, reason="pending-work")
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
                    **(
                        push_recorded_user_message(
                            self, caller=caller, target=target,
                            operation_id=operation_id, message_id=message.message_id,
                            source=source, text=text, conversation=conversation,
                            resources=resources,
                        )
                        if user_queued and result.accepted
                        else {}
                    ),
                    # queued=True means neither direct delivery nor a
                    # custody mailbox accepted the message; surface why
                    # instead of letting ok:true mask a silent deferral.
                    **(
                        {
                            "reason": "target-not-visible",
                            "resolutionReason": self._queued_resolution_reason(target),
                        }
                        if result.queued
                        and result.code != ipc_errors.SUBMIT_OUTCOME_UNKNOWN
                        else {}
                    ),
                }
            )
        _stamp_conversations(deliveries, spans)
        # The top-level id: the explicit one, else the first target's default.
        conversation = conversations[0]
        merged_requests: list[str] = []
        for target in requested_targets:
            for message_id in open_requests.get(target, ()):
                if message_id not in merged_requests:
                    merged_requests.append(message_id)
        return {
            "ok": all(item["accepted"] for item in deliveries),
            "actingPrincipal": source,
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

    def _queued_resolution_reason(self, target: str) -> str:
        parsed = parse_agent_uri(target)
        if parsed is None:
            return "no-route"
        node = parsed[1]
        if node != self.node_id and not self._presence.actor_online(node):
            return "node-offline"
        if not self._presence.actor_online(target):
            return "actor-unknown"
        return "no-route"

    def _registered_session_cwd(self, actor: str) -> str | None:
        """The cwd the daemon registered for this actor's session or worker."""

        for session in self._agent_session_domains.session.read_sessions():
            if session.actor == actor:
                return session.cwd
        for spec in self.desired_state.load().harnesses:
            if self._canonical_harness_uri(spec.name, spec) == actor:
                return spec.cwd
        return None
