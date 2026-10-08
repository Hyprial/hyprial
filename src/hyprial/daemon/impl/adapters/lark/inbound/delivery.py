"""Lark event conversion, routing, acknowledgement, and receipt correlation."""

from __future__ import annotations

from datetime import UTC, datetime


from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    DeliveryOutcome,
    HarnessDelivery,
)
from hyprial.daemon.impl.adapters.lark.outbound.reactions import (
    ReactionEffectAdmission,
)
from hyprial.daemon.impl.adapters.lark.state.records import (
    ReplyRoute,
)
from hyprial.daemon.impl.adapters.lark.outbound.reactions import ACK_EMOJI
class _DeliveryAckMixin:
    def handle_delivery(
        self, delivery: HarnessDelivery, *, settle: bool = True
    ) -> DeliveryOutcome:
        """Send one correlated native reply.

        Normal adapter delivery uses ``settle=True`` and reports acceptance or
        rejection through the supplied Harness port.  The daemon reply bridge
        uses ``settle=False`` because its durable outbox owns settlement: the
        worker's positive control-socket response is the receipt that removes
        that row and writes the terminal record.  Calling back into the daemon
        while it waits for the control response would deadlock the serialized
        IPC transaction and would also settle the wrong inbox leg.
        """

        correlation = self._state.request(delivery.reply_to)
        if correlation is None:
            error = f"no Lark correlation for {delivery.reply_to}"
            if settle:
                self._harness.reject_delivery(
                    delivery.delivery_id, error, deterministic=True
                )
            return DeliveryOutcome(status="rejected", error=error)

        try:
            native_message_id = self._lark.reply(
                correlation.message_id,
                self._render_reply(delivery, native_reply_to=correlation.message_id),
                idempotency_key=(
                    f"{correlation.message_id}:delivery:{delivery.message_id}"
                ),
            )
            self._state.record_reply(
                ReplyRoute(
                    message_id=native_message_id,
                    actor_id=delivery.from_actor.actor_id,
                    actor_key=delivery.from_actor.actor_key,
                    harness_message_id=delivery.reply_to,
                    conversation_id=correlation.conversation_id,
                    chat_id=correlation.chat_id,
                )
            )
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform/state boundary
            if settle:
                self._harness.reject_delivery(
                    delivery.delivery_id, str(error), deterministic=False
                )
            return DeliveryOutcome(status="rejected", error=str(error))

        if settle:
            self._harness.accept_delivery(delivery.delivery_id, native_message_id)
        cleanup_error = (
            self._clear_ack_reaction(correlation.message_id) if settle else None
        )
        return DeliveryOutcome(
            status="accepted",
            native_message_id=native_message_id,
            cleanup_error=cleanup_error,
        )

    def clear_delivery_reaction(self, reply_to: str) -> str | None:
        """Best-effort cleanup after the reply-bridge receipt is emitted."""

        correlation = self._state.request(reply_to)
        if correlation is None:
            return f"no Lark correlation for {reply_to}"
        return self._clear_ack_reaction(correlation.message_id)

    def persist_delivery_reaction(self, reply_to: str) -> str | None:
        """Durably queue reply-wins cleanup before publishing its receipt."""

        if self._reaction_effects is None:
            return "reaction-effect-ledger-unavailable"
        correlation = self._state.request(reply_to)
        if correlation is None:
            return f"no Lark correlation for {reply_to}"
        admission = self._reaction_effects.persist_reply(correlation.message_id)
        return (
            None
            if admission is ReactionEffectAdmission.ACCEPTED
            else f"reaction-effect-{admission.value}"
        )

    def _clear_ack_reaction(self, native_message_id: str) -> str | None:
        if self._reaction_effects is not None:
            admission = self._reaction_effects.reply(native_message_id)
            return (
                None
                if admission is ReactionEffectAdmission.ACCEPTED
                else f"reaction-effect-{admission.value}"
            )
        try:
            with self._reaction_lock:
                self._reaction_lark.clear_reaction(native_message_id, ACK_EMOJI)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform boundary
            # Delivery is already accepted; never redeliver for cleanup.
            return str(error)
        return None

    def handle_alarm_delivery(
        self, correlation_id: str, text: str, *, idempotency_key: str
    ) -> DeliveryOutcome:
        """Reply to the original Lark message without creating a reply leg."""

        correlation = self._state.request(correlation_id)
        if correlation is None:
            return DeliveryOutcome(
                status="rejected",
                error=f"no Lark correlation for {correlation_id}",
            )
        try:
            native_message_id = self._lark.reply(
                correlation.message_id,
                text,
                idempotency_key=idempotency_key,
            )
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform boundary
            return DeliveryOutcome(status="rejected", error=str(error))
        return DeliveryOutcome(
            status="accepted", native_message_id=native_message_id
        )

    @staticmethod
    def _render_reply(delivery: HarnessDelivery, *, native_reply_to: str) -> str:
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        return (
            f"Agent: {delivery.from_actor.display_name}\n"
            f"Replied-At: {timestamp}\n"
            f"Reply-To: {native_reply_to}\n\n"
            f"{delivery.text}"
        )
