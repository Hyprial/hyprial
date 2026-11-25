# Decision record (file header; no inline comments by repository rule):
# - ACP JSON-RPC 2.0 server->client requests may use int or str ids (for example
#   "1" in session/request_permission); request_id_key keys pending tables by
#   (type(id), id) so int and str ids never collide or get coerced.
# - session/update progress observations map to the canonical Route C phases
#   (thinking, message-segment, tool-call, tool-result) while keeping thought
#   chunks out of the final assistant text buffer.
# - The -32601 constant is named ...UNKNOWN_REQUEST_CODE because daemon/impl
#   module-level *METHOD* names are treated as router method tables by
#   scripts/ci/check_method_registration.py; it is a JSON-RPC 2.0 error code.
# hyprial-unbounded-wait-allow: wait - the terminate/kill escalation bounds every process.wait with asyncio.wait_for deadlines
"""ACP stdio JSON-RPC 2.0 wire types, framing, and progress extraction."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass

from hyprial.daemon.impl.harnesses.protocol.common import summarize_stderr

from hyprial.daemon.impl.harnesses.streaming.protocol import ProgressObservation
from hyprial.daemon.impl.inbox.tracking.progress import (
    PHASE_MESSAGE_SEGMENT,
    PHASE_THINKING,
    PHASE_TOOL_CALL,
    PHASE_TOOL_RESULT,
    SUMMARY_LIMIT,
)
from hyprial.kernel import ipc_errors

ACP_PROTOCOL_VERSION = 1
ACP_AUTH_ERROR_CODE = -32000
ACP_JSONRPC_UNKNOWN_REQUEST_CODE = -32601
ACP_INVALID_PARAMS_CODE = -32602

ACP_REQUEST_TIMEOUT_SECONDS_DEFAULT = 30.0
ACP_CANCEL_TIMEOUT_SECONDS_DEFAULT = 5.0
ACP_CLOSE_STEP_SECONDS = 1.0
ACP_CLOSE_TOTAL_SECONDS = 1.2

STREAM_LIMIT_BYTES = 8 * 1024 * 1024
STDERR_TAIL_BYTES = 16 * 1024

JsonRpcId = int | str


class AgyStartPreconditionError(ValueError):
    """Deterministic agy configuration or auth refusal that retry cannot heal."""

    code = ipc_errors.HARNESS_START_FAILED
    permanent_start_failure = True
    start_precondition_failure = True


class AgyRpcError(RuntimeError):
    """Structured JSON-RPC 2.0 error returned by agy acp."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"agy ACP error ({code}): {message}")


class AgySessionNotFoundError(RuntimeError):
    """Strict resume refused because session/list did not contain session_ref."""


@dataclass(frozen=True, slots=True)
class AgyTurnOutcome:
    """Terminal result of one agy ACP session/prompt turn."""

    result: str
    is_error: bool = False
    stop_reason: str | None = None
    error_code: int | None = None
    failure_code: str | None = None
    provider_error: str | None = None
    usage: Mapping[str, object] | None = None


@dataclass(slots=True)
class ActiveTurnState:
    """Per-turn FIFO event queue and cancellation handle keyed by generation."""

    generation: int
    queue: asyncio.Queue[tuple[str, int, object]]
    text_chunks: list[str]
    prompt_future: asyncio.Future[object] | None = None
    cancel_future: asyncio.Future[None] | None = None
    cancel_sent: bool = False
    prompt_request_id: JsonRpcId | None = None


def decode_jsonrpc_line(raw_line: bytes) -> Mapping[str, object]:
    """Decode one JSONL frame or raise ConnectionError on malformed input."""

    try:
        decoded = json.loads(raw_line.rstrip(b"\r\n"))
    except json.JSONDecodeError as error:
        raise ConnectionError(f"agy ACP emitted invalid JSON: {error}") from error
    if not isinstance(decoded, Mapping):
        raise ConnectionError("agy ACP frame must be a JSON object")
    return decoded


def is_valid_jsonrpc_id(value: object) -> bool:
    """Return True for valid JSON-RPC 2.0 request/response ids (int or str, not bool)."""

    return type(value) is int or isinstance(value, str)


def request_id_key(request_id: JsonRpcId) -> tuple[type[int] | type[str], JsonRpcId]:
    """Type-preserving lookup key so int 1 and str '1' remain distinct."""

    return (int if type(request_id) is int else str, request_id)


def encode_jsonrpc_request(
    request_id: JsonRpcId,
    method: str,
    params: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a JSON-RPC 2.0 request frame."""

    frame: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        frame["params"] = dict(params)
    return frame


def encode_jsonrpc_notification(
    method: str,
    params: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a JSON-RPC 2.0 notification frame (explicitly omits id)."""

    frame: dict[str, object] = {
        "jsonrpc": "2.0",
        "method": method,
    }
    if params is not None:
        frame["params"] = dict(params)
    return frame


def encode_jsonrpc_response(request_id: JsonRpcId, result: object) -> dict[str, object]:
    """Build a JSON-RPC 2.0 success response preserving the original id type."""

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def encode_jsonrpc_error(
    request_id: JsonRpcId,
    code: int,
    message: str,
    data: object = None,
) -> dict[str, object]:
    """Build a JSON-RPC 2.0 error response preserving the original id type."""

    error_body: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error_body["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error_body,
    }


def classify_jsonrpc_message(message: Mapping[str, object]) -> str:
    """Classify an incoming JSON-RPC 2.0 object frame."""

    has_id = "id" in message and is_valid_jsonrpc_id(message.get("id"))
    has_method = isinstance(message.get("method"), str)
    if has_id and ("result" in message or "error" in message):
        return "response"
    if has_id and has_method:
        return "request"
    if "id" not in message and has_method:
        return "notification"
    return "unknown"


def _bounded_summary(text: object, *, fallback: str) -> str:
    cleaned = str(text).strip() if text is not None else ""
    if not cleaned:
        cleaned = fallback
    if len(cleaned) <= SUMMARY_LIMIT:
        return cleaned
    return f"{cleaned[: SUMMARY_LIMIT - 3]}..."


def extract_session_update_text(update: Mapping[str, object], expected_kind: str) -> str:
    """Extract text from a chunk update matching expected_kind."""

    if update.get("sessionUpdate") != expected_kind:
        return ""
    content = update.get("content")
    if not isinstance(content, Mapping) or content.get("type") != "text":
        return ""
    text = content.get("text")
    return text if isinstance(text, str) else ""


def extract_progress_observation(
    update: Mapping[str, object],
) -> ProgressObservation | None:
    """Translate an ACP session/update payload into a Route C ProgressObservation."""

    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        text = extract_session_update_text(update, "agent_message_chunk")
        if not text.strip():
            return None
        return ProgressObservation(
            phase=PHASE_MESSAGE_SEGMENT,
            summary=_bounded_summary(text, fallback="assistant response"),
        )
    if kind == "agent_thought_chunk":
        text = extract_session_update_text(update, "agent_thought_chunk")
        if not text.strip():
            return None
        return ProgressObservation(
            phase=PHASE_THINKING,
            summary=_bounded_summary(text, fallback="thinking"),
        )
    if kind in {"tool_call", "tool_call_update"}:
        tool_call_id = update.get("toolCallId")
        tool_name_raw = update.get("name")
        title_raw = update.get("title")
        status_raw = update.get("status")
        kind_raw = update.get("kind")
        tool_name = tool_name_raw if isinstance(tool_name_raw, str) and tool_name_raw else None
        title = title_raw if isinstance(title_raw, str) and title_raw else None
        status = status_raw if isinstance(status_raw, str) and status_raw else None
        phase = (
            PHASE_TOOL_RESULT
            if kind == "tool_call_update" and status in {"completed", "failed"}
            else PHASE_TOOL_CALL
        )
        detail: dict[str, object] = {}
        if status is not None:
            detail["status"] = status
        if isinstance(kind_raw, str) and kind_raw:
            detail["kind"] = kind_raw
        return ProgressObservation(
            phase=phase,
            summary=_bounded_summary(title or tool_name, fallback="tool call"),
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) and tool_call_id else None,
            tool_name=tool_name,
            detail=detail or None,
        )
    return None


def extract_known_session_ids(list_result: object) -> set[str]:
    """Extract non-empty sessionId values from an ACP session/list response."""

    known: set[str] = set()
    if not isinstance(list_result, Mapping):
        return known
    sessions = list_result.get("sessions")
    if not isinstance(sessions, list):
        return known
    for entry in sessions:
        if isinstance(entry, Mapping):
            sid = entry.get("sessionId")
            if isinstance(sid, str) and sid:
                known.add(sid)
    return known


def supports_session_resume(init_result: Mapping[str, object]) -> bool:
    """Return True if initialize capabilities allow session/resume."""

    caps = init_result.get("agentCapabilities")
    if not isinstance(caps, Mapping):
        return True
    session_caps = caps.get("sessionCapabilities")
    if not isinstance(session_caps, Mapping) or not session_caps:
        return True
    return "resume" in session_caps


def decode_rpc_error(err: Mapping[str, object]) -> AgyRpcError:
    """Convert a JSON-RPC error object into an AgyRpcError."""

    code = err.get("code")
    err_code = code if type(code) is int else -32603
    msg = str(err.get("message") or "agy ACP error")
    return AgyRpcError(err_code, msg, err.get("data"))


def build_rpc_error_outcome(exc: AgyRpcError) -> AgyTurnOutcome:
    """Map an AgyRpcError returned on session/prompt into AgyTurnOutcome."""

    if exc.code == ACP_AUTH_ERROR_CODE:
        return AgyTurnOutcome(
            result=f"authentication failed: {exc.message}",
            is_error=True,
            error_code=exc.code,
            provider_error=exc.message,
        )
    return AgyTurnOutcome(result=str(exc), is_error=True, error_code=exc.code)


def build_turn_outcome(result: object, text_chunks: list[str]) -> AgyTurnOutcome:
    """Map a session/prompt JSON-RPC result into an AgyTurnOutcome."""

    result_map = result if isinstance(result, Mapping) else {}
    stop_reason_raw = result_map.get("stopReason")
    stop_reason = stop_reason_raw if isinstance(stop_reason_raw, str) else None
    usage_raw = result_map.get("usage")
    usage = usage_raw if isinstance(usage_raw, Mapping) else None
    text = "".join(text_chunks)
    if stop_reason == "cancelled":
        return AgyTurnOutcome(
            result=text or "turn cancelled",
            is_error=True,
            stop_reason="cancelled",
            usage=usage,
        )
    return AgyTurnOutcome(
        result=text,
        is_error=False,
        stop_reason=stop_reason,
        usage=usage,
    )


def build_initialize_params(version: str) -> dict[str, object]:
    """Build ACP initialize request parameters."""

    return {
        "protocolVersion": ACP_PROTOCOL_VERSION,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {"name": "hyprial", "version": version},
    }


def build_session_params(
    *,
    cwd: str,
    mcp_servers: tuple[dict[str, object], ...],
    system_prompt_append: str | None,
) -> dict[str, object]:
    """Build common ACP session/new, session/resume, and session/load params."""

    params: dict[str, object] = {
        "cwd": cwd,
        "mcpServers": list(mcp_servers),
    }
    if system_prompt_append is not None:
        params["_meta"] = {
            "antigravity": {
                "systemPrompt": {"append": system_prompt_append}
            }
        }
    return params


async def drain_stderr_tail(
    process: asyncio.subprocess.Process,
    tail: bytearray,
    *,
    limit: int = STDERR_TAIL_BYTES,
) -> None:
    """Drain subprocess stderr into a bounded tail buffer."""

    if process.stderr is None:
        return
    while chunk := await process.stderr.read(4096):
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[: len(tail) - limit]


def format_exited_error(stderr_tail: bytearray) -> ConnectionError:
    """Format an unexpected ACP subprocess exit error with redacted stderr."""

    detail = summarize_stderr(bytes(stderr_tail))
    return ConnectionError("agy ACP session exited unexpectedly" + (f": {detail}" if detail else ""))


async def close_acp_subprocess(
    process: asyncio.subprocess.Process,
    tasks: tuple[asyncio.Task[None] | None, ...],
    deadline: float,
) -> None:
    """Close stdin, reap process with bounded terminate/kill escalation, and drain tasks."""

    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is None:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=max(0.05, min(ACP_CLOSE_STEP_SECONDS, deadline - time.monotonic())),
            )
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=max(0.05, min(ACP_CLOSE_STEP_SECONDS, deadline - time.monotonic())),
                )
            except TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.3)
                except TimeoutError:
                    pass
    for task in tasks:
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
            except TimeoutError:
                task.cancel()
