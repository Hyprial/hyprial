"""Lark event conversion, routing, acknowledgement, and receipt correlation."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any

from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError

from hyprial.daemon.impl.adapters.lark.contracts.messages import (
    MERGE_FORWARD_LABEL,
)
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    ActorTarget,
    HarnessRequest,
    HarnessSubmissionRejected,
    InboundOutcome,
    LarkInboundMessage,
    is_safe_runtime_target_field,
)
from hyprial.daemon.impl.adapters.lark.outbound.reactions import (
    ReactionEffectAdmission,
)
from hyprial.daemon.impl.adapters.lark.state.records import (
    PendingCommandCapacityError,
    PendingInboundSubmissionCapacityError,
    RequestCorrelation,
)
# PR #332 F3: in-place bounded retry for daemon-bound inbound steps during
# the daemon's restore window.  The budget must cover a derived restore
# round (F1: (ceil(targets/width)+1) x per-start timeout) for typical
# fleets -- 120s covers ~8 targets at the default admission width and
# start timeout -- while staying short enough that a wedged restore does
# not pin the ordered inbound lane for long.
from hyprial.daemon.impl.adapters.lark.inbound.commands import (
    _bounded_listing,
    _command_response_kind,
)
from hyprial.daemon.impl.adapters.lark.outbound.reactions import ACK_EMOJI
DEFAULT_TRANSIENT_RETRY_BUDGET_SECONDS = 120.0
DEFAULT_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS = 0.5
DEFAULT_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS = 5.0
# These IPC checks reject before the single Lark target can be submitted.
_SUBMISSION_PRE_ADMISSION_CODES = frozenset({
    ipc_errors.INVALID_REQUEST, ipc_errors.INVALID_ARGUMENT,
    ipc_errors.VERSION_MISMATCH, ipc_errors.UNSUPPORTED_TARGET,
    ipc_errors.TARGET_IS_NODE, ipc_errors.SENDER_UNRESOLVED,
    ipc_errors.AMBIGUOUS_TARGET, ipc_errors.DISPATCH_NO_CAPABLE_HARNESS,
    ipc_errors.DISPATCH_ROLE_MISMATCH,
})
class TransientHarnessRetryExhausted(RuntimeError):
    """A transient daemon refusal outlived the inbound retry budget.

    Carries the refusal's stable code (or, for a transport blip, the
    exception name) so the dead-letter trail records WHY the budget burned
    out -- distinct from a permanent ``forward-error`` rejection.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = detail


class _InboundPipelineMixin:
    def _expanded_merge_forward(
        self, message: LarkInboundMessage
    ) -> LarkInboundMessage:
        """Replace a forward's placeholder with the text its children carry.

        The websocket event for a merge-forward carries only the platform's
        ``Merged and Forwarded Message`` placeholder -- the forwarded
        conversation exists solely in ``im.v1.message.get``.  The bounded
        fetch executes on the inbound actor's ordered effect lane rather than
        an SDK websocket thread.

        This never raises and never dead-letters: an unreachable platform
        degrades to a loud label, because a forward the agent can see the
        shape of beats one it never receives.

        Duplicates are pre-checked (reads take their own state locks) so a
        redelivered event or a reconcile sweep full of already-seen forwards
        does not refetch each one; :meth:`_handle_inbound` remains the
        authoritative duplicate gate.
        """

        if self._state.seen(message.event_id) or self._state.seen_message(
            message.message_id
        ):
            return message
        try:
            quote = self._lark.get_message(message.message_id)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform boundary
            del error
            quote = None
        if quote is None or not quote.text.strip():
            return replace(
                message,
                text=f"[{MERGE_FORWARD_LABEL} (contents unavailable)]",
            )
        return replace(message, text=quote.text.strip())


    def _with_transient_retry(
        self, operation: Callable[[], Any], *, retry_submission_unknown: bool = False,
        prior_submission_unknown: bool = False,
        recovery_deadline: float | None = None,
    ) -> Any:
        """Run one daemon-bound inbound step, retrying transient refusals.

        PR #332 F3: a DAEMON_RESTORING envelope is the daemon's restore
        gate refusing the call at dispatch -- BEFORE any side effect -- so
        retrying it in place is safe even for the ``message.send``
        mutation when the original idempotency key is retained. Transport
        failure is not proof that a send had no effect. Only frozen inbound
        submissions opt into unknown-outcome handling; generic daemon errors
        remain non-retried, but do not become false terminal send failures.
        The budget and backoff are bounded. Restore exhaustion remains
        TransientHarnessRetryExhausted; ambiguous send exhaustion remains
        SubmitOutcomeUnknownError with its frozen request still in custody.
        """
        backoff = self._transient_retry_backoff_initial
        deadline = time.monotonic() + self._transient_retry_budget
        if recovery_deadline is not None:
            deadline = min(deadline, recovery_deadline)
        unknown = (
            ipc_errors.SubmitOutcomeUnknownError("previous frozen submission remains unconfirmed")
            if prior_submission_unknown else None
        )
        while True:
            if recovery_deadline is not None and time.monotonic() >= recovery_deadline:
                raise ipc_errors.SubmitOutcomeUnknownError("pending recovery deadline elapsed")
            if self._closing.is_set():
                if retry_submission_unknown:
                    raise ipc_errors.SubmitOutcomeUnknownError(
                        "adapter closed before submission settlement",
                        None if unknown is None else unknown.data,
                    )
                raise RuntimeError("adapter closing")
            try:
                return operation()
            except (NameError, ImportError):
                raise
            # Restore refusal is typed and pre-effect. Ambiguous submission
            # results below are separate and require the frozen send request.
            except ipc_errors.DaemonRestoringError as error:
                detail = error.code
            except ipc_errors.SubmitOutcomeUnknownError as error:
                if not retry_submission_unknown:
                    raise
                unknown = error
                detail = error.code
            except (DaemonRequestError, HarnessSubmissionRejected) as error:
                if unknown is not None:
                    raise ipc_errors.SubmitOutcomeUnknownError(
                        "earlier submission outcome remains unconfirmed", unknown.data
                    ) from None
                if (retry_submission_unknown and isinstance(error, DaemonRequestError)
                        and error.code not in _SUBMISSION_PRE_ADMISSION_CODES):
                    raise ipc_errors.SubmitOutcomeUnknownError(
                        "daemon response does not establish submission rejection",
                        {"causeCode": error.code},
                    ) from None
                raise
            except (OSError, TimeoutError) as error:
                detail = type(error).__name__
                if retry_submission_unknown:
                    unknown = ipc_errors.SubmitOutcomeUnknownError(
                        "submission transport did not confirm its outcome",
                        {"causeType": detail},
                    )
            except Exception as error:
                if retry_submission_unknown:
                    raise ipc_errors.SubmitOutcomeUnknownError(
                        "submission reply could not be validated",
                        {"causeType": type(error).__name__},
                    ) from None
                raise
            if recovery_deadline is not None or time.monotonic() >= deadline:
                if unknown is not None:
                    raise ipc_errors.SubmitOutcomeUnknownError(
                        "submission outcome remains unconfirmed after retry budget", unknown.data
                    ) from None
                raise TransientHarnessRetryExhausted(detail) from None
            self._closing.wait(min(backoff, max(0.0, deadline - time.monotonic())))
            backoff = min(backoff * 2.0, self._transient_retry_backoff_max)


    def _handle_inbound(
        self, message: LarkInboundMessage, *, suppress_guidance: bool = False,
        frozen_recovery: bool = False, recovery_deadline: float | None = None,
    ) -> InboundOutcome:
        if self._closing.is_set():
            return InboundOutcome(status="error", ack_error="inbound-closing")
        # Idempotency is keyed on BOTH the event id (platform redelivery of
        # the same event) and the native message id (the same message
        # arriving via live event, manual recovery, or reconcile replay).
        if self._state.seen(message.event_id) or self._state.seen_message(
            message.message_id
        ):
            return InboundOutcome(status="duplicate")
        # Retirement is lifted by evidence from OUTSIDE the sweep, never by
        # the sweep that imposed it.  The invariant covering every upstream
        # of this function (live SDK events, reconcile replays, and
        # ``recover_message`` re-drives): any path that reaches here holds a
        # ``LarkInboundMessage`` the platform served for this chat, and the
        # platform only serves a chat's messages to a member -- so a served
        # message IS membership evidence.  ``recover_message`` is covered by
        # its own early return: when the platform will not serve the body
        # (bot not in the chat), ``get_inbound_message`` answers None and it
        # returns ``not-found`` BEFORE reaching here.  That early return
        # rests on the platform refusing non-member reads of
        # ``im.v1.message.get``; if a tenant-level scope ever lets a
        # non-member read through, the worst case is a self-correcting
        # oscillation (rejoin scan set -> next sweep eats 230002 -> retired
        # again), never data loss.
        # The read probe keeps the common case (nothing ever retired, or the
        # row already gone) free of a write transaction per inbound message.
        if self._state.retired_chat_code(message.chat_id) is not None:
            if frozen_recovery:
                # A stored request is not fresh platform membership evidence.
                return InboundOutcome(status="submission-held", ack_error="chat-retired")
            self._unretire_chat(message.chat_id)
        try:
            frozen = self._state.pending_inbound_submission(message.message_id)
            if frozen is not None:
                return self._submit_inbound_request(
                    message, frozen, recovering=True, recovery_deadline=recovery_deadline
                )
            if message.message_type == "merge_forward":
                message = self._expanded_merge_forward(message)
            # Remember the chat type observed on live traffic; the REST
            # message models carry no chat_type, so recovery paths rely on
            # this record to route recovered messages from the same chat.
            if message.chat_type != "unknown":
                self._state.record_chat_type(message.chat_id, message.chat_type)
            self._observe_sender_identity(message)
            if message.content_status != "supported":
                reason = (
                    "unsupported-message-type"
                    if message.content_status == "unsupported"
                    else "invalid-message-content"
                )
                created = self._record_dead_letter(message, reason=reason)
                return InboundOutcome(status=reason, new_dead_letter=created)
            return self._forward_inbound(message, suppress_guidance=suppress_guidance)
        except PendingCommandCapacityError as error:
            # Capacity is custody, not a cache. Refuse the new command before
            # any Lark platform send and expose only bounded numeric state.
            return InboundOutcome(
                status="command-capacity-exhausted",
                pending_capacity=error.capacity,
            )
        except PendingInboundSubmissionCapacityError:
            return InboundOutcome(status="submission-capacity-exhausted")
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - failure-path preservation
            # The forwarding path failed (IPC, state, platform).  The event
            # is gone from the websocket stream for good, so preserve the
            # body before reporting the failure.  A transient refusal that
            # outlived the retry budget is recorded under its own reason so
            # replay tooling can tell "the daemon never finished restore"
            # from a permanent rejection.  Either way the sender is told
            # their message was not delivered (best effort).
            unconfirmed = isinstance(error, ipc_errors.SubmitOutcomeUnknownError)
            if unconfirmed:
                reason = "forward-outcome-unknown"
                detail = ipc_errors.SUBMIT_OUTCOME_UNKNOWN
            elif isinstance(error, TransientHarnessRetryExhausted):
                reason = "forward-retry-exhausted"
                detail = error.code
            else:
                reason = "forward-error"
                detail = (
                    error.code
                    if isinstance(error, DaemonRequestError)
                    else type(error).__name__
                )
            created = self._record_dead_letter(message, reason=reason, detail=detail)
            if created and not unconfirmed and not self._closing.is_set() and isinstance(
                error, (DaemonRequestError, TransientHarnessRetryExhausted)
            ):
                # The "not delivered" reply is scoped to daemon-forward
                # failures (F3's wire refusals).  State-store failures keep
                # the custody fence pinned by the local-command tests: when
                # pending state cannot be written, no platform send happens
                # at all -- an unpersisted reply must never be sent.
                self._notify_sender_of_dead_letter(
                    message, reason=reason, code=detail if detail != reason else None
                )
            return InboundOutcome(
                status="error",
                ack_error=type(error).__name__,
                new_dead_letter=created,
            )

    def _forward_inbound(
        self, message: LarkInboundMessage, *, suppress_guidance: bool
    ) -> InboundOutcome:
        frozen = self._state.pending_inbound_submission(message.message_id)
        if frozen is not None:
            return self._submit_inbound_request(message, frozen, recovering=True)
        pending = self._state.pending_command_response(message.message_id)
        if pending is not None:
            return self._send_pending_command(message, pending)
        slash = re.fullmatch(
            r"/hyprial[ \t]+ask[ \t]+(\S+)[ \t]+(.+)",
            message.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if slash is not None:
            token = slash.group(1)
            if not is_safe_runtime_target_field(token):
                reply = (
                    "Delivery failed\nCode: INVALID_TARGET\n"
                    "Runtime target must be valid printable UTF-8 under 4096 bytes."
                )
                return self._consume_command(
                    message,
                    reply,
                    status="command-error",
                    response_kind="ask-invalid",
                )
            matches = self._with_transient_retry(
                lambda: self._routes.resolve_runtime_target(token)
            )
            if len(matches) != 1:
                if not matches:
                    reply = (
                        "Delivery failed\nCode: TARGET_NOT_FOUND\n"
                        f"Runtime target: {token}\n"
                        "Run /hyprial agents and use an exact listed target URI."
                    )
                    response_kind = "ask-not-found"
                else:
                    reply = _bounded_listing(
                        "Delivery failed\nCode: AMBIGUOUS_TARGET\n"
                        f"Alias: {token}\nCandidates:",
                        tuple((item.actor_id, None) for item in matches),
                        total=len(matches),
                    )
                    response_kind = "ask-ambiguous"
                return self._consume_command(
                    message,
                    reply,
                    status="command-error",
                    response_kind=response_kind,
                )
            target = matches[0]
            if not all(
                is_safe_runtime_target_field(value)
                for value in (
                    target.actor_id,
                    target.actor_key,
                    target.display_name,
                )
            ):
                return self._consume_command(
                    message,
                    "Delivery failed\nCode: INVALID_TARGET_RESPONSE",
                    status="command-error",
                    response_kind="ask-invalid-response",
                )
            route_status = "slash-ask"
            # The adapter owns command parsing. Only the requested body may
            # cross the Harness boundary; command syntax is never prompt text.
            message = replace(message, text=slash.group(2))
        else:
            command = self._handle_command(message)
            if command is not None:
                return self._consume_command(
                    message,
                    command,
                    response_kind=_command_response_kind(message.text),
                )
            target, route_status = self._with_transient_retry(
                lambda: self._route(message)
            )
        if target is None:
            # "ignored" is a group message that never addressed us — not a
            # loss.  Anything a user plausibly expected us to handle keeps
            # its body in the dead-letter trail.
            created = False
            if route_status != "ignored":
                created = self._record_dead_letter(message, reason=route_status)
            guidance = (
                None
                if suppress_guidance or self._alarm is not None
                else self._route_guidance(message, route_status)
            )
            if guidance is not None:
                try:
                    self._lark.reply(
                        message.message_id,
                        guidance,
                        idempotency_key=(
                            f"{message.message_id}:route-guidance:{route_status}"
                        ),
                    )
                    self._state.record_seen(message.event_id)
                except (NameError, ImportError):
                    raise
                except Exception as error:  # noqa: BLE001 - platform/state boundary
                    return InboundOutcome(status="route-error", ack_error=str(error))
            return InboundOutcome(
                status=route_status,
                new_dead_letter=created,
            )

        text = self._fallback_quote_context(message, route_status)
        request = HarnessRequest(
            message_id=message.message_id,
            from_actor_id=self._channel_actor_id,
            to=target,
            text=text,
            conversation_id=message.conversation_id,
            provider_metadata={
                "provider": "lark",
                "eventId": message.event_id,
                "messageId": message.message_id,
                "chatId": message.chat_id,
                "chatType": message.chat_type,
                "rootId": message.root_id,
                "threadId": message.thread_id,
                "replyTo": message.reply_to,
                "senderId": message.sender_id,
                "senderType": message.sender_type,
                "sender": self._resolved_sender(message),
                "createTime": message.create_time,
                "messageType": message.message_type,
            },
        )
        frozen, created = self._state.record_pending_inbound_submission(
            message.message_id,
            message.event_id,
            json.dumps({
                "schema": "lark-inbound-submission/v1",
                "idempotencyKey": f"lark-inbound:{request.from_actor_id}:{request.message_id}",
                "request": asdict(request),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        return self._submit_inbound_request(message, frozen, recovering=not created)

    def _submit_inbound_request(
        self, message: LarkInboundMessage, frozen: str, *, recovering: bool = False,
        recovery_deadline: float | None = None,
    ) -> InboundOutcome:
        # Recover the complete original request, never a fresh pin/alias or
        # newly rendered quote context. Corrupt custody fails closed.
        try:
            envelope = json.loads(frozen)
            raw = envelope["request"]
            target = ActorTarget(**raw["to"])
            request = HarnessRequest(**{**raw, "to": target})
            if (
                envelope["schema"] != "lark-inbound-submission/v1"
                or request.message_id != message.message_id
                or request.from_actor_id != self._channel_actor_id
                or request.conversation_id != message.conversation_id
                or not isinstance(request.text, str)
                or not isinstance(request.provider_metadata, dict)
                or request.provider_metadata.get("messageId") != message.message_id
                or envelope["idempotencyKey"] != f"lark-inbound:{request.from_actor_id}:{request.message_id}"
                or not all(is_safe_runtime_target_field(value) for value in (
                    target.actor_id, target.actor_key, target.display_name
                ))
            ):
                raise ValueError("frozen submission identity mismatch")
        except (KeyError, TypeError, ValueError):
            raise ipc_errors.SubmitOutcomeUnknownError(
                "stored submission identity cannot be validated"
            ) from None
        try:
            receipt = self._with_transient_retry(
                lambda: self._harness.send_request(
                    request,
                    **({"timeout": max(0.0, recovery_deadline - time.monotonic())}
                       if recovery_deadline is not None else {}),
                ),
                retry_submission_unknown=True,
                prior_submission_unknown=recovering,
                recovery_deadline=recovery_deadline,
            )
        except (DaemonRequestError, HarnessSubmissionRejected) as error:
            if not isinstance(error, ipc_errors.SubmitOutcomeUnknownError):
                self._state.discard_pending_inbound_submission(message.message_id, frozen)
            raise
        if self._logger is not None:
            try:
                self._logger.info(
                    "adapter.inbound",
                    messageId=receipt.message_id,
                    correlationId=receipt.message_id,
                    node="adapter-inbound",
                    conversationId=message.conversation_id,
                    actorId=self._channel_actor_id,
                    sender=self._channel_actor_id,
                    target=target.actor_id,
                    platform="lark",
                    nativeMessageId=message.message_id,
                    nativeEventId=message.event_id,
                )
            except (NameError, ImportError):
                raise
            except Exception:
                # The daemon receipt is custody. Losing local visibility must
                # not cause the adapter to resubmit an already accepted request.
                pass
        try:
            self._state.record_inbound(
                RequestCorrelation(
                    harness_message_id=receipt.message_id,
                    message_id=message.message_id,
                    chat_id=message.chat_id,
                    conversation_id=message.conversation_id,
                ),
                event_id=message.event_id,
            )
        except (NameError, ImportError):
            raise
        except Exception as error:
            raise ipc_errors.SubmitOutcomeUnknownError(
                "daemon accepted the request but local settlement is incomplete",
                {"messageId": receipt.message_id, "daemonAccepted": True,
                 "localSettlementKnown": False, "causeType": type(error).__name__},
            ) from error
        if self._closing.is_set():
            return InboundOutcome(
                status="forwarded", harness_message_id=receipt.message_id,
                target_actor_id=target.actor_id, ack_error="adapter-closing",
            )
        ack_error = None
        if self._reaction_effects is not None:
            admission = self._reaction_effects.ack(message.message_id)
            if admission is not ReactionEffectAdmission.ACCEPTED:
                ack_error = f"reaction-effect-{admission.value}"
        else:
            try:
                with self._reaction_lock:
                    self._reaction_lark.add_reaction(message.message_id, ACK_EMOJI)
            except (NameError, ImportError):
                raise
            except Exception as error:  # noqa: BLE001 - platform boundary
                # ACK visibility cannot undo accepted Harness custody.
                ack_error = str(error)
        return InboundOutcome(
            status="forwarded",
            harness_message_id=receipt.message_id,
            target_actor_id=target.actor_id,
            ack_error=ack_error,
        )


    def _with_known_chat_type(self, message: LarkInboundMessage) -> LarkInboundMessage:
        if message.chat_type != "unknown":
            return message
        known = self._state.chat_type(message.chat_id)
        return replace(message, chat_type=known) if known else message


    def _is_bot_mentioned(self, message: LarkInboundMessage) -> bool:
        """True when THIS app's bot is @-mentioned, judged by identity key.

        Compares the mention open_ids against the app's own bot open_id --
        never display names, which are mutable and collide (the TS-era
        route-name matching silently dropped a production group mention for
        exactly that reason).  When the platform cannot answer the bot's
        identity the message counts as not mentioned: group traffic keeps
        its "ignored" default instead of being misrouted on a guess.
        """

        if not message.mention_open_ids:
            return False
        try:
            bot = self._lark.bot_open_id()
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - platform boundary
            return False
        return bot is not None and bot in message.mention_open_ids

    def _route(self, message: LarkInboundMessage) -> tuple[ActorTarget | None, str]:
        if message.reply_to:
            reply_route = self._state.reply(message.reply_to)
            if reply_route is not None:
                actor = self._routes.actor_by_key(reply_route.actor_key)
                if actor is None:
                    return None, "route-unavailable"
                return actor, "native-reply"

        # An @ of this bot goes to the adapter's pinned agent, whatever the
        # chat: the adapter binds one agent, so addressing the bot IS
        # addressing that agent.  Mentions of anyone else fall through --
        # in a group that means "ignored", the decided default for group
        # traffic that never addressed us.
        if self._is_bot_mentioned(message):
            actor = self._routes.pinned_actor(message.conversation_id)
            if actor is not None:
                return actor, "bot-mention"
            return None, "no-target"

        if message.chat_type == "unknown":
            # Recovery paths could not establish the chat type (the REST
            # models carry none and this chat never produced a live event).
            # Do not guess "p2p" (that would misroute group traffic to a
            # pin), but do not silently drop it either: dead-letter it so
            # the body stays recoverable once the chat type is known.
            return None, "no-target"
        if message.chat_type == "p2p":
            actor = self._routes.pinned_actor(message.conversation_id)
            if actor is not None:
                return actor, "pin-fallback" if message.reply_to else "pin"
            return None, "no-target"
        return None, "ignored"

    def _route_guidance(
        self, message: LarkInboundMessage, route_status: str
    ) -> str | None:
        pin_command = f"hyprial adapter pin {self._adapter_name} <agent>"
        if route_status == "route-unavailable":
            return (
                "The agent associated with this reply is offline or unavailable. "
                "Your message was not rerouted."
            )
        if route_status != "no-target":
            return None
        if message.reply_to:
            return (
                "The quoted message does not resolve to an addressable Harness "
                "agent, and this adapter has no pinned agent. An operator can "
                f"bind one on the daemon machine with: {pin_command}"
            )
        return (
            "No agent is bound to this adapter. An operator can bind one on "
            f"the daemon machine with: {pin_command}"
        )

    def _fallback_quote_context(
        self,
        message: LarkInboundMessage,
        route_status: str,
    ) -> str:
        # A native reply quotes one of the agent's own messages; without the
        # block the agent cannot tell which one was meant (2026-10-07).
        if route_status not in {"pin-fallback", "native-reply"} or not message.reply_to:
            return message.text
        try:
            quote = self._lark.get_message(message.reply_to)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform boundary
            del error
            return self._unresolved_quote(message)
        if quote is None:
            return self._unresolved_quote(message)
        return (
            f"{message.text}\n\n"
            f"[Quoted message {quote.message_id} "
            f"(from {quote.sender_id}, {quote.message_type})]\n"
            f"{quote.text}"
        )

    @staticmethod
    def _unresolved_quote(message: LarkInboundMessage) -> str:
        return (
            f"{message.text}\n\n"
            f"[Quoted message {message.reply_to} (unavailable)]\n"
            "Quoted context could not be resolved."
        )
