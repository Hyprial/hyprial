"""Phase B CLI end to end: send, then read it back by conversation.

One isolated daemon (``python -m hyprial.cli daemon run`` under a private
home, state root and HOME, no network) and the real CLI as subprocesses: the
operator sends to a local agent, then ``message conversation list`` and
``message history`` read the same message back through ``conversations.*``
by ``c.`` id; both ``--thread`` on ``message conversation list`` and
``--thread`` on ``message history`` are refused.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from tests.integration.cli.isolated_daemon import OWNER, environment, ipc, run_cli, start_daemon
from tests.support.runtime._tmp import hyprial_test_tmp_root

OPERATOR = f"user:{OWNER}"


def _json(env: dict[str, str], *argv: str) -> dict[str, Any]:
    actual = run_cli(env, [*argv, "--json"])
    assert actual["stderr"] == "", actual
    payload = json.loads(actual["stdout"])
    assert actual["exitCode"] == (0 if payload["ok"] is True else 1), actual
    return payload


def _text(env: dict[str, str], *argv: str) -> str:
    actual = run_cli(env, list(argv))
    assert actual["exitCode"] == 0, actual
    return actual["stdout"]


def test_send_then_read_back_by_conversation() -> None:
    with tempfile.TemporaryDirectory(prefix="b-cli-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw) / "threads"
        root.mkdir()
        env = environment(root)
        process, log = start_daemon(root, env)
        try:
            created = ipc(Path(env["HARNESS_STATE_DIR"]) / "daemon.sock", "agent.create", {"name": "alice"})
            ALICE = created.get("result", created)["agent"]["uri"]
            sent = _json(
                env, "message", "send", "hello alice", "--from", OPERATOR, "--to", ALICE,
                "--summary", "greeting", "--conversation", "conv-e2e",
            )
            assert sent["ok"] is True, sent

            listed = _json(env, "message", "conversation", "list")
            [head] = listed["conversations"]
            assert "threadId" not in head
            assert head["legacyConversationId"] == "conv-e2e"
            conversation = head["conversationId"]

            history = _json(env, "message", "history", "--conversation", conversation)
            assert [entry["message"] for entry in history["entries"]] == ["hello alice"]
            assert "threadId" not in history

            rendered = _text(env, "message", "history", "--conversation", conversation).splitlines()
            assert rendered[0].endswith(f"{OPERATOR} -> {ALICE}  request  {sent['messageId']}")
            assert rendered[1] == "  hello alice"

            refused_conv = run_cli(env, ["message", "conversation", "list", "--thread", ALICE, "--json"])
            assert refused_conv["exitCode"] == 2, refused_conv
            refused = run_cli(env, ["message", "history", "--thread", "t." + "a" * 26, "--json"])
            assert refused["exitCode"] == 2, refused

            on_behalf = _json(
                env, "message", "conversation", "list", "--on-behalf-of", ALICE,
            )
            assert on_behalf["participant"] == ALICE
            assert [row["conversationId"] for row in on_behalf["conversations"]] == [conversation]

            misuse = _json(env, "message", "history")
            assert misuse["code"] == "INVALID_ARGUMENT"
        finally:
            process.terminate()
            process.wait(timeout=30)
            log.close()
