"""S8 under ``HYPRIAL_SENDER_AUTH_MODE=enforce`` on a real isolated daemon.

Pins what an attached harness can do today without a registered session
(the review asked for this to be explicit, not implied by observe mode):

- ``message batch --from <attached>`` is refused ``SENDER_NOT_AUTHENTICATED``:
  the name is only a claim until a session proves it, and there is no
  operator form of ``message batch`` yet;
- the operator form ``--from user:<owner> --on-behalf-of <attached>`` works
  for ``message reply`` and ``message ack``;
- ``hyprial agent start`` of an attached agent is refused
  ``INVALID_ARGUMENT`` before anything is launched (custody=external): the
  harness binaries on PATH are stubs that leave a marker when run, and no
  marker appears.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from tests.integration.cli.attach.test_attach_cli_e2e import (
    OWNER,
    _environment,
    _cli,
    _ok,
    _refused,
    _start_daemon,
)
from tests.support.runtime._tmp import hyprial_test_tmp_root

OPERATOR = f"user:{OWNER}"
HARNESSES = ("claude", "pi")


def _stub_harnesses(root: Path) -> tuple[Path, Path]:
    bin_dir, marker = root / "bin", root / "launched"
    bin_dir.mkdir()
    for name in HARNESSES:
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir, marker


def _start_refused(env: dict[str, str], harness: str) -> None:
    done = _cli(env, "agent", "start", harness, "--name", "ext", "--headless", "--json")
    assert done.returncode == 1, (done.stdout, done.stderr)
    payload = json.loads(done.stdout)
    assert payload["code"] == "INVALID_ARGUMENT", payload
    assert "custody=external" in payload["error"], payload


def test_an_attached_harness_under_enforced_sender_auth() -> None:
    with tempfile.TemporaryDirectory(prefix="s8e-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw) / "e2e"
        root.mkdir()
        bin_dir, marker = _stub_harnesses(root)
        env = {**_environment(root), "HYPRIAL_SENDER_AUTH_MODE": "enforce"}
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        process, log = _start_daemon(root, env)
        try:
            _ok(env, "agent", "attach", "ext")
            first = str(_ok(env, "message", "send", "one", "--from", OPERATOR,
                            "--to", "ext", "--summary", "first")["messageId"])
            _refused(env, "SENDER_NOT_AUTHENTICATED", "message", "batch", "--from", "ext")
            replied = _ok(env, "message", "reply", first, "answered", "--from", OPERATOR,
                          "--on-behalf-of", "ext", "--summary", "answer")
            assert replied["replyMessageId"]
            second = str(_ok(env, "message", "send", "two", "--from", OPERATOR,
                             "--to", "ext", "--summary", "second")["messageId"])
            _ok(env, "message", "ack", second, "--from", OPERATOR, "--on-behalf-of", "ext")
            for harness in HARNESSES:
                _start_refused(env, harness)
            assert not marker.exists()
            assert _ok(env, "agent", "detach", "ext")["changed"] is True
        finally:
            process.terminate()
            process.wait(timeout=30)
            log.close()
