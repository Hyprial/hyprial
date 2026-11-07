"""Read-only link queries from an isolated registered agent session.

The CLI must keep the strict query schema, reject other actor selectors and
distinguish an empty link list from an unavailable daemon in its output.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.integration.cli.link.test_link_daemon_parity import (
    ALICE,
    BOB,
    OWNER,
    _environment,
    _ipc,
    _run_cli,
    _start_daemon,
)
from tests.support.runtime._tmp import hyprial_test_tmp_root


@pytest.mark.parametrize("command", ["list", "stats"])
def test_registered_agent_can_query_only_its_own_links(command: str) -> None:
    with tempfile.TemporaryDirectory(prefix="link-bound-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        env = _environment(root)
        process, log = _start_daemon(root, env)
        try:
            registration = _ipc(
                Path(env["HARNESS_STATE_DIR"]) / "daemon.sock",
                "session.register",
                {
                    "actor": "alice",
                    "sessionRef": "session-alice",
                    "cwd": str(root),
                    "command": ["claude"],
                    "source": "claude-channel",
                    "channelConfirmed": True,
                },
            )
            assert "error" not in registration, registration
            env.update(HYPRIAL_WORKER_ACTOR=ALICE, HYPRIAL_WORKER_SESSION_REF="session-alice")
            own = _run_cli(env, ["agent", "link", command, "--from", ALICE, "--json"])
            assert own["exitCode"] == 0, own
            payload = json.loads(own["stdout"])
            assert payload["ok"] is True and payload["links"] == [], own
            assert own["stderr"] == "", own

            alias = _run_cli(env, ["agent", "link", command, "--from", "alice", "--json"])
            assert alias["exitCode"] == 0, alias
            assert json.loads(alias["stdout"])["actor"] == ALICE, alias

            for source in (BOB, f"user:{OWNER}"):
                other = _run_cli(env, ["agent", "link", command, "--from", source, "--json"])
                assert other["exitCode"] == 1, other
                denied = json.loads(other["stdout"])
                assert denied["ok"] is False, other
                assert denied["code"] == "SENDER_NOT_AUTHENTICATED", other
                assert other["stderr"] == "", other

            if command == "list":
                empty = _run_cli(env, ["agent", "link", "list", "--from", ALICE])
                assert empty["exitCode"] == 0, empty
                assert "No links recorded." in empty["stdout"], empty
        finally:
            process.terminate()
            process.wait(timeout=30)
            log.close()
