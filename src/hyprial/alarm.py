"""Synchronous message-plane alarms for terminal delivery failures.

The emitter records the trajectory events through :class:`hyprial.log.Logger`,
but delivery remains an explicit message-plane callback.  Logger is therefore
still a pure sink: writing an error log can never cause a bounce.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from hyprial.log import Logger

ALARM_THROTTLE_WINDOW_MS = 60_000

AlarmAudience = Literal["human", "agent", "operator"]
AlarmDelivery = Callable[["Alarm", str], bool]
AlarmClaim = Callable[[str, str, int, int], bool]


@dataclass(frozen=True, slots=True)
class Alarm:
    """One failure notification, correlated to the original message."""

    correlation_id: str
    message_id: str
    conversation_id: str
    sender: str
    recipient: str
    reason: str
    audience: AlarmAudience


@dataclass(frozen=True, slots=True)
class AlarmResult:
    status: Literal["delivered", "failed", "throttled"]
    audience: AlarmAudience


class AlarmEmitter:
    """Log and synchronously deliver one non-recursive failure alarm."""

    def __init__(
        self,
        logger: Logger,
        *,
        deliver_human: AlarmDelivery | None = None,
        deliver_agent: AlarmDelivery | None = None,
        claim: AlarmClaim | None = None,
        clock_ms: Callable[[], int] | None = None,
        throttle_window_ms: int = ALARM_THROTTLE_WINDOW_MS,
    ) -> None:
        if throttle_window_ms < 1:
            raise ValueError("alarm throttle window must be positive")
        self._logger = logger
        self._deliver_human = deliver_human
        self._deliver_agent = deliver_agent
        self._claim = claim
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._throttle_window_ms = throttle_window_ms

    def emit(
        self,
        alarm: Alarm,
        *,
        delivery: AlarmDelivery | None = None,
        terminal: bool = True,
        throttle: bool = True,
    ) -> AlarmResult:
        """Emit trajectory events and attempt one delivery.

        Delivery exceptions stop here and become ``alarm.failed``.  They never
        call :meth:`emit` again, which is the bounce-storm recursion fence.
        """

        fields = {
            "correlationId": alarm.correlation_id,
            "originalMessageId": alarm.message_id,
            "conversationId": alarm.conversation_id,
            "sender": alarm.sender,
            "recipient": alarm.recipient,
            "reason": alarm.reason,
            "audience": alarm.audience,
        }
        if terminal:
            self._safe_log("error", "terminal.failure", **fields)

        now_ms = self._clock_ms()
        window_start_ms = (
            now_ms // self._throttle_window_ms
        ) * self._throttle_window_ms
        if throttle and self._claim is not None and not self._claim(
            alarm.conversation_id, alarm.reason, window_start_ms, now_ms
        ):
            self._safe_log("info", "alarm.raised", **fields, throttled=True)
            return AlarmResult("throttled", alarm.audience)
        self._safe_log("warn", "alarm.raised", **fields)

        selected = delivery
        if selected is None:
            selected = (
                self._deliver_human
                if alarm.audience == "human"
                else self._deliver_agent
            )
        text = self.render(alarm)
        try:
            delivered = selected is not None and selected(alarm, text)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - alarm delivery boundary
            self._failed(
                fields,
                type(error).__name__,
            )
            return AlarmResult("failed", alarm.audience)
        if not delivered:
            self._failed(
                fields,
                "delivery-rejected",
            )
            return AlarmResult("failed", alarm.audience)
        self._safe_log("info", "alarm.delivered", **fields)
        return AlarmResult("delivered", alarm.audience)

    @staticmethod
    def render(alarm: Alarm) -> str:
        if alarm.audience == "human":
            return f"消息投递失败：{alarm.reason}（消息 {alarm.message_id}）。"
        if alarm.audience == "operator":
            return alarm.reason
        return (
            f"System notice: delivery failed for message {alarm.message_id}; "
            f"reason={alarm.reason}."
        )

    def _failed(
        self,
        fields: dict[str, object],
        failure: str,
    ) -> None:
        self._safe_log("error", "alarm.failed", **fields, failure=failure)

    def _safe_log(self, level: str, event: str, **fields: object) -> None:
        try:
            self._logger.log(level, event, **fields)  # type: ignore[arg-type]
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - logging cannot suppress the bounce
            pass


def audience_for_sender(sender: str) -> Literal["human", "agent"]:
    """A Lark reply-bridge sender is a forwarded human message; else agent.

    Recognizes both the legacy ``channel:lark:`` spelling (historical data)
    and the current ``adapter:lark:`` spelling (every new mint) via the
    single reply-bridge parser -- deferred import because ``hyprial.adapters.
    lark``'s package ``__init__`` pulls in ``adapter.py``, which imports
    ``hyprial.alarm``; importing at module level here would cycle back through
    that package init while this module is still loading.
    """

    from hyprial.adapters.lark.reply_bridge import lark_reply_adapter

    return "human" if lark_reply_adapter(sender) is not None else "agent"


__all__ = [
    "ALARM_THROTTLE_WINDOW_MS",
    "Alarm",
    "AlarmEmitter",
    "AlarmResult",
    "audience_for_sender",
]
