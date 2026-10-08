from __future__ import annotations
from hyprial.daemon import (
    ALARM_THROTTLE_WINDOW_MS,
    Alarm,
    AlarmResult,
    audience_for_sender,
)
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    EmitAlarmCommand,
)
from hyprial.daemon.impl.inbox.service.delivery.notices  import _ExpiredSenderNotice

from ..events import (
    DispatchIoKind,
    DispatchIoRequested,
    DispatchItem,
)

class DeliveryCustodyNoticesMixin:
    def _emit_explicit_alarm(self, command: EmitAlarmCommand) -> None:
        alarm = command.alarm
        fields = self._alarm_fields(alarm)
        if command.terminal:
            self._service._alarm._safe_log(
                "error", "terminal.failure", **fields
            )
        now_ms = self._service._now_ms()
        window_start_ms = (
            now_ms // ALARM_THROTTLE_WINDOW_MS
        ) * ALARM_THROTTLE_WINDOW_MS
        if command.throttle:
            claimed = self._service._claim_alarm(
                alarm.conversation_id,
                alarm.reason,
                window_start_ms,
                now_ms,
            )
            if not claimed:
                self._service._alarm._safe_log(
                    "info", "alarm.raised", **fields, throttled=True
                )
                self._publish_alarm_completed(
                    command.correlation_id,
                    AlarmResult("throttled", alarm.audience),
                )
                return
        self._service._alarm._safe_log("warn", "alarm.raised", **fields)
        request = DispatchIoRequested(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.ALARM,
            alarm=alarm,
            alarm_terminal=command.terminal,
            alarm_claimed=command.throttle,
        )
        self._request_io(request, completion_kind="explicit_alarm")
    @staticmethod
    def _alarm_fields(alarm: Alarm) -> dict[str, object]:
        return {
            "correlationId": alarm.correlation_id,
            "originalMessageId": alarm.message_id,
            "conversationId": alarm.conversation_id,
            "sender": alarm.sender,
            "recipient": alarm.recipient,
            # Split the two receivers that used to share "recipient": the
            # notice goes to alarm.sender, while alarm.recipient is the
            # original message's addressee (2026-09-14 spec item 5c; the
            # actor path must carry the same fields as AlarmEmitter).
            "noticeRecipient": alarm.sender,
            "originalRecipient": alarm.recipient,
            "reason": alarm.reason,
            "audience": alarm.audience,
        }
    def _request_alarm(self, message: InboxMessage, reason: str) -> None:
        now_ms = self._service._now_ms()
        window_start_ms = (
            now_ms // ALARM_THROTTLE_WINDOW_MS
        ) * ALARM_THROTTLE_WINDOW_MS
        if not self._service._claim_alarm(
            message.conversation_id,
            reason,
            window_start_ms,
            now_ms,
        ):
            return
        correlation_id = f"alarm:{message.message_id}:{reason}:{self._version}"
        request = DispatchIoRequested(
            correlation_id=correlation_id,
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.ALARM,
            alarm=Alarm(
                correlation_id=message.message_id,
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                sender=message.sender,
                recipient=message.recipient,
                reason=str(reason),
                audience=audience_for_sender(message.sender),
            ),
            alarm_claimed=True,
        )
        self._request_io(request, completion_kind="alarm")
    def _stage_expired_sender_notices(
        self,
        prune_correlation_id: str,
        notices: tuple[_ExpiredSenderNotice, ...],
    ) -> None:
        remote: list[DispatchItem] = []
        for item in notices:
            if item.target_node is None:
                self._service._log_expired_sender_notice(
                    item.original_message_id,
                    None,
                    False,
                )
                continue
            if item.target_node == self._service.node_id:
                try:
                    delivered = self._service.receive_system_notice(item.notice)
                except Exception:  # noqa: BLE001 - the notice is best-effort
                    delivered = False
                self._service._log_expired_sender_notice(
                    item.original_message_id,
                    item.target_node,
                    delivered,
                )
                continue
            remote.append(
                DispatchItem(
                    message=item.notice,
                    target_node=item.target_node,
                    source_message_id=item.original_message_id,
                )
            )
        if not remote:
            return
        request = DispatchIoRequested(
            correlation_id=f"{prune_correlation_id}:expiry-notice",
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.NOTICE,
            items=tuple(remote),
        )
        self._request_io(request, completion_kind="expiry_notice")
