"""Owner-local agent-bot branch for ``message.send`` user targets."""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.adapters.lark.outbound.gateway import (
    GatewayAdmissionRefused,
)
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.daemon.impl.application.messaging.inbox_surface.agent_bot_attempts import (
    ATTEMPT_ACCEPTED,
    ATTEMPT_DEFINITELY_NOT_SENT,
    LARK_DEDUPE_WINDOW_MS,
    AgentBotDeliverySupport,
    lark_pre_send_refusal_reason,
)
from hyprial.daemon.impl.identity import IdentityResolverError
from hyprial.kernel import DaemonRequestError, parse_agent_uri
from hyprial.daemon.impl.ipc.params import JsonObject


class AgentBotSendSupport(AgentBotDeliverySupport):
    """Try a fenced local agent's pinned bot before receiver delivery."""

    def _deliver_user_via_agent_bot(
        self,
        *,
        caller: Any,
        target: str,
        owner: str,
        text: str,
        target_key: str,
        message_id: str,
        conversation_id: str,
        trusted_origin: str | None,
    ) -> JsonObject | None:
        """Return one bot result, or ``None`` to keep the receiver path."""

        if owner != self.owner:
            return None
        agent, _reason = self._agent_bot_sender(
            caller, trusted_origin=trusted_origin
        )
        if agent is None:
            # Eligibility can change between attempts (no session on a
            # replay, re-registration, a delegated retry), and the key does
            # not carry the sender: a record from an earlier bot attempt still
            # decides, so the proxy never repeats what the bot may have sent.
            return self._agent_bot_replay_for_ineligible(
                caller=caller,
                target=target,
                owner=owner,
                target_key=target_key,
                message_id=message_id,
                conversation_id=conversation_id,
            )

        with self._agent_bot_attempts.key_lock(target_key):
            prior = self._agent_bot_attempts.get(target_key)
            if prior is not None and prior.state == ATTEMPT_ACCEPTED:
                if prior.delivery is None:
                    raise ValueError("accepted agent-bot attempt is missing delivery")
                self._log_agent_bot_attempt(
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=prior.adapter,
                    outcome="delivered",
                    reason="accepted-replay",
                    conversation_id=conversation_id,
                )
                return {**prior.delivery, "duplicate": True}
            if prior is not None and prior.state == ATTEMPT_DEFINITELY_NOT_SENT:
                self._log_definite_agent_bot_replay(
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=prior.adapter,
                    conversation_id=conversation_id,
                )
                return None

            locked = prior if prior is not None and prior.locks_fallback else None
            if locked is not None and (
                self._agent_bot_attempts.now_ms()
                - (
                    locked.first_send_at_ms
                    if locked.first_send_at_ms is not None
                    else locked.recorded_at_ms
                )
                >= LARK_DEDUPE_WINDOW_MS
            ):
                return self._agent_bot_outcome_unknown(
                    target=target,
                    target_key=target_key,
                    lark_idempotency_key=locked.lark_idempotency_key,
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=locked.adapter,
                    reason="lark-dedupe-window-expired",
                    conversation_id=conversation_id,
                    record_uncertain=False,
                )

            adapter, reason = self._agent_bot_adapter(
                agent,
                locked_adapter=locked.adapter if locked is not None else None,
            )
            if adapter is None:
                return self._agent_bot_pre_send_fallback(
                    caller=caller,
                    target=target,
                    owner=owner,
                    target_key=target_key,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    locked=locked,
                    adapter=None,
                    reason=reason,
                )

            try:
                open_id = self._identity_resolver.owner_open_id(owner, adapter)
            except IdentityResolverError as error:
                return self._agent_bot_pre_send_fallback(
                    caller=caller,
                    target=target,
                    owner=owner,
                    target_key=target_key,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    locked=locked,
                    adapter=adapter,
                    reason=f"identity-{error.code}",
                )
            if open_id is None:
                return self._agent_bot_pre_send_fallback(
                    caller=caller,
                    target=target,
                    owner=owner,
                    target_key=target_key,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    locked=locked,
                    adapter=adapter,
                    reason="open-id-unresolved",
                )

            gateway, unavailable = self._running_agent_bot_gateway(adapter)
            if gateway is None:
                return self._agent_bot_pre_send_fallback(
                    caller=caller,
                    target=target,
                    owner=owner,
                    target_key=target_key,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    locked=locked,
                    adapter=adapter,
                    reason=unavailable,
                )

            idempotency_key = (
                locked.lark_idempotency_key
                if locked is not None
                else f"{target_key}:agent-bot:{adapter}"
            )
            attempt, fresh = self._agent_bot_attempts.begin(
                target_key,
                adapter=adapter,
                lark_idempotency_key=idempotency_key,
                sender=caller.sender,
            )
            if attempt.state == ATTEMPT_ACCEPTED:
                if attempt.delivery is None:
                    raise ValueError("accepted agent-bot attempt is missing delivery")
                return {**attempt.delivery, "duplicate": True}
            if (
                attempt.adapter != adapter
                or attempt.lark_idempotency_key != idempotency_key
            ):
                return self._agent_bot_outcome_unknown(
                    target=target,
                    target_key=target_key,
                    lark_idempotency_key=attempt.lark_idempotency_key,
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=attempt.adapter,
                    reason="attempt-adapter-conflict",
                    conversation_id=conversation_id,
                    record_uncertain=False,
                )
            if not fresh and locked is None:
                return self._agent_bot_outcome_unknown(
                    target=target,
                    target_key=target_key,
                    lark_idempotency_key=attempt.lark_idempotency_key,
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=attempt.adapter,
                    reason="attempt-already-in-flight",
                    conversation_id=conversation_id,
                    record_uncertain=False,
                )

            try:
                native_message_id = gateway.send_owner_dm(
                    open_id,
                    text,
                    idempotency_key=idempotency_key,
                )
            except LarkApiError as error:
                refusal = lark_pre_send_refusal_reason(
                    error, adapter=adapter, open_id=open_id
                )
                if refusal is not None and locked is None:
                    return self._settle_fresh_agent_bot_fallback(
                        caller=caller,
                        target_key=target_key,
                        message_id=message_id,
                        owner=owner,
                        adapter=adapter,
                        idempotency_key=idempotency_key,
                        reason=refusal,
                        conversation_id=conversation_id,
                    )
                return self._agent_bot_outcome_unknown(
                    target=target,
                    target_key=target_key,
                    lark_idempotency_key=idempotency_key,
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=adapter,
                    reason=refusal or "lark-error-unknown",
                    conversation_id=conversation_id,
                    record_uncertain=locked is None,
                )
            except GatewayAdmissionRefused as error:
                reason = f"gateway-{error.reason}-before-send"
                if locked is None:
                    return self._settle_fresh_agent_bot_fallback(
                        caller=caller,
                        target_key=target_key,
                        message_id=message_id,
                        owner=owner,
                        adapter=adapter,
                        idempotency_key=idempotency_key,
                        reason=reason,
                        conversation_id=conversation_id,
                    )
                return self._agent_bot_outcome_unknown(
                    target=target,
                    target_key=target_key,
                    lark_idempotency_key=idempotency_key,
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=adapter,
                    reason=reason,
                    conversation_id=conversation_id,
                    record_uncertain=False,
                )
            except Exception:  # noqa: BLE001 - accepted boundary is uncertain
                return self._agent_bot_outcome_unknown(
                    target=target,
                    target_key=target_key,
                    lark_idempotency_key=idempotency_key,
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=adapter,
                    reason="transport-error",
                    conversation_id=conversation_id,
                    record_uncertain=locked is None,
                )

            return self._accepted_agent_bot_delivery(
                target=target,
                target_key=target_key,
                message_id=message_id,
                native_message_id=native_message_id,
                sender=caller.sender,
                owner=owner,
                adapter=adapter,
                lark_idempotency_key=idempotency_key,
                conversation_id=conversation_id,
            )

    def _agent_bot_pre_send_fallback(
        self,
        *,
        caller: Any,
        target: str,
        owner: str,
        target_key: str,
        message_id: str,
        conversation_id: str,
        locked: Any | None,
        adapter: str | None,
        reason: str,
    ) -> JsonObject | None:
        if locked is not None:
            return self._agent_bot_outcome_unknown(
                target=target,
                target_key=target_key,
                lark_idempotency_key=locked.lark_idempotency_key,
                message_id=message_id,
                sender=caller.sender,
                owner=owner,
                adapter=locked.adapter,
                reason=reason,
                conversation_id=conversation_id,
                record_uncertain=False,
            )
        self._record_agent_bot_fallback(
            target_key, adapter=adapter, reason=reason
        )
        self._log_agent_bot_attempt(
            message_id=message_id,
            sender=caller.sender,
            owner=owner,
            adapter=adapter,
            outcome="fallback",
            reason=reason,
            conversation_id=conversation_id,
        )
        return None

    def _settle_fresh_agent_bot_fallback(
        self,
        *,
        caller: Any,
        target_key: str,
        message_id: str,
        owner: str,
        adapter: str,
        idempotency_key: str,
        reason: str,
        conversation_id: str,
    ) -> None:
        self._record_agent_bot_fallback(
            target_key,
            adapter=adapter,
            reason=reason,
            lark_idempotency_key=idempotency_key,
        )
        self._log_agent_bot_attempt(
            message_id=message_id,
            sender=caller.sender,
            owner=owner,
            adapter=adapter,
            outcome="fallback",
            reason=reason,
            conversation_id=conversation_id,
        )
        return None

    def _agent_bot_replay_for_ineligible(
        self,
        *,
        caller: Any,
        target: str,
        owner: str,
        target_key: str,
        message_id: str,
        conversation_id: str,
    ) -> JsonObject | None:
        """Let an earlier bot attempt decide a replay from an ineligible caller.

        No record (the common case: system alarms, the operator) or a
        definitely-not-sent record keeps the receiver path.  An accepted
        record replays as a duplicate; an in-flight or uncertain one returns
        SUBMIT_OUTCOME_UNKNOWN.  Neither sends again: this caller may not use
        the bot, and the proxy must not repeat a message the bot may have
        delivered.  An unreadable ledger keeps the receiver path for these
        callers, so a broken bot ledger never silences system alarms.
        """

        try:
            with self._agent_bot_attempts.key_lock(target_key):
                prior = self._agent_bot_attempts.get(target_key)
        except DaemonRequestError:
            # Only an identity that can own no bot record may fall through: a
            # system service, or this daemon's dispatch service (its relays to
            # the owner).  Decided on the subject, so an operator send on
            # behalf of an agent counts as that agent.  Any other agent-shaped
            # identity may be replaying what its bot already sent, so it gets
            # the typed refusal rather than a possible second DM.
            subject = caller.subject
            if (
                parse_agent_uri(subject) is not None
                and subject != self._dispatch_service_actor
            ):
                raise
            return None
        if prior is None or prior.state == ATTEMPT_DEFINITELY_NOT_SENT:
            return None
        if prior.state == ATTEMPT_ACCEPTED:
            if prior.delivery is None:
                return self._agent_bot_outcome_unknown(
                    target=target,
                    target_key=target_key,
                    lark_idempotency_key=prior.lark_idempotency_key,
                    message_id=message_id,
                    sender=caller.sender,
                    owner=owner,
                    adapter=prior.adapter,
                    reason="accepted-record-missing-delivery",
                    conversation_id=conversation_id,
                    record_uncertain=False,
                )
            self._log_agent_bot_attempt(
                message_id=message_id,
                sender=caller.sender,
                owner=owner,
                adapter=prior.adapter,
                outcome="delivered",
                reason="accepted-replay-ineligible-caller",
                conversation_id=conversation_id,
            )
            return {**prior.delivery, "duplicate": True}
        return self._agent_bot_outcome_unknown(
            target=target,
            target_key=target_key,
            lark_idempotency_key=prior.lark_idempotency_key,
            message_id=message_id,
            sender=caller.sender,
            owner=owner,
            adapter=prior.adapter,
            reason="locked-replay-ineligible-caller",
            conversation_id=conversation_id,
            record_uncertain=False,
        )
