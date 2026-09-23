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
    """One failure notification, correlated to the original message.

    ``text`` is the human-authored body (routine escalation text, breaker
    reason).  When set it IS the notice; without it the renderer degrades to
    the generic "delivery failed" string and the actual cause is lost -- the
    2026-09-14 routine-alarm defect.
    """

    correlation_id: str
    message_id: str
    conversation_id: str
    sender: str
    recipient: str
    reason: str
    audience: AlarmAudience
    text: str | None = None


@dataclass(frozen=True, slots=True)
class AlarmResult:
    status: Literal["delivered", "failed", "throttled"]
    audience: AlarmAudience


#: Reasons that mean "this side stopped asking", never "the recipient said no".
#:
#: Both are minted by the sender's own give-up path -- ``inbox/actor.py``
#: calls that outcome ``FETCH_UNCONFIRMED``, which is precisely the right
#: word -- and the sender cannot tell a message that never arrived from one
#: that arrived and whose receipt was lost.  Measured on the production node
#: (2026-09-18, notes/delivery-failed-notice-is-unconfirmed-2026-09-18.md):
#: all 144 such rows carry no second status row of any kind, so nothing on
#: this side could ever decide it.
#:
#: Anything not listed here keeps the old "失败" wording: a reason that names
#: a refusal (TARGET_NOT_FOUND, PROVIDER_BLOCKED, ...) IS evidence about the
#: recipient.  ⚠️ A new give-up reason added elsewhere will default to the
#: wrong side of this line, which is why the acceptance test drives the real
#: retry-exhaustion path instead of only calling :meth:`AlarmEmitter.render`.
UNCONFIRMED_REASONS: frozenset[str] = frozenset(
    {"DELIVERY_RETRY_EXHAUSTED", "DELIVERY_EXPIRED"}
)


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
            # Two different receivers used to share the word "recipient": the
            # notice goes to alarm.sender, while alarm.recipient is the
            # original message's addressee.  Split them explicitly.
            "noticeRecipient": alarm.sender,
            "originalRecipient": alarm.recipient,
            "reason": alarm.reason,
            "audience": alarm.audience,
            **({"textLength": len(alarm.text)} if alarm.text is not None else {}),
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
        """The text the sender reads -- which must not outrun what we know.

        ``UNCONFIRMED_REASONS`` are the outcomes where nobody refused the
        message: the sender ran out of attempts, or the message reached its
        deadline.  Calling those 「失败」 asserts something about the
        RECIPIENT that this side has no evidence for, and the sender acts on
        it -- re-dispatching work that may already be running, or writing
        someone off as unreachable.
        """

        unconfirmed = alarm.reason in UNCONFIRMED_REASONS
        if alarm.text is not None:
            return alarm.text
        if alarm.audience == "human":
            if unconfirmed:
                return (
                    f"消息投递未确认：{alarm.reason}（消息 {alarm.message_id}）。"
                    "没有人拒收,是本端不再等回执了 —— 对方可能已经收到,"
                    "⛔ 不要当成没送到来处理。"
                )
            return f"消息投递失败：{alarm.reason}（消息 {alarm.message_id}）。"
        if alarm.audience == "operator":
            return alarm.reason
        if unconfirmed:
            return (
                f"System notice: delivery UNCONFIRMED for message "
                f"{alarm.message_id}; reason={alarm.reason}. Nothing refused "
                "it -- this side stopped waiting for a receipt, so the "
                "recipient may well have it."
            )
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
    "UNCONFIRMED_REASONS",
    "audience_for_sender",
]
