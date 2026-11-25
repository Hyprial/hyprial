"""Fake Antigravity ACP stdio server for deterministic unit and process tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _log_event(log_path: str | None, event: str, payload: object = None) -> None:
    if not log_path:
        return
    record = {"event": event, "payload": payload}
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _model_options() -> list[dict[str, str]]:
    return [
        {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
        {"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
        {"value": "gemini-pro-orchestrator", "name": "Orchestrator"},
    ]


def _available_models() -> list[dict[str, str]]:
    return [
        {"modelId": "gemini-4-neon-high", "name": "Gemini 4 Neon (High)"},
        {"modelId": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"},
        {"modelId": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
        {"modelId": "gemini-pro-orchestrator", "name": "Orchestrator"},
    ]


def _thought_options(model: str) -> list[dict[str, str]] | None:
    if model.startswith("gemini-pro-orchestrator"):
        return None
    if model.startswith("gemini-3.8-flash"):
        return [
            {"value": "low", "name": "Low"},
            {"value": "medium", "name": "Medium"},
            {"value": "high", "name": "High"},
        ]
    return [
        {"value": "low", "name": "Low"},
        {"value": "medium", "name": "Medium"},
        {"value": "high", "name": "High"},
        {"value": "xhigh", "name": "Extra"},
    ]


def _build_config_options(model: str, thought: str, mode: str) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": model,
            "options": _model_options(),
        }
    ]
    thoughts = _thought_options(model)
    if thoughts is not None:
        options.append(
            {
                "id": "thought_level",
                "name": "Thinking Level",
                "category": "thought_level",
                "type": "select",
                "currentValue": thought,
                "options": thoughts,
            }
        )
    options.append(
        {
            "id": "mode",
            "name": "Mode",
            "category": "mode",
            "type": "select",
            "currentValue": mode,
            "options": [
                {"value": "default", "name": "Default"},
                {"value": "accept-edits", "name": "Accept edits"},
                {"value": "plan", "name": "Plan"},
            ],
        }
    )
    return options


def _session_payload(sid: str, model: str, thought: str, mode: str) -> dict[str, Any]:
    return {
        "sessionId": sid,
        "models": {
            "availableModels": _available_models(),
            "currentModelId": f"{model}-{thought}" if model in {"gemini-4-neon", "gemini-3.8-flash"} else model,
        },
        "modes": {
            "currentModeId": mode,
            "availableModes": [
                {"id": "default", "name": "Default"},
                {"id": "accept-edits", "name": "Accept edits"},
                {"id": "plan", "name": "Plan"},
            ],
        },
        "configOptions": _build_config_options(model, thought, mode),
    }


def _probe_mcp_server(mcp_servers: Any, log_path: str | None) -> str:
    if not isinstance(mcp_servers, list) or not mcp_servers:
        return "no-mcp"
    entry = mcp_servers[0]
    cmd = [entry["command"], *entry.get("args", [])]
    child_env = dict(os.environ)
    for item in entry.get("env", []):
        child_env[item["name"]] = item["value"]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        discover = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/clientCapabilities": {
                        "elicitation": {"form": {}, "url": {}},
                        "roots": {"listChanged": True},
                    },
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "antigravity-client",
                        "version": "v1.0.0",
                    },
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                }
            },
        }
        proc.stdin.write(json.dumps(discover) + "\n")
        proc.stdin.flush()
        discover_resp = json.loads(proc.stdout.readline())
        _log_event(log_path, "mcp_discover", discover_resp)

        meta = discover["params"]["_meta"]
        tools_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {"_meta": meta},
        }
        proc.stdin.write(json.dumps(tools_req) + "\n")
        proc.stdin.flush()
        tools_resp = json.loads(proc.stdout.readline())
        _log_event(log_path, "mcp_tools_list", tools_resp)

        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "_meta": meta,
                "name": "harness_whoami",
                "arguments": {},
            },
        }
        proc.stdin.write(json.dumps(call_req) + "\n")
        proc.stdin.flush()
        call_resp = json.loads(proc.stdout.readline())
        _log_event(log_path, "mcp_tools_call", call_resp)
        return json.dumps(call_resp.get("result", {}), separators=(",", ":"))
    finally:
        proc.stdin.close()
        proc.wait(timeout=5.0)


def main() -> int:
    mode = os.environ.get("FAKE_ACP_MODE", "normal")
    log_path = os.environ.get("FAKE_ACP_LOG")
    state_path = os.environ.get("FAKE_ACP_STATE")
    known_sid = os.environ.get("FAKE_ACP_KNOWN_SID", "known-session-1")
    authenticated = mode != "require_api_key"
    current_model = "gemini-4-neon"
    current_thought = "high"
    current_mode = "default"
    active_sid = known_sid
    mcp_summary = ""
    turn_count = 0

    _log_event(log_path, "argv", sys.argv[1:])

    pending_lock = threading.Lock()
    server_replies: dict[tuple[str, str], dict[str, Any]] = {}
    server_reply_event = threading.Event()
    cancel_event = threading.Event()
    prompt_worker: threading.Thread | None = None

    def handle_prompt(req_id: int | str, params: dict[str, Any]) -> None:
        nonlocal turn_count
        turn_count += 1
        sid = params.get("sessionId", active_sid)
        prompt_items = params.get("prompt", [])
        prompt_text = ""
        if isinstance(prompt_items, list) and prompt_items:
            prompt_text = str(prompt_items[0].get("text", ""))
        _log_event(
            log_path,
            "session/prompt",
            {
                "id": req_id,
                "sessionId": sid,
                "text": prompt_text,
                "model": current_model,
                "thought": current_thought,
                "mode": current_mode,
                "turn": turn_count,
            },
        )

        if mode == "require_api_key_in_prompt":
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Authentication required"},
                }
            )
            return

        if mode == "crash_mid_turn_once" and state_path:
            marker = Path(state_path)
            if not marker.exists():
                marker.write_text("crashed-once", encoding="utf-8")
                _write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "partial "},
                            },
                        },
                    }
                )
                os._exit(2)

        if mode == "permission_str_id":
            for perm_id, tool_name in (
                ("1", "mcp_harness-bridge_harness_read"),
                (99, "run_command"),
            ):
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": perm_id,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": sid,
                            "toolCall": {
                                "toolCallId": f"tc-{perm_id}",
                                "name": tool_name,
                                "title": tool_name,
                            },
                            "options": [
                                {"optionId": "allow", "kind": "allow_once"},
                                {"optionId": "deny", "kind": "reject_once"},
                            ],
                        },
                    }
                )
                key = (type(perm_id).__name__, str(perm_id))
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with pending_lock:
                        if key in server_replies:
                            reply = server_replies.pop(key)
                            _log_event(
                                log_path,
                                "permission_reply",
                                {"id": perm_id, "id_type": type(perm_id).__name__, "tool": tool_name, "reply": reply},
                            )
                            break
                    server_reply_event.wait(timeout=0.05)
                    server_reply_event.clear()
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": "unknown-9",
                    "method": "antigravity/unknown_server_method",
                    "params": {},
                }
            )
            key = ("str", "unknown-9")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with pending_lock:
                    if key in server_replies:
                        reply = server_replies.pop(key)
                        _log_event(log_path, "unknown_reply", reply)
                        break
                server_reply_event.wait(timeout=0.05)
                server_reply_event.clear()

        if mode in {"cancel_then_cancelled", "late_chunk_after_cancel"} and (
            mode == "cancel_then_cancelled" or prompt_text == "turn1"
        ):
            cancel_event.wait(timeout=5.0)
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"stopReason": "cancelled", "usage": {"totalTokens": 10}},
                }
            )
            if mode == "late_chunk_after_cancel":
                time.sleep(0.02)
                _write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "STALE_LATE_CHUNK"},
                            },
                        },
                    }
                )
            return

        if mode == "cancel_then_end_turn":
            cancel_event.wait(timeout=5.0)
            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "natural finish"},
                        },
                    },
                }
            )
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"stopReason": "end_turn", "usage": {"totalTokens": 15}},
                }
            )
            return

        if mode == "cancel_then_eof":
            cancel_event.wait(timeout=5.0)
            os._exit(0)

        if mode == "cancel_hang":
            time.sleep(10.0)
            return

        if mode == "prompt_slow_overlap":
            time.sleep(0.12)

        if mode == "stale_correlation":
            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "promptId": True,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "STALE_CORRELATED_CHUNK"},
                        },
                    },
                }
            )

        _write(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "SECRET_THOUGHT"},
                    },
                },
            }
        )
        _write(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "name": "mcp_harness-bridge_harness_whoami",
                        "status": "in_progress",
                        "kind": "mcp",
                        "title": "harness_whoami",
                    },
                },
            }
        )
        _write(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-1",
                        "name": "mcp_harness-bridge_harness_whoami",
                        "status": "completed",
                        "title": "harness_whoami",
                    },
                },
            }
        )
        reply_text = mcp_summary if mode == "mcp_handshake_real" else f"echo:{prompt_text}"
        _write(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": reply_text},
                    },
                },
            }
        )
        _write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                },
            }
        )

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" in msg and "method" not in msg:
            key = (type(msg["id"]).__name__, str(msg["id"]))
            with pending_lock:
                server_replies[key] = msg
            server_reply_event.set()
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            _log_event(log_path, "initialize", params)
            supports_resume = mode != "no_resume_use_load"
            caps: dict[str, Any] = {
                "loadSession": True,
                "sessionCapabilities": {
                    "close": {},
                    "load": {},
                    "list": {},
                    **({"resume": {}} if supports_resume else {}),
                },
                "_meta": {"antigravity": {"systemPrompt": {"append": True}}},
            }
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": caps,
                        "agentInfo": {"name": "Fake Antigravity ACP", "version": "1.5.14"},
                    },
                }
            )
            continue

        if method == "authenticate":
            _log_event(log_path, "authenticate", params)
            if params.get("methodId") == "gemini-api-key" and os.environ.get("GEMINI_API_KEY"):
                authenticated = True
                _write({"jsonrpc": "2.0", "id": req_id, "result": {}})
            else:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32000, "message": "Authentication required"},
                    }
                )
            continue

        if not authenticated:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Authentication required"},
                }
            )
            continue

        if method == "session/list":
            _log_event(log_path, "session/list", params)
            sessions = (
                [{"sessionId": known_sid, "cwd": params.get("cwd", ""), "title": "existing"}]
                if known_sid
                else []
            )
            _write({"jsonrpc": "2.0", "id": req_id, "result": {"sessions": sessions}})
            continue

        if method == "session/new":
            _log_event(log_path, "session/new", params)
            if mode == "mcp_handshake_real":
                mcp_summary = _probe_mcp_server(params.get("mcpServers"), log_path)
            active_sid = known_sid or "new-session-1"
            current_model = "gemini-4-neon"
            current_thought = "high"
            current_mode = "default"
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": _session_payload(active_sid, current_model, current_thought, current_mode),
                }
            )
            continue

        if method == "session/resume":
            _log_event(log_path, "session/resume", params)
            if mode == "no_resume_use_load":
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
                continue
            active_sid = str(params.get("sessionId") or known_sid)
            if mode == "resume_replay":
                _write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": active_sid,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "REPLAYED_RESUME"},
                            },
                        },
                    }
                )
            current_model = "gemini-4-neon"
            current_thought = "high"
            current_mode = "default"
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": _session_payload(active_sid, current_model, current_thought, current_mode),
                }
            )
            continue

        if method == "session/load":
            _log_event(log_path, "session/load", params)
            active_sid = str(params.get("sessionId") or known_sid)
            for chunk_type, content_text in (
                ("user_message_chunk", "REPLAYED_USER_MSG"),
                ("agent_message_chunk", "REPLAYED_OLD_TURN"),
            ):
                _write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": active_sid,
                            "update": {
                                "sessionUpdate": chunk_type,
                                "content": {"type": "text", "text": content_text},
                            },
                        },
                    }
                )
            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": active_sid,
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "replay-tc",
                            "name": "replayed_tool",
                            "status": "completed",
                            "title": "replayed_tool",
                        },
                    },
                }
            )
            current_model = "gemini-4-neon"
            current_thought = "high"
            current_mode = "default"
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": _session_payload(active_sid, current_model, current_thought, current_mode),
                }
            )
            continue

        if method == "session/set_config_option":
            _log_event(log_path, "session/set_config_option", params)
            config_id = params.get("configId")
            value = str(params.get("value", ""))
            if config_id == "model":
                valid = {item["value"] for item in _model_options()} | {
                    item["modelId"] for item in _available_models()
                }
                if value not in valid:
                    _write(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32602, "message": f"Unknown model ID: {value}"},
                        }
                    )
                    continue
                if value.startswith("gemini-3.8-flash"):
                    current_model = "gemini-3.8-flash"
                    if value.endswith("-low"):
                        current_thought = "low"
                elif value.startswith("gemini-4-neon"):
                    current_model = "gemini-4-neon"
                else:
                    current_model = value
            elif config_id == "thought_level":
                current_thought = value
            elif config_id == "mode":
                current_mode = value
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "configOptions": _build_config_options(
                            current_model, current_thought, current_mode
                        )
                    },
                }
            )
            continue

        if method == "session/prompt":
            cancel_event.clear()
            prompt_worker = threading.Thread(
                target=handle_prompt, args=(req_id, params), daemon=True
            )
            prompt_worker.start()
            continue

        if method == "session/cancel":
            _log_event(log_path, "session/cancel", {"has_id": "id" in msg, "params": params})
            cancel_event.set()
            continue

        if method == "session/close":
            _log_event(log_path, "session/close", params)
            _write({"jsonrpc": "2.0", "id": req_id, "result": {}})
            continue

        if req_id is not None:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )

    if prompt_worker is not None:
        prompt_worker.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
