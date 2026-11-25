"""A real daemon start runs the user agent ensure after ``daemon.ready``.

An isolated home with an owner and no agents (the upgrade path): the ensure
creates this machine's user agent; ``agent.list`` hides it by default and
shows it with ``includeUserAgent: true``.  Live test: it
starts a real daemon, so it runs in CI, not on a developer machine.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.user_agent import USER_AGENT_NAME
from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import IsolatedHarness
from tests.support.runtime._tmp import hyprial_test_tmp_root

OWNER = "ua-owner"


def _events(harness: IsolatedHarness) -> list[dict[str, Any]]:
    log = harness.state / "logs" / "daemon.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_a_started_daemon_runs_the_ensure_after_ready() -> None:
    root = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprial-user-agent-"))
    harness = IsolatedHarness(root)
    harness.write_owner(OWNER)
    try:
        harness.start_daemon()
        deadline = time.monotonic() + 30.0
        names: list[str] = []
        while time.monotonic() < deadline:
            names = [str(item.get("event")) for item in _events(harness)]
            if any(name.startswith("user_agent.") for name in names):
                break
            time.sleep(0.2)
        outcome = [name for name in names if name.startswith("user_agent.")]
        assert outcome, names[-20:]
        assert names.index("daemon.ready") < names.index(outcome[0])
        assert outcome[0] == "user_agent.created"
        assert USER_AGENT_NAME not in {
            row["actor"] for row in harness.ipc("agent.list", {})["agents"]
        }
        listed = harness.ipc("agent.list", {"includeUserAgent": True})
        rows = {row["actor"]: row for row in listed["agents"]}
        assert rows[USER_AGENT_NAME]["capabilities"] == {"template": "user"}
    finally:
        if harness.daemon is not None:
            harness.stop_daemon()
        shutil.rmtree(root, ignore_errors=True)
