from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path


MODE = os.environ.get("FAKE_CODEX_MODE", "complete")
LOG_PATH = os.environ.get("FAKE_CODEX_LOG")
STATE_PATH = os.environ.get("FAKE_CODEX_STATE")
RELEASE_PATH = os.environ.get("FAKE_CODEX_RELEASE")
THREAD_ID = "codex-thread-1"

turns: dict[str, dict[str, object]] = {}
active_turn_id: str | None = None
turn_number = 0
approval_prompt: str | None = None
unsupported_prompt: str | None = None


_emit_lock = threading.Lock()


def emit(message: dict[str, object]) -> None:
    with _emit_lock:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def record(message: dict[str, object]) -> None:
    if LOG_PATH is None:
        return
    if MODE == "pac-complete":
        # Keep test identity/turn evidence, never native session credentials.
        safe = {key: message[key] for key in ("id", "method", "pacCompletion") if key in message}
        safe["fixtureActor"] = os.environ.get("HYPRIAL_WORKER_ACTOR")
        safe["pid"] = os.getpid()
        if message.get("method") == "turn/start":
            safe["input"] = message.get("params", {}).get("input", [])
        message = safe
    with Path(LOG_PATH).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, separators=(",", ":")) + "\n")


def respond(identifier: object, result: object) -> None:
    emit({"id": identifier, "result": result})


def complete(turn_id: str, prompt: str) -> None:
    if MODE == "pac-complete" and "fixture:turn-fail" in prompt:
        emit({"method":"turn/completed","params":{"threadId":THREAD_ID,
              "turn":{"id":turn_id,"status":"failed","items":[],"error":{"message":"fixture transport failed"}}}})
        return
    if MODE == "pac-complete" and "fixture:no-flag" not in prompt:
        import re
        match = re.search(r"hyprial workflow complete (wf-[a-f0-9]+) ([A-Za-z][A-Za-z0-9_.-]*) --request-id (workflow-request:[a-f0-9]+)", prompt)
        if match:
            result = subprocess.run(
                [sys.executable, "-m", "hyprial.cli", "workflow", "complete",
                 match[1], match[2], "--request-id", match[3],
                 "--reason-ref", "fixture:pac-completed", "--json"],
                text=True, capture_output=True, timeout=15,
            )
            record({"pacCompletion": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
            if result.returncode:
                emit({"method": "turn/completed", "params": {"threadId": THREAD_ID,
                      "turn": {"id": turn_id, "status": "failed", "items": [],
                               "error": {"message": "fixture PAC completion failed"}}}})
                return
    if MODE == "failed":
        turn = {
            "id": turn_id,
            "status": "failed",
            "items": [],
            "error": {"message": "model turn failed"},
        }
    else:
        items: list[dict[str, object]] = []
        if MODE != "no-final":
            items.append(
                {
                    "type": "agentMessage",
                    "text": f"{prompt} done",
                    "phase": "final_answer",
                }
            )
        turn = {
            "id": turn_id,
            "status": "completed",
            "items": items,
            "error": None,
        }
    turns[turn_id] = turn
    if MODE != "no-notification":
        emit(
            {
                "method": "turn/completed",
                "params": {"threadId": THREAD_ID, "turn": turn},
            }
        )


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    record(message)
    method = message.get("method")
    identifier = message.get("id")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}

    if method == "initialize":
        respond(identifier, {
            "userAgent": "fake-codex-app-server",
            "codexHome": os.environ.get("CODEX_HOME"),
        })
    elif method == "initialized":
        continue
    elif method == "config/read":
        config_path = Path(os.environ["CODEX_HOME"]) / "config.toml"
        config = tomllib.loads(config_path.read_text())
        respond(identifier, {
            "config": config,
            "layers": [
                {"name": {"type": "user", "file": str(config_path)}, "config": config},
                {"name": {"type": "system", "file": "/etc/codex/config.toml"}, "config": {}},
            ],
        })
    elif method == "account/read":
        # This is a credential-free protocol fixture, never a real login.
        respond(identifier, {"account": None, "requiresOpenaiAuth": False})
    elif method == "skills/list":
        respond(identifier, {"data": [
            {"cwd": cwd, "skills": [], "errors": []}
            for cwd in params.get("cwds", [])
        ]})
    elif method == "thread/start":
        if MODE == "orphan-on-eof" and STATE_PATH is not None:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"]
            )
            Path(STATE_PATH).write_text(str(child.pid), encoding="utf-8")
        respond(
            identifier,
            {"thread": {"id": THREAD_ID, "status": {"type": "idle"}, "turns": []}},
        )
    elif method == "thread/resume":
        if MODE in {"loaded-fallback", "resume-miss"}:
            emit(
                {
                    "id": identifier,
                    "error": {"code": -32602, "message": "no rollout found"},
                }
            )
            continue
        respond(
            identifier,
            {"thread": {"id": params.get("threadId"), "status": {"type": "idle"}, "turns": []}},
        )
    elif method == "thread/loaded/list":
        # resume-miss: the thread is in no rollout and not loaded either, so
        # the client must treat the stored ref as definitively gone.
        respond(identifier, {"data": [] if MODE == "resume-miss" else [THREAD_ID]})
    elif method == "turn/start":
        turn_number += 1
        active_turn_id = f"turn-{turn_number}"
        prompt_items = params.get("input")
        prompt = ""
        if isinstance(prompt_items, list) and prompt_items:
            first = prompt_items[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                prompt = first["text"]
        if MODE == "delay-turn-start":
            assert STATE_PATH is not None
            assert RELEASE_PATH is not None
            Path(STATE_PATH).write_text(active_turn_id, encoding="utf-8")
            while not Path(RELEASE_PATH).exists():
                time.sleep(0.001)
        respond(identifier, {"turn": {"id": active_turn_id}})
        if MODE == "sigstop-turn" and STATE_PATH is not None:
            Path(STATE_PATH).write_text(str(os.getpid()), encoding="utf-8")
        if MODE == "approval":
            approval_prompt = prompt
            emit(
                {
                    "id": 900,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": THREAD_ID,
                        "turnId": active_turn_id,
                        "itemId": "command-1",
                        "startedAtMs": 1,
                    },
                }
            )
            continue
        if MODE == "unsupported-request":
            # A server request this client has never seen: it must still be
            # answered fail-closed (and named in the worker log), and the
            # turn must still complete rather than wait forever.
            unsupported_prompt = prompt
            emit(
                {
                    "id": 901,
                    "method": "attestation/generate",
                    "params": {
                        "threadId": THREAD_ID,
                        "turnId": active_turn_id,
                    },
                }
            )
            continue
        if MODE == "crash-once" and STATE_PATH is not None:
            marker = Path(STATE_PATH)
            if not marker.exists():
                marker.write_text("crashed", encoding="utf-8")
                raise SystemExit(17)
        if MODE == "progress":
            emit(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": THREAD_ID,
                        "turn": {
                            "id": active_turn_id,
                            "status": "inProgress",
                            "items": [],
                        },
                    },
                }
            )
            emit(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": THREAD_ID,
                        "turnId": active_turn_id,
                        "startedAtMs": 1,
                        "item": {
                            "id": "command-1",
                            "type": "commandExecution",
                            "command": "printf ok",
                            "commandActions": [],
                            "cwd": "/work",
                            "status": "inProgress",
                        },
                    },
                }
            )
            # Route-C boundary: raw delta streams are not progress events.
            emit(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": THREAD_ID,
                        "turnId": active_turn_id,
                        "itemId": "message-1",
                        "delta": "partial",
                    },
                }
            )
            emit(
                {
                    "method": "turn/plan/updated",
                    "params": {
                        "threadId": THREAD_ID,
                        "turnId": active_turn_id,
                        "plan": [
                            {"step": "inspect", "status": "completed"},
                            {"step": "report", "status": "inProgress"},
                        ],
                    },
                }
            )
            emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": THREAD_ID,
                        "turnId": active_turn_id,
                        "completedAtMs": 2,
                        "item": {
                            "id": "command-1",
                            "type": "commandExecution",
                            "command": "printf ok",
                            "commandActions": [],
                            "cwd": "/work",
                            "status": "completed",
                            "durationMs": 7,
                            "exitCode": 0,
                        },
                    },
                }
            )
        if MODE == "hold-inprogress":
            # Level state only: thread/read keeps answering "inProgress"
            # while no notification ever arrives -- the stuck-turn shape
            # that must NOT renew the idle lease.
            turns[active_turn_id] = {
                "id": active_turn_id,
                "status": "inProgress",
                "items": [],
                "error": None,
            }
        if MODE == "slow-complete":
            # Finish well after any would-be wall-clock deadline while the
            # server stays responsive (thread/read keeps answering): proves
            # a long turn is never killed on time and still delivers.
            def slow_complete(
                turn_id: str = active_turn_id, turn_prompt: str = prompt
            ) -> None:
                time.sleep(2.0)
                try:
                    complete(turn_id, turn_prompt)
                except Exception:
                    return

            threading.Thread(target=slow_complete, daemon=True).start()
        if MODE == "quiet-then-complete":
            # A quiet stretch (stall report should fire), one correlated
            # notification (resume), then normal completion.
            def quiet_then_complete(
                turn_id: str = active_turn_id, turn_prompt: str = prompt
            ) -> None:
                time.sleep(2.5)
                try:
                    emit(
                        {
                            "method": "item/updated",
                            "params": {
                                "threadId": THREAD_ID,
                                "turnId": turn_id,
                                "item": {
                                    "id": "command-1",
                                    "type": "commandExecution",
                                    "status": "inProgress",
                                },
                            },
                        }
                    )
                    time.sleep(0.3)
                    complete(turn_id, turn_prompt)
                except Exception:
                    return

            threading.Thread(target=quiet_then_complete, daemon=True).start()
        if MODE in {"hold-chatter", "hold-chatter-uncorrelated"}:
            chatter_turn_id = (
                active_turn_id if MODE == "hold-chatter" else "other-turn"
            )

            def chatter(turn_id: str = chatter_turn_id) -> None:
                while True:
                    time.sleep(0.15)
                    try:
                        emit(
                            {
                                "method": "item/updated",
                                "params": {
                                    "threadId": THREAD_ID,
                                    "turnId": turn_id,
                                    "item": {
                                        "id": "command-1",
                                        "type": "commandExecution",
                                        "status": "inProgress",
                                    },
                                },
                            }
                        )
                    except Exception:
                        return

            threading.Thread(target=chatter, daemon=True).start()
        if MODE not in {
            "hold",
            "hold-inprogress",
            "hold-chatter",
            "hold-chatter-uncorrelated",
            "quiet-then-complete",
            "slow-complete",
            "interrupt",
            "sigstop-turn",
            "delay-turn-start",
        }:
            complete(active_turn_id, prompt)
    elif identifier == 900 and approval_prompt is not None:
        assert message == {"id": 900, "result": {"decision": "decline"}}
        complete(str(active_turn_id), approval_prompt)
        approval_prompt = None
    elif identifier == 901 and unsupported_prompt is not None:
        # The fail-closed answer is a JSON-RPC error; completing here proves
        # the transport answer is what unblocks the turn.
        assert "error" in message
        complete(str(active_turn_id), unsupported_prompt)
        unsupported_prompt = None
    elif method == "turn/steer":
        steer_mode = os.environ.get("FAKE_CODEX_STEER_MODE", "")
        if steer_mode == "silent":
            pass  # never answer: the write may have landed, the reply did not
        elif steer_mode == "stale":
            emit(
                {
                    "id": identifier,
                    "error": {
                        "code": -32001,
                        "message": "no active turn matches expectedTurnId",
                    },
                }
            )
        elif steer_mode == "different":
            respond(identifier, {"turnId": "turn-other-protocol-error"})
        else:
            active_turn_id = str(params.get("expectedTurnId"))
            respond(identifier, {"turnId": active_turn_id})
    elif method == "turn/interrupt":
        turn_id = str(params.get("turnId"))
        respond(identifier, {})
        turn = {
            "id": turn_id,
            "status": "interrupted",
            "items": [],
            "error": {"message": "turn interrupted"},
        }
        turns[turn_id] = turn
        emit(
            {
                "method": "turn/completed",
                "params": {"threadId": THREAD_ID, "turn": turn},
            }
        )
    elif method == "thread/read":
        respond(
            identifier,
            {
                "thread": {
                    "id": params.get("threadId"),
                    "status": {"type": "idle"},
                    "turns": list(turns.values()),
                }
            },
        )
    elif identifier is not None:
        emit(
            {
                "id": identifier,
                "error": {"code": -32601, "message": f"unsupported method {method}"},
            }
        )
