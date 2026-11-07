"""Controlled CLI transport for the real pinned Claude Agent SDK.

This fixture records actual argv from the SDK and answers initialization;
it is not Claude Code, a provider or a proof of vendor transcript recovery.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4


def main() -> int:
    if "--version" in sys.argv or "-v" in sys.argv:
        print("2.1.999 (Claude Code test transport)")
        return 0
    args = sys.argv[1:]
    def option(name: str) -> str | None:
        for index, value in enumerate(args):
            if value.startswith(name + "="):
                return value.split("=", 1)[1]
            if value == name:
                return args[index + 1]
        return None
    log = Path(os.environ["HYPRIAL_TEST_CLAUDE_ARGV_LOG"])
    with log.open("a") as stream:
        stream.write(json.dumps(args) + "\n")
    root = Path(os.environ["HYPRIAL_TEST_CLAUDE_SESSIONS"])
    root.mkdir(exist_ok=True)
    resumed = option("--resume")
    if resumed:
        ref = resumed
        if not (root / ref).is_file():
            print(f"No conversation found with session ID {ref}", file=sys.stderr, flush=True)
            return 17
    else:
        ref = option("--session-id") or str(uuid4())
        (root / ref).write_text("established")
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("type") == "control_request":
            print(json.dumps({
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request["request_id"], "response": {}},
            }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
