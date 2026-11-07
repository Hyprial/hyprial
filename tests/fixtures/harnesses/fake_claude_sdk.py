"""A fake Claude Agent SDK worker for exercising the agent_sdk JSONL wire.

Disclosure -- the one piece that is not the product: the Claude *model
protocol* here is the repository's own double.  This file is NOT Claude Code
and NOT ``claude-agent-sdk``; it speaks only the JSONL worker wire the
product's ``IsolatedAgentSdkClient`` owns (ready -> query -> result, plus
progress/interrupt/stop).  A run driven by this double proves the product can
launch, bind, resume and drive a claude worker when the model protocol is a
double; the real vendor binary, the real SDK and the real credential remain
NOT_RUN (TC012 BLOCKED).  The reading such a run may claim is "real daemon,
real lifecycle, real identity, real ledger, real landing, fake model
protocol" and no more.  Everything else on the path is the product:
``IsolatedAgentSdkClient``/``ClaudeAgentSdkProcess``, the daemon's desired
state, lifecycle manager, identity registry and entity tokens, the P2 ledger,
bundle export/landing, runtime-context activation, the custody hold, and the
completion decision.

``FAKE_CLAUDE_MODE`` selects the behaviour under test:

- ``settled`` (default) - ``query`` completes with one terminal ``result``
- ``progress`` - progress frames precede the terminal result
- ``error``    - the terminal result carries ``isError: true``
- ``hang``     - accepts ``query`` and never answers (even on ``interrupt``)
- ``no-ready`` - exits before the ready handshake (a startup failure)
- ``crash``    - emits ready, then dies on the first query
- ``no-stop``  - ignores the stop op (force-stop owns cleanup)
- ``resume-miss`` - exits non-zero when launched with a stored ``resume``
  (the P2 resume target is gone; a fresh replacement is not allowed)

``FAKE_CLAUDE_LOG`` appends every received command as one JSON line,
preserving order for interrupt/stop race assertions.  ``FAKE_CLAUDE_ARGV_LOG``
appends each process's argv as one JSON line, so a test can assert how the
worker was launched.  ``FAKE_CLAUDE_SESSION_ID`` pins the advertised session
id.  ``--probe`` answers one model-free probe frame.  All switches are
independent and default off.
"""

from __future__ import annotations

import json
import os
import sys
from uuid import uuid4

MODE = os.environ.get("FAKE_CLAUDE_MODE", "settled")
LOG_PATH = os.environ.get("FAKE_CLAUDE_LOG")
ARGV_LOG = os.environ.get("FAKE_CLAUDE_ARGV_LOG")
PINNED_SESSION = os.environ.get("FAKE_CLAUDE_SESSION_ID")


def emit(value: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def record(value: dict[str, object]) -> None:
    if LOG_PATH is None:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def options() -> dict[str, object]:
    """The daemon's ``HYPRIAL_AGENT_SDK_OPTIONS`` payload, or an empty object."""

    raw = os.environ.get("HYPRIAL_AGENT_SDK_OPTIONS", "{}")
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def session_id(value: dict[str, object]) -> str:
    """What the real worker reports in ``ready``: resume, pinned id, or fresh.

    The real ``_agent_sdk_worker`` answers ``kwargs["resume"] or
    kwargs["sessionId"]``, and a fresh conversation mints its id.  Pinning is a
    test-only override so a caller can assert an exact ref without guessing.
    """

    if PINNED_SESSION:
        return PINNED_SESSION
    for key in ("resume", "sessionId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return str(uuid4())


def read_command() -> dict[str, object] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    command = json.loads(line)
    record(command)
    return command


def progress_frames() -> list[dict[str, object]]:
    return [
        {
            "type": "progress",
            "phase": "tool-call",
            "summary": "calling read",
            "toolCallId": "call-1",
            "toolName": "read",
        },
        {"type": "progress", "phase": "thinking", "summary": "thinking"},
        {
            "type": "progress",
            "phase": "turn-end",
            "summary": "Claude turn completed",
            "terminal": True,
        },
    ]


def serve() -> int:
    value = options()
    if MODE == "resume-miss" and isinstance(value.get("resume"), str) and value.get("resume"):
        print(
            f"fake claude: stored session {value['resume']} could not be resumed",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if MODE == "no-ready":
        print("fake claude: ready handshake withheld", file=sys.stderr, flush=True)
        return 1
    emit({"type": "ready", "sessionId": session_id(value)})
    while True:
        command = read_command()
        if command is None:
            return 0
        operation = command.get("op")
        if operation == "stop":
            if MODE == "no-stop":
                continue
            return 0
        if operation == "interrupt":
            # The real worker ends the in-flight turn; this double's turns are
            # synchronous, so there is nothing to advance.
            continue
        if operation != "query":
            continue
        if MODE == "crash":
            print("fake claude: crashed on the first query", file=sys.stderr, flush=True)
            return 23
        if MODE == "hang":
            continue
        if MODE == "progress":
            for frame in progress_frames():
                emit(frame)
        prompt = command.get("prompt")
        text = prompt if isinstance(prompt, str) else ""
        emit(
            {
                "type": "result",
                "result": "model turn failed" if MODE == "error" else f"{text} done",
                "isError": MODE == "error",
            }
        )


def main() -> int:
    if ARGV_LOG:
        with open(ARGV_LOG, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(sys.argv[1:]) + "\n")
    if "--probe" in sys.argv:
        emit({"type": "probe", "client": "fake-claude-sdk"})
        return 0
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
