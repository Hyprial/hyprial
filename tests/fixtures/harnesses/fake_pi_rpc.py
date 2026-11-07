"""A fake ``pi --mode rpc`` process for exercising the PiRpcClient wire.

Speaks the documented JSONL protocol on stdin/stdout.  ``FAKE_PI_MODE``
selects the behaviour under test:

- ``settled``     – prompt completes with agent_end + agent_settled
- ``no-settled``  – like ``settled`` but never emits agent_settled
- ``reject``      – prompt is answered with success=false
- ``ui-dialog``   – a blocking extension UI confirm precedes the answer
- ``abort``       – the turn hangs until an abort command arrives
- ``hang``        – accepts the prompt, then never finishes (even on abort)
- ``crash``       – first process accepts the prompt then dies; later
                    processes complete normally (``FAKE_PI_STATE`` marker)
- ``exit-after-turn`` – completes one turn, then exits with status 23 while idle
- ``no-text``     – agent_end carries no assistant text; the answer is only
                    available through get_last_assistant_text
- ``provider-error`` – agent_end's last assistant message carries the crh
                    incident shape verbatim (stopReason:"error",
                    errorMessage:"fetch failed", empty content, zero usage)
- ``provider-auth-error`` – the f2lt/kimi incident shape verbatim (OAuth
                    refresh unauthorized, status 400)
- ``empty``       – a truly empty reply: no error stop, no text, and
                    get_last_assistant_text also answers empty
- ``context-exhausted-no-compact`` – a length stop with no output; the
                    compact RPC command is unavailable
- ``context-exhausted-recovers`` – a length stop, then compact succeeds and
                    the retried prompt produces text
- ``context-exhausted-stuck`` – compact succeeds, but the single retried
                    prompt is length-stopped too
- ``length-with-text`` – an ordinary length stop that still carries text
                    (a truncated but real answer); must complete normally
- ``reject-session`` – exits immediately when launched with the session id in
                    ``FAKE_PI_DEAD_ID`` (a session file pi cannot open);
                    any other id behaves as ``settled``

``FAKE_PI_LOG`` appends each received command as one JSON line, preserving
order for stop/interrupt race assertions.
``FAKE_PI_ARGV_LOG`` appends each process's argv as one JSON line, so tests
can assert which session id every (re)launch resumed.
``FAKE_PI_VALIDATE_SESSION_ID=1`` mirrors pi >=0.76's launch-time session id
charset gate ([A-Za-z0-9._-]).  Both switches are independent and default off.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def session_id_argument() -> str | None:
    argv = sys.argv[1:]
    for index, value in enumerate(argv):
        if value == "--session-id" and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith("--session-id="):
            return value.split("=", 1)[1]
    return None


MODE = os.environ.get("FAKE_PI_MODE", "settled")
LOG_PATH = os.environ.get("FAKE_PI_LOG")
FRAME_LOG = os.environ.get("FAKE_PI_FRAME_LOG")


def record_frame(command: dict[str, object]) -> None:
    """Append the FULL received frame, for exact-wire steer tests."""

    if FRAME_LOG is None:
        return
    with open(FRAME_LOG, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(command, separators=(",", ":")) + "\n")


def maybe_steer(command: dict[str, object]) -> bool:
    """Answer a steer frame per FAKE_PI_STEER_MODE; True when handled."""

    if command.get("type") != "steer":
        return False
    steer_mode = os.environ.get("FAKE_PI_STEER_MODE", "queued")
    if steer_mode == "silent":
        return True
    if steer_mode == "reject":
        emit(
            {
                "type": "response",
                "command": "steer",
                "id": command.get("id"),
                "success": False,
                "error": "no active run to steer",
            }
        )
        return True
    emit(
        {
            "type": "response",
            "command": "steer",
            "id": command.get("id"),
            "success": True,
            "data": {"disposition": steer_mode},
        }
    )
    return True


def record(value: dict[str, object]) -> None:
    if LOG_PATH is None:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def emit(value: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def read_command() -> dict[str, object] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    command = json.loads(line)
    record_frame(command)
    record({"event": "command", "type": command.get("type"), "id": command.get("id")})
    return command


def assistant(text: str) -> dict[str, object]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def main() -> None:
    mode = os.environ.get("FAKE_PI_MODE", "settled")
    state_path = os.environ.get("FAKE_PI_STATE", "")
    compacted = False
    # Log argv first so even launches the gates below reject are recorded.
    argv_log = os.environ.get("FAKE_PI_ARGV_LOG")
    if argv_log:
        with open(argv_log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(sys.argv[1:]) + "\n")
    if os.environ.get("FAKE_PI_VALIDATE_SESSION_ID") == "1":
        # Pi >=0.76 rejects a --session-id with any character outside
        # [A-Za-z0-9._-] at launch; mirror that gate so the harness can
        # prove its sanitized value passes (test_pi_rpc_process.py).
        value = session_id_argument()
        if value is not None:
            allowed = (
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "abcdefghijklmnopqrstuvwxyz0123456789._-"
            )
            if any(char not in allowed for char in value):
                print(
                    f"fake pi rejects --session-id {value!r}: "
                    "characters outside [A-Za-z0-9._-]",
                    file=sys.stderr,
                    flush=True,
                )
                sys.exit(42)
    if mode == "reject-session" and session_id_argument() == os.environ.get(
        "FAKE_PI_DEAD_ID"
    ):
        print("pi: cannot open session file", file=sys.stderr, flush=True)
        sys.exit(1)
    while True:
        command = read_command()
        if command is None:
            return
        kind = command.get("type")
        if kind == "get_state":
            if mode == "startup-hang":
                continue
            emit(
                {
                    "type": "response",
                    "command": "get_state",
                    "id": command.get("id"),
                    "success": True,
                    "data": json.loads(os.environ.get("FAKE_PI_NATIVE_STATE", "null")) or {
                        "isStreaming": False, "isCompacting": False,
                        "pendingMessageCount": 0, "sessionId": "fake",
                    },
                }
            )
            continue
        if kind == "abort":
            emit({"type": "response", "command": "abort", "success": True})
            continue
        if maybe_steer(command):
            continue
        if kind == "compact":
            if mode == "context-exhausted-no-compact":
                emit(
                    {
                        "type": "response",
                        "command": "compact",
                        "id": command.get("id"),
                        "success": False,
                        "error": "Unknown command: compact",
                    }
                )
                continue
            compacted = True
            emit({"type": "compaction_start", "reason": "manual"})
            # A real compaction summarises the whole context through the model.
            time.sleep(float(os.environ.get("FAKE_PI_COMPACT_DELAY", "0")))
            emit(
                {
                    "type": "response",
                    "command": "compact",
                    "id": command.get("id"),
                    "success": True,
                    "data": {"tokensBefore": 998640},
                }
            )
            emit({"type": "compaction_end", "reason": "manual"})
            continue
        if kind == "get_last_assistant_text":
            # The incident shapes have no assistant text anywhere: the last
            # assistant message IS the error one, with empty content, so the
            # real pi answers empty here too.
            text = (
                ""
                if mode
                in {"empty", "provider-error", "provider-auth-error"}
                else "recovered from session"
            )
            if mode == "no-text-whitespace":
                text = "  \n\t"
            emit(
                {
                    "type": "response",
                    "command": "get_last_assistant_text",
                    "id": command.get("id"),
                    "success": True,
                    "data": {"text": text},
                }
            )
            continue
        if kind != "prompt":
            continue
        prompt_id = command.get("id")
        prompt = str(command.get("message", ""))
        if mode == "delayed-settlement":
            emit({"type": "agent_end", "messages": [
                {"role": "assistant", "content": [], "stopReason": "stop"},
            ], "willRetry": False})
            Path(os.environ["FAKE_PI_END_OBSERVED"]).touch()
            while not Path(os.environ["FAKE_PI_CONTINUE"]).exists():
                time.sleep(0.01)
            emit({"type": "agent_start"})
            emit({"type": "agent_end", "messages": [assistant(f"{prompt} done")]})
            emit({"type": "agent_settled"})
            continue
        if mode == "reject":
            emit(
                {
                    "type": "response",
                    "command": "prompt",
                    "id": prompt_id,
                    "success": False,
                    "error": "pi session is busy",
                }
            )
            continue
        emit(
            {"type": "response", "command": "prompt", "id": prompt_id, "success": True}
        )
        emit({"type": "agent_start"})
        if mode == "progress":
            emit({"type": "turn_start"})
            emit({"type": "message_start", "message": {"role": "assistant"}})
            emit(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "args": {"path": "README.md"},
                }
            )
            # Explicitly out of route-C scope: the connector must ignore the
            # high-frequency update stream rather than sliding into deltas.
            emit(
                {
                    "type": "tool_execution_update",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "partialResult": {"content": [{"type": "text", "text": "..."}]},
                }
            )
            emit(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "result": {"content": [{"type": "text", "text": "ok"}]},
                    "isError": False,
                }
            )
            emit({"type": "compaction_start", "reason": "threshold"})
            emit({"type": "compaction_end", "reason": "threshold", "willRetry": False})
            emit(
                {
                    "type": "auto_retry_start",
                    "attempt": 1,
                    "maxAttempts": 3,
                    "delayMs": 1,
                    "errorMessage": "transient",
                }
            )
            emit({"type": "auto_retry_end", "success": True, "attempt": 1})
            emit({"type": "message_end", "message": assistant("segment")})
            emit({"type": "turn_end", "message": assistant("segment")})
            emit(
                {
                    "type": "agent_end",
                    "messages": [assistant(f"{prompt} done")],
                    "willRetry": False,
                }
            )
            emit({"type": "agent_settled"})
            continue
        if mode == "crash" and state_path and not os.path.exists(state_path):
            with open(state_path, "w", encoding="utf-8") as marker:
                marker.write("crashed once")
            sys.exit(1)
        if mode == "hang":
            # A wedged child that ignores even abort; force-stop owns cleanup.
            emit({"type": "agent_start"})
            while True:
                pending = read_command()
                if pending is None:
                    return
                if pending.get("type") == "abort":
                    break
                maybe_steer(pending)
            return
        if mode == "ui-dialog":
            emit(
                {
                    "type": "extension_ui_request",
                    "id": "ui-1",
                    "method": "confirm",
                    "title": "Trust project files?",
                }
            )
            answer = read_command()
            if answer is None:
                return
            cancelled = answer.get("cancelled") is True
            emit(
                {
                    "type": "agent_end",
                    "messages": [
                        assistant(f"dialog cancelled={str(cancelled).lower()}")
                    ],
                    "willRetry": False,
                }
            )
            emit({"type": "agent_settled"})
            continue
        if mode == "abort":
            while True:
                pending = read_command()
                if pending is None:
                    return
                if pending.get("type") == "abort":
                    break
            emit({"type": "response", "command": "abort", "success": True})
            emit({"type": "agent_end", "messages": [], "willRetry": False})
            emit({"type": "agent_settled"})
            continue
        if mode == "provider-error" or mode == "provider-auth-error":
            # Incident shapes, verbatim from the two pi session files (card
            # 260): an assistant message whose content is empty, usage all
            # zero, and stopReason="error" carrying the provider's own text.
            error_message = (
                "OAuth refresh failed for kimi-coding: Kimi Code token "
                "refresh unauthorized (status 400): The provided "
                "authorization grant is invalid"
                if mode == "provider-auth-error"
                else "fetch failed"
            )
            emit(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [],
                            "api": "anthropic-messages",
                            "provider": (
                                "kimi-coding"
                                if mode == "provider-auth-error"
                                else "openai-codex"
                            ),
                            "model": (
                                "k3"
                                if mode == "provider-auth-error"
                                else "gpt-5.6-terra"
                            ),
                            "usage": {
                                "input": 0,
                                "output": 0,
                                "cacheRead": 0,
                                "cacheWrite": 0,
                                "totalTokens": 0,
                                "cost": {
                                    "input": 0,
                                    "output": 0,
                                    "cacheRead": 0,
                                    "cacheWrite": 0,
                                    "total": 0,
                                },
                            },
                            "stopReason": "error",
                            "errorMessage": error_message,
                        }
                    ],
                    "willRetry": False,
                }
            )
            emit({"type": "agent_settled"})
            continue
        if mode == "empty":
            emit(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [{"type": "thinking", "thinking": ""}],
                            "stopReason": "stop",
                            "usage": {
                                "input": 12,
                                "output": 0,
                                "cacheRead": 34,
                                "cacheWrite": 5,
                                "totalTokens": 51,
                            },
                        }
                    ],
                    "willRetry": False,
                }
            )
            emit({"type": "agent_settled"})
            continue
        if mode == "length-with-text":
            emit(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": f"{prompt} partial"}],
                            "stopReason": "length",
                            "usage": {
                                "input": 812,
                                "output": 4096,
                                "cacheRead": 997828,
                                "cacheWrite": 0,
                                "totalTokens": 1002736,
                            },
                        }
                    ],
                    "willRetry": False,
                }
            )
            emit({"type": "agent_settled"})
            continue
        if mode.startswith("context-exhausted") and not (
            mode == "context-exhausted-recovers" and compacted
        ):
            emit(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "length",
                            "usage": {
                                "input": 812,
                                "output": 0,
                                "cacheRead": 997828,
                                "cacheWrite": 0,
                                "totalTokens": 998640,
                            },
                        }
                    ],
                    "willRetry": False,
                }
            )
            emit({"type": "agent_settled"})
            continue
        if mode in {"empty-last", "whitespace-last"}:
            last_content = (
                [] if mode == "empty-last"
                else [{"type": "text", "text": "  \n\t"}]
            )
            emit({
                "type": "agent_end",
                "messages": [
                    assistant("earlier partial"),
                    {"role": "assistant", "content": last_content, "stopReason": "stop"},
                ],
                "willRetry": False,
            })
            emit({"type": "agent_settled"})
            continue
        if mode in {"no-text", "no-text-whitespace"}:
            emit({"type": "agent_end", "messages": [], "willRetry": False})
            emit({"type": "agent_settled"})
            continue
        # One retried run before the real answer proves willRetry gates the
        # fallback grace window.
        emit({"type": "agent_end", "messages": [], "willRetry": True})
        emit({"type": "auto_retry_start", "attempt": 1})
        emit(
            {
                "type": "agent_end",
                "messages": [assistant(f"{prompt} done")],
                "willRetry": False,
            }
        )
        if mode != "no-settled":
            emit({"type": "agent_settled"})
        if mode == "exit-after-turn":
            print("fake pi exited after its first turn", file=sys.stderr, flush=True)
            sys.exit(23)


if __name__ == "__main__":
    main()
