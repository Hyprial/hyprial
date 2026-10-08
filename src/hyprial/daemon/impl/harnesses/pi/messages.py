"""Pi RPC agent-event parsing and assistant-message extraction."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


from hyprial.daemon.impl.harnesses.streaming.protocol  import (
    ProgressObservation,
)

@dataclass(frozen=True, slots=True)
class _PiTurnOutcome:
    result: str
    is_error: bool = False
    failure_code: str | None = None
    #: Verbatim provider text when the failure came from the model backend
    #: itself (assistant ``stopReason=error``).  ``result`` carries the same
    #: text for the generic error path; this field keeps its provenance
    #: distinguishable so logs can name it (card 260).
    provider_error: str | None = None

def _bounded(text: object, *, fallback: str) -> str:
    value = str(text).strip() if text is not None else ""
    if not value:
        value = fallback
    return value if len(value) <= 200 else f"{value[:197]}..."

def _pi_progress_observation(message: dict[str, object]) -> ProgressObservation | None:
    """Translate pi's coarse lifecycle events; token/delta streams stay out.

    The v1 set is deliberately limited to event boundaries.  In particular
    ``message_update`` and ``tool_execution_update`` are route-B-style streams
    and are dropped here even though RPC exposes them.
    """

    kind = message.get("type")
    if kind == "agent_start":
        return ProgressObservation(phase="turn-start", summary="pi agent started")
    if kind == "turn_start":
        return ProgressObservation(phase="turn-start", summary="pi turn started")
    if kind == "turn_end":
        return ProgressObservation(phase="turn-end", summary="pi turn ended")
    if kind == "agent_settled":
        return ProgressObservation(
            phase="turn-end",
            summary="pi turn settled",
            terminal=True,
        )
    if kind in {"message_start", "message_end"}:
        action = "started" if kind == "message_start" else "completed"
        return ProgressObservation(
            phase="message-segment",
            summary=f"assistant message {action}",
        )
    if kind in {"tool_execution_start", "tool_execution_end"}:
        tool_name = _bounded(message.get("toolName"), fallback="tool")
        started = kind == "tool_execution_start"
        detail: dict[str, object] = {"event": kind}
        if not started and message.get("isError") is not None:
            detail["isError"] = message.get("isError") is True
        return ProgressObservation(
            phase="tool-call" if started else "tool-result",
            summary=(
                f"calling {tool_name}"
                if started
                else f"{tool_name} {'failed' if message.get('isError') is True else 'finished'}"
            ),
            tool_call_id=(
                message.get("toolCallId")
                if isinstance(message.get("toolCallId"), str)
                else None
            ),
            tool_name=tool_name,
            detail=detail,
        )
    if kind in {"compaction_start", "compaction_end"}:
        started = kind == "compaction_start"
        reason = message.get("reason")
        return ProgressObservation(
            phase="compaction",
            summary=(
                f"compaction {'started' if started else 'finished'}"
                + (f" ({reason})" if isinstance(reason, str) and reason else "")
            ),
            detail={
                "event": kind,
                **(
                    {"reason": reason}
                    if isinstance(reason, str) and reason
                    else {}
                ),
                **(
                    {"willRetry": True}
                    if not started and message.get("willRetry") is True
                    else {}
                ),
            },
        )
    if kind in {"auto_retry_start", "auto_retry_end"}:
        attempt = message.get("attempt")
        max_attempts = message.get("maxAttempts")
        suffix = (
            f" {attempt}/{max_attempts}"
            if isinstance(attempt, int) and isinstance(max_attempts, int)
            else ""
        )
        return ProgressObservation(
            phase="retry",
            summary=(
                f"auto-retry started{suffix}"
                if kind == "auto_retry_start"
                else f"auto-retry finished{suffix}"
            ),
            detail={
                "event": kind,
                **({"attempt": attempt} if isinstance(attempt, int) else {}),
                **(
                    {"maxAttempts": max_attempts}
                    if isinstance(max_attempts, int)
                    else {}
                ),
                **(
                    {"success": message.get("success") is True}
                    if kind == "auto_retry_end"
                    else {}
                ),
            },
        )
    if kind == "extension_error":
        event = message.get("event")
        return ProgressObservation(
            phase="retry",
            summary=(
                "pi extension error"
                + (f" in {event}" if isinstance(event, str) and event else "")
            ),
            detail={
                "event": event if isinstance(event, str) else "unknown",
            },
        )
    return None

def _assistant_provider_error(messages: list[object]) -> str | None:
    """The provider's own error text when the run ended in a backend error.

    The judgment is the LAST assistant message alone, in the same shape pi
    persists to its session file (``stopReason``/``errorMessage`` ride on
    the message object next to ``role``/``content``).  An error behind a
    ``willRetry`` agent_end never reaches here: those runs continue, and
    ``last_run_messages`` is replaced by the next agent_end's batch.

    Returns the ``errorMessage`` verbatim -- no truncation, no rewriting:
    the provider's words are the diagnosis, and the incident shape was
    exactly this text being thrown away while "no final assistant text"
    was reported instead.
    """

    for candidate in reversed(messages):
        if not isinstance(candidate, dict) or candidate.get("role") != "assistant":
            continue
        if candidate.get("stopReason") != "error":
            return None
        error_message = candidate.get("errorMessage")
        if isinstance(error_message, str) and error_message.strip():
            return error_message
        return None
    return None

def _last_assistant_message(messages: list[object]) -> dict[str, object] | None:
    for candidate in reversed(messages):
        if isinstance(candidate, dict) and candidate.get("role") == "assistant":
            return candidate
    return None

def _assistant_content_types(message: Mapping[str, object]) -> tuple[str, ...]:
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    return tuple(
        str(item.get("type", "unknown"))
        if isinstance(item, dict)
        else type(item).__name__
        for item in content
    )

def _usage_value(usage: object, key: str) -> object:
    if not isinstance(usage, dict):
        return "unknown"
    value = usage.get(key)
    return value if isinstance(value, (int, float)) else "unknown"

def _assistant_failure_detail(messages: list[object]) -> str:
    message = _last_assistant_message(messages)
    if message is None:
        return "stopReason=missing; contentTypes=[]; usage=missing"
    stop_reason = message.get("stopReason")
    rendered_stop = stop_reason if isinstance(stop_reason, str) else "missing"
    content_types = ",".join(_assistant_content_types(message))
    usage = message.get("usage")
    return (
        f"stopReason={rendered_stop}; contentTypes=[{content_types}]; "
        f"usage(input={_usage_value(usage, 'input')}, "
        f"output={_usage_value(usage, 'output')}, "
        f"cacheRead={_usage_value(usage, 'cacheRead')}, "
        f"cacheWrite={_usage_value(usage, 'cacheWrite')})"
    )

def _assistant_context_exhaustion(messages: list[object]) -> str | None:
    """Describe pi's deterministic no-output length stop, if present."""

    message = _last_assistant_message(messages)
    if message is None or message.get("stopReason") != "length":
        return None
    if _final_assistant_text([message]) is not None:
        return None
    content = message.get("content")
    usage = message.get("usage")
    empty_content = isinstance(content, list) and not content
    output_tokens = _usage_value(usage, "output")
    if output_tokens != 0 and not empty_content:
        return None
    return f"pi context exhausted: {_assistant_failure_detail([message])}"

def _final_assistant_text(messages: list[object]) -> str | None:
    """The reply is the last assistant message's text parts, joined.

    Mirrors the TS connector's ``finalAssistantText`` so both runtimes
    capture the same answer from the same message stream.
    """

    for candidate in reversed(messages):
        if not isinstance(candidate, dict) or candidate.get("role") != "assistant":
            continue
        content = candidate.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            item["text"]
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        text = "\n".join(parts)
        if text:
            return text
    return None
