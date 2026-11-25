#!/usr/bin/env python3
"""Codex app-server shim: read the message body before the fixture acts.

Design `docs/design/design-send-links-2026-10-14.md` §7 and the hard cut C07:
the carrier injects the ``summary`` only, plus a pointer to
``harness_read_body`` / ``hyprial message read``.  A real model calls that tool; the
deterministic ``fake_codex_app_server.py`` fixture cannot, because it only
matches the completion command against the turn prompt.

This shim is the harness-side emulation of that read, and it stays entirely
inside ``contract/e2e`` (the fixture under ``tests/`` is untouched).  It sits
between the worker channel and the fixture:

  1. it forwards every line of the app-server protocol unchanged;
  2. for a ``turn/start`` whose injected summary names a ``messageId``, it
     fetches that body with the worker's own identity
     (``hyprial message read <id> --from <HYPRIAL_WORKER_ACTOR> --json``) and appends
     it to the turn input, so the fixture sees exactly the text a model that
     followed the injection contract would have seen;
  3. a failed read is not fatal: the turn proceeds with the summary alone,
     which keeps a transient daemon failure visible as the scenario's own
     timeout rather than a shim crash.

Run as the ``codex`` executable a runner installs on PATH; ``FAKE_CODEX_MODE``
and the fixture path are inherited from that wrapper.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "harnesses" / "fake_codex_app_server.py"

#: The injection block writes ``messageId: <id>, bodyBytes: N)``; the id is a
#: uuid-shaped token, never spaces.
_MESSAGE_ID = re.compile(r"messageId:\s*([A-Za-z0-9._:-]+)")


def fetch_body(message_id: str) -> str | None:
    """Read one body as the worker this shim belongs to, or None on failure."""

    actor = os.environ.get("HYPRIAL_WORKER_ACTOR")
    if not actor:
        return None
    completed = subprocess.run(
        [
            sys.executable, "-m", "hyprial.cli", "message", "read", message_id,
            "--from", actor, "--json",
        ],
        text=True, capture_output=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    body = value.get("message") if isinstance(value, dict) else None
    return body if isinstance(body, str) else None


def augment(message: dict) -> dict:
    """Append the referenced body to one ``turn/start`` input, in place."""

    if message.get("method") != "turn/start":
        return message
    params = message.get("params")
    if not isinstance(params, dict):
        return message
    items = params.get("input")
    if not isinstance(items, list) or not items:
        return message
    first = items[0]
    if not isinstance(first, dict) or not isinstance(first.get("text"), str):
        return message
    match = _MESSAGE_ID.search(first["text"])
    if match is None:
        return message
    body = fetch_body(match.group(1))
    if body is None:
        return message
    first["text"] = f"{first['text']}\n--- message body ---\n{body}"
    return message


def forward(child: subprocess.Popen[str]) -> None:
    assert child.stdout is not None
    for line in child.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()


def main() -> int:
    child = subprocess.Popen(
        [sys.executable, str(FIXTURE), *sys.argv[1:]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    reader = threading.Thread(target=forward, args=(child,), daemon=True)
    reader.start()
    assert child.stdin is not None
    try:
        for line in sys.stdin:
            stripped = line.strip()
            if not stripped:
                child.stdin.write(line)
            else:
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    child.stdin.write(line)
                else:
                    child.stdin.write(
                        json.dumps(augment(message), separators=(",", ":")) + "\n"
                    )
            child.stdin.flush()
    except BrokenPipeError:
        pass
    finally:
        child.stdin.close()
    reader.join(timeout=30)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
