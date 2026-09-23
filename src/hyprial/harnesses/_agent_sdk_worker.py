"""Dependency-isolated Claude Agent SDK JSONL worker.

This file is executed directly inside ``uv run --isolated``. Keep its imports
limited to the standard library and the pinned Agent SDK environment.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from typing import Any
from uuid import uuid4

try:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
except ModuleNotFoundError as error:
    # A raw ImportError traceback here used to be the only signal when the
    # SDK was absent (wrong interpreter, broken uv resolution). Surface one
    # actionable line instead; the parent reports this stderr tail verbatim.
    print(
        "Agent SDK worker cannot import claude-agent-sdk "
        f"(missing module: {error.name}). Launch the worker through "
        "hyprial.harnesses.agent_sdk.sdk_worker_command(), which runs it in an "
        "isolated uv environment with the pinned claude-agent-sdk release, or "
        "install claude-agent-sdk into this interpreter.",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(1) from error

# Per-worker Harness identity (the worker speaks to the daemon as its OWN
# canonical actor) depends on these ClaudeAgentOptions parameters. A pinned
# release that lacks them must fail closed at startup with a clear message
# instead of a TypeError deep inside option construction.  ``resume`` and
# ``session_id`` carry the daemon's session persistence: a restarted daemon
# resumes the worker's previous conversation instead of cold-starting it.
_REQUIRED_OPTION_PARAMS = frozenset(
    {"setting_sources", "strict_mcp_config", "resume", "session_id"}
)


def _require_supported_sdk() -> None:
    try:
        parameters = tuple(inspect.signature(ClaudeAgentOptions).parameters.values())
    except (TypeError, ValueError):
        return  # not introspectable; option construction still fails closed
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters):
        return  # a generic **kwargs constructor accepts every option
    missing = _REQUIRED_OPTION_PARAMS - {param.name for param in parameters}
    if not missing:
        return
    print(
        "Agent SDK worker requires a claude-agent-sdk release supporting "
        f"{', '.join(sorted(missing))} for per-worker Harness identity; the "
        "imported SDK is too old. Pin a supported release through "
        "hyprial.harnesses.agent_sdk.sdk_worker_command().",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(1)


def _options_kwargs() -> dict[str, Any]:
    raw = os.environ.get("HYPRIAL_AGENT_SDK_OPTIONS", "{}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("HYPRIAL_AGENT_SDK_OPTIONS must be an object")
    cwd = value.get("cwd")
    model = value.get("model")
    extra_args = value.get("extraArgs", {})
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("Agent SDK cwd must be a string")
    if model is not None and not isinstance(model, str):
        raise ValueError("Agent SDK model must be a string")
    if not isinstance(extra_args, dict):
        raise TypeError("Agent SDK extraArgs must be an object")
    # Session persistence, owned by the daemon: "resume" continues the
    # stored conversation after a daemon restart; "sessionId" pins the id of
    # a fresh conversation so it can be resumed later.  They never coexist.
    resume = value.get("resume")
    session_id = value.get("sessionId")
    if resume is not None and not isinstance(resume, str):
        raise TypeError("Agent SDK resume must be a string")
    if session_id is not None and not isinstance(session_id, str):
        raise TypeError("Agent SDK sessionId must be a string")
    # A daemon-managed worker overrides these so it speaks to the Harness
    # daemon as its OWN canonical actor: settingSources is narrowed and the
    # worker's own harness-bridge server is injected directly (with strict MCP
    # config) to keep the coordinator's ambient MCP out of the child.  Absent
    # those keys the defaults reproduce the pre-fix behavior exactly.
    setting_sources = value.get("settingSources", ["user", "project", "local"])
    if not isinstance(setting_sources, list) or any(
        not isinstance(item, str) for item in setting_sources
    ):
        raise TypeError("Agent SDK settingSources must be an array of strings")
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "model": model,
        "system_prompt": {"type": "preset", "preset": "claude_code"},
        "setting_sources": setting_sources,
        "extra_args": extra_args,
    }
    cli_path = os.environ.get("HYPRIAL_CLAUDE_EXECUTABLE")
    if os.environ.get("HYPRIAL_DESKTOP_COMPONENTS") == "1" and not cli_path:
        raise ValueError("Claude Code is not enabled; configure it in desktop setup")
    if cli_path:
        if not os.path.isabs(cli_path) or not os.path.isfile(cli_path):
            raise ValueError("Desktop Claude Code executable must be an existing absolute file")
        kwargs["cli_path"] = cli_path
    if resume is not None:
        kwargs["resume"] = resume
    elif session_id is not None:
        kwargs["session_id"] = session_id
    mcp_servers = value.get("mcpServers")
    if mcp_servers is not None:
        if not isinstance(mcp_servers, dict):
            raise TypeError("Agent SDK mcpServers must be an object")
        kwargs["mcp_servers"] = mcp_servers
    if "strictMcpConfig" in value:
        # Belt-and-suspenders with the emptied settingSources: only the
        # injected servers are used, so a settings-file server can never
        # re-introduce the coordinator's MCP identity into the worker.
        kwargs["strict_mcp_config"] = bool(value["strictMcpConfig"])
    allowed_tools = value.get("allowedTools")
    if allowed_tools is not None:
        if not isinstance(allowed_tools, list) or any(
            not isinstance(item, str) for item in allowed_tools
        ):
            raise TypeError("Agent SDK allowedTools must be an array of strings")
        kwargs["allowed_tools"] = allowed_tools
    return kwargs


def _options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(**_options_kwargs())


async def _read_commands(commands: asyncio.Queue[dict[str, Any]]) -> None:
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError("worker command must be an object")
        await commands.put(value)
    await commands.put({"op": "stop"})


def _emit(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _bounded(text: object, *, fallback: str) -> str:
    value = str(text).strip() if text is not None else ""
    if not value:
        value = fallback
    return value if len(value) <= 200 else f"{value[:197]}..."


def _progress_frames(message: object) -> list[dict[str, object]]:
    """Convert SDK boundary messages into route-C coarse progress frames.

    Duck typing is deliberate: this file runs inside an isolated pinned SDK,
    while tests use minimal fakes.  Raw StreamEvent deltas stay excluded.
    """

    name = type(message).__name__
    frames: list[dict[str, object]] = []
    if name == "AssistantMessage":
        content = getattr(message, "content", ())
        if not isinstance(content, list):
            return []
        for block in content:
            block_name = type(block).__name__
            if block_name == "TextBlock":
                frames.append(
                    {
                        "phase": "message-segment",
                        "summary": _bounded(
                            getattr(block, "text", "assistant text"),
                            fallback="assistant text",
                        ),
                    }
                )
            elif block_name == "ThinkingBlock":
                # Never forward chain-of-thought content; the boundary alone
                # is the progress signal.
                frames.append({"phase": "thinking", "summary": "thinking"})
            elif block_name in {"ToolUseBlock", "ServerToolUseBlock"}:
                tool_name = _bounded(getattr(block, "name", "tool"), fallback="tool")
                tool_input = getattr(block, "input", None)
                frames.append(
                    {
                        "phase": "tool-call",
                        "summary": f"calling {tool_name}",
                        "toolCallId": getattr(block, "id", None),
                        "toolName": tool_name,
                        "detail": {
                            "inputKeys": sorted(tool_input)
                            if isinstance(tool_input, dict)
                            else []
                        },
                    }
                )
            elif block_name in {"ToolResultBlock", "ServerToolResultBlock"}:
                is_error = getattr(block, "is_error", None)
                frames.append(
                    {
                        "phase": "tool-result",
                        "summary": (
                            "tool result error"
                            if is_error is True
                            else "tool result received"
                        ),
                        "toolCallId": getattr(block, "tool_use_id", None),
                        "detail": {"isError": is_error is True},
                    }
                )
        return frames
    if name == "TaskStartedMessage":
        return [
            {
                "phase": "tool-call",
                "summary": _bounded(
                    getattr(message, "description", "task started"),
                    fallback="task started",
                ),
                "toolCallId": getattr(message, "task_id", None),
                "toolName": getattr(message, "task_type", None) or "task",
            }
        ]
    if name == "TaskProgressMessage":
        return [
            {
                "phase": "thinking",
                "summary": _bounded(
                    getattr(message, "description", "task progress"),
                    fallback="task progress",
                ),
                "toolCallId": getattr(message, "tool_use_id", None),
                "toolName": getattr(message, "last_tool_name", None),
            }
        ]
    if name == "TaskNotificationMessage":
        status = getattr(message, "status", "finished")
        return [
            {
                "phase": "tool-result",
                "summary": _bounded(
                    getattr(message, "summary", f"task {status}"),
                    fallback=f"task {status}",
                ),
                "toolCallId": getattr(message, "task_id", None),
                "detail": {"status": status},
            }
        ]
    if name == "TaskUpdatedMessage":
        status = getattr(message, "status", None)
        return [
            {
                "phase": "thinking",
                "summary": f"task updated ({status or 'unknown'})",
                "toolCallId": getattr(message, "task_id", None),
            }
        ]
    if name in {"MirrorErrorMessage", "RateLimitEvent"}:
        return [
            {
                "phase": "retry",
                "summary": "Claude rate limit or mirror error observed",
            }
        ]
    if name == "HookEventMessage":
        hook = getattr(message, "hook_event_name", "")
        return [
            {
                "phase": "message-segment",
                "summary": f"hook event {hook or 'observed'}",
            }
        ]
    return []


def _connect_timeout_seconds() -> float:
    """Bound on the Claude CLI connect inside the worker.

    A connect that never completes (bad harness args making the CLI wait on
    stdin, a throttled handshake, a dead proxy) used to be invisible: the
    parent waited its own 300s startup timeout, killed the worker, and the
    restart loop repeated -- with no log line explaining any of it. The
    watchdog converts every such hang into ONE actionable stderr line and a
    non-zero exit at a bounded time, so the parent's worker.exited record
    carries the reason. Override via env for tests.
    """

    raw = os.environ.get("HYPRIAL_AGENT_SDK_CONNECT_TIMEOUT_SECONDS", "90")
    try:
        value = float(raw)
    except ValueError:
        return 90.0
    return value if value > 0 else 90.0


async def _receive_result(client: ClaudeSDKClient) -> dict[str, object]:
    output = ""
    is_error = False
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            output = message.result or ""
            is_error = message.is_error
            _emit(
                {
                    "type": "progress",
                    "phase": "turn-end",
                    "summary": (
                        "Claude turn failed" if is_error else "Claude turn completed"
                    ),
                    "terminal": True,
                }
            )
            continue
        for progress in _progress_frames(message):
            _emit({"type": "progress", **progress})
    return {"type": "result", "result": output, "isError": is_error}


async def _connect(options: ClaudeAgentOptions) -> ClaudeSDKClient:
    """Connect one SDK client, converting a hung connect into an exit."""

    client = ClaudeSDKClient(options=options)
    try:
        await asyncio.wait_for(
            client.__aenter__(), timeout=_connect_timeout_seconds()
        )
    except asyncio.TimeoutError:
        print(
            "Agent SDK worker: the Claude CLI subprocess did not finish "
            f"connecting within {_connect_timeout_seconds():.0f}s; the "
            "turn path never opened. Check the harness args passed to the "
            "CLI (an unknown flag can leave it waiting on stdin), "
            "anthropic connectivity/proxy, and account-level throttling.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from None
    return client


async def _serve() -> None:
    commands: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    reader = asyncio.create_task(_read_commands(commands))
    try:
        kwargs = _options_kwargs()
        try:
            client = await _connect(ClaudeAgentOptions(**kwargs))
        except Exception as error:
            resume = kwargs.get("resume")
            if not (isinstance(resume, str) and resume):
                raise
            # A stored session that no longer exists fails the CLI connect
            # (client-side validation: "No conversation found with session
            # ID"); the SDK does not surface the CLI's stderr in the
            # exception, so any connect failure with a resume target is
            # treated as a possibly-dead session.  Resume must never become
            # a startup failure source: retry exactly once with a fresh,
            # pinned session id and let the daemon learn the replacement
            # from the ready message.  A persistent environmental failure
            # fails this retry too and exits with the original clarity.
            print(
                f"Agent SDK worker: stored session {resume} could not be "
                f"resumed ({type(error).__name__}: {error}); starting a "
                "fresh session",
                file=sys.stderr,
                flush=True,
            )
            kwargs.pop("resume", None)
            kwargs["session_id"] = str(uuid4())
            client = await _connect(ClaudeAgentOptions(**kwargs))
        try:
            ready: dict[str, object] = {"type": "ready"}
            effective_session = kwargs.get("resume") or kwargs.get("session_id")
            if isinstance(effective_session, str):
                ready["sessionId"] = effective_session
            _emit(ready)
            while True:
                command = await commands.get()
                operation = command.get("op")
                if operation == "stop":
                    return
                if operation != "query":
                    continue
                prompt = command.get("prompt")
                if not isinstance(prompt, str) or not prompt:
                    _emit(
                        {
                            "type": "result",
                            "result": "Agent SDK query prompt is required",
                            "isError": True,
                        }
                    )
                    continue
                await client.query(prompt)
                response = asyncio.create_task(_receive_result(client))
                while True:
                    control = asyncio.create_task(commands.get())
                    done, _ = await asyncio.wait(
                        {response, control}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if response in done:
                        control.cancel()
                        await asyncio.gather(control, return_exceptions=True)
                        _emit(response.result())
                        break
                    next_command = control.result()
                    if next_command.get("op") == "interrupt":
                        await client.interrupt()
                    elif next_command.get("op") == "stop":
                        await client.interrupt()
                        await response
                        return
        finally:
            await client.__aexit__(None, None, None)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


def main() -> None:
    try:
        _require_supported_sdk()
        if "--probe" in sys.argv:
            client = ClaudeSDKClient(options=_options())
            _emit({"type": "probe", "client": type(client).__name__})
            return
        asyncio.run(_serve())
    except Exception as error:  # standalone process boundary
        print(f"Agent SDK worker failed: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
