"""M-01: the migrated agent reads over a real daemon socket and the real CLI.

The in-process contract tests compare the legacy handler with the registry
binding; this one proves the same declarations survive the actual IPC transport
and that ``hyprial agent list`` / ``hyprial agent secret ls`` still print the
daemon result unchanged, including the ``AGENT_NOT_FOUND`` exit path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog import agent as agent_catalog
from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import (
    IsolatedHarness,
)

OWNER = "m01-cli"


@pytest.fixture
def harness(tmp_path: Path):
    node = IsolatedHarness(tmp_path / "node")
    node.write_owner(OWNER)
    node.start_daemon()
    try:
        yield node
    finally:
        node.stop_daemon()


def _cli(node: IsolatedHarness, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", *arguments, "--json"],
        env=node.environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    document = json.loads(result.stdout)
    assert isinstance(document, dict), document
    return document


def _without_run_state(result: dict[str, object]) -> dict[str, object]:
    return {
        **result,
        "agents": [
            {key: value for key, value in row.items() if key != "runState"}
            for row in result["agents"]  # type: ignore[union-attr]
        ],
    }


def test_agent_list_cli_prints_the_registry_result_unchanged(harness: IsolatedHarness) -> None:
    harness.ipc("agent.create", {"name": "alpha"})
    harness.ipc("agent.create", {"name": "beta", "provider": "deepseek"})
    harness.ipc("agent.create", {"name": "wf-worker"})

    over_ipc = harness.ipc("agent.list", {})
    printed = _json(_cli(harness, "agent", "list"))

    validator = Draft202012Validator(agent_catalog.AGENT_LIST_SPEC.result_schema)
    assert not list(validator.iter_errors(over_ipc)), over_ipc
    assert _without_run_state(printed) == _without_run_state(over_ipc)
    assert {row["actor"] for row in printed["agents"]} == {"alpha", "beta", "wf-worker"}

    filtered = _json(_cli(harness, "agent", "list", "--exclude-wf"))
    assert _without_run_state(filtered) == _without_run_state(
        harness.ipc("agent.list", {"excludeWf": True})
    )
    assert {row["actor"] for row in filtered["agents"]} == {"alpha", "beta"}

    recent = _json(_cli(harness, "agent", "list", "--inactive-since", "1h"))
    assert recent == harness.ipc("agent.list", {"inactiveSince": "1h"})
    assert recent["agents"] == []


def test_agent_secret_ls_cli_keeps_the_grant_inventory_and_error_semantics(
    harness: IsolatedHarness,
) -> None:
    harness.ipc("agent.create", {"name": "alpha"})

    assert _json(_cli(harness, "agent", "secret", "ls")) == {
        "ok": True,
        "grants": [],
    }
    assert _json(_cli(harness, "agent", "secret", "ls", "alpha")) == harness.ipc(
        "agent.secret.list", {"actor": "alpha"}
    )

    failed = _cli(harness, "agent", "secret", "ls", "ghost")
    assert failed.returncode == 1, (failed.stdout, failed.stderr)
    error = json.loads(failed.stdout)
    assert error["ok"] is False
    assert error["code"] == "AGENT_NOT_FOUND"
