"""Shared dispatch escalation delivery, independent of workflow execution."""

from __future__ import annotations
from collections.abc import Callable
from uuid import uuid4
from hyprial.alarm import Alarm, AlarmEmitter, AlarmResult
from hyprial.inbox.io import InboxIoError
from hyprial.log import Logger


def _audience_for_recipient(recipient: str) -> str:
    return "human" if recipient.startswith("user:") else "agent"


class DispatchAlarm:
    def __init__(
        self,
        emitter: AlarmEmitter,
        deliver_user: Callable[[str, str, str], bool] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._emitter = emitter
        self._deliver_user = deliver_user
        self._logger = logger

    def escalate(
        self,
        *,
        to: str,
        text: str,
        reason: str | None = None,
        conversation_id: str = "workflow",
    ) -> AlarmResult:
        # The escalation text (routine name, breaker reason) is the payload --
        # it must survive to the reader verbatim.  The pre-2026-09-14 code
        # dropped it and rendered only "delivery failed ...
        # WORKFLOW_TARGET_TIMEOUT", which erased why the alarm existed.
        # ``conversation_id`` is the throttle ledger key's first component;
        # callers pass a per-routine / per-run id so one routine's alarm does
        # not eat another routine's (B3, 2026-09-14).  The returned status is
        # the completion code the routine/workflow caller records (S1).
        effective_reason = reason or "WORKFLOW_TARGET_TIMEOUT"
        if to.startswith("user:"):
            try:
                delivered = self._deliver_user is not None and self._deliver_user(
                    to, text, f"workflow-{uuid4().hex[:12]}"
                )
            except InboxIoError as error:
                # The DM callback raises transient (timeout) / permanent
                # failures as InboxIoError; the alarm path is best-effort and
                # must record loud rather than propagate into the routine loop.
                self._log_alarm_failed(
                    to,
                    text,
                    effective_reason,
                    conversation_id,
                    "user-delivery-timeout"
                    if not error.permanent
                    else "user-delivery-rejected",
                )
                return AlarmResult("failed", "human")
            if delivered:
                return AlarmResult("delivered", "human")
            # DM path failed or unwired: fail LOUD with the text preserved,
            # never degrade to a system notice keyed by an unreadable
            # recipient (user: has no notice reader).
            failure = (
                "user-delivery-unwired"
                if self._deliver_user is None
                else "user-delivery-rejected"
            )
            self._log_alarm_failed(to, text, effective_reason, conversation_id, failure)
            return AlarmResult("failed", "human")
        return self._emitter.emit(
            Alarm(
                correlation_id=f"workflow-{uuid4().hex[:12]}",
                message_id=f"workflow-{uuid4().hex[:12]}",
                conversation_id=conversation_id,
                sender=to,
                recipient="workflow",
                reason=effective_reason,
                audience=_audience_for_recipient(to),  # type: ignore[arg-type]
                text=text,
            ),
            delivery=None,
            terminal=False,
        )

    def _log_alarm_failed(
        self, to: str, text: str, reason: str, conversation_id: str, failure: str
    ) -> None:
        if self._logger is None:
            return
        try:
            self._logger.log(
                "error",
                "alarm.failed",
                **{
                    "correlationId": f"workflow-escalate-{uuid4().hex[:12]}",
                    "conversationId": conversation_id,
                    "sender": to,
                    "recipient": to,
                    "noticeRecipient": to,
                    "originalRecipient": to,
                    "reason": reason,
                    "audience": _audience_for_recipient(to),
                    "textLength": len(text),
                    "failure": failure,
                },
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - logging must never break escalation
            pass
