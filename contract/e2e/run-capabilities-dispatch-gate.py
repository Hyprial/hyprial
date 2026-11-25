#!/usr/bin/env python3
"""CAPMX acceptance: three controlled checks through one real isolated daemon.

This runner uses the checkout's hyprial executable and the installed Pi binary.  It
never substitutes a fake harness protocol, sends a model turn, or touches the
ambient ~/.hyprial daemon/state.  The interactive leg registers through the same
public session.register IPC used by MCP/interactive carriers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (
    ROOT,
    IsolatedDaemon,
    hyprial_executable,
    sanitized_env,
    scenario_tempdir,
    short_temp_root,
    teardown_guard,
)

SCENARIO = "CAPMX-CONTROLLED"
HEADLESS = "capmx-headless"
INTERACTIVE = "capmx-interactive"
DRIVER = "capmx-driver"
MODEL_PROVIDER = "kimi-coding"
MODEL = "k3"
EXPECTED_TIER = "strong"


def emit(check: str, value: object) -> None:
    print(
        "EVIDENCE "
        + check
        + " "
        + json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        flush=True,
    )


def agent_by_name(listing: dict[str, Any], name: str) -> dict[str, Any]:
    agents = listing.get("agents")
    if not isinstance(agents, list):
        raise AssertionError(f"agent list returned no agents array: {listing}")
    matches = [
        row for row in agents
        if isinstance(row, dict) and row.get("actor") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"agent list did not contain exactly one {name!r}: {listing}")
    return matches[0]


def main() -> int:
    executable = hyprial_executable()
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    print(f"SUBJECT sha={sha} hyprial={executable}", flush=True)

    with scenario_tempdir(SCENARIO.lower(), directory=short_temp_root()) as root:
        daemon = IsolatedDaemon(executable, root, "capmx-controlled-node")
        daemon.env = sanitized_env(daemon.env)
        if Path(daemon.env["HYPRIAL_HOME"]).resolve() == (Path.home() / ".hyprial").resolve():
            raise AssertionError("isolated HYPRIAL_HOME resolved to production ~/.hyprial")

        with teardown_guard(daemon.stop):
            daemon.start()
            daemon.command("agent", "create", "--name", DRIVER, "--json")

            # Acceptance 1: public headless startup followed by public agent list.
            started = daemon.command(
                "agent", "start", "pi", "--name", HEADLESS, "--headless",
                "--provider", MODEL_PROVIDER, "--model", MODEL, "--json",
                timeout=90,
            )
            listing = daemon.command("agent", "list", "--json")
            headless = agent_by_name(listing, HEADLESS)
            headless_caps = headless.get("capabilities")
            if not isinstance(headless_caps, dict):
                raise AssertionError(f"headless capabilities are not JSON: {headless}")
            if headless_caps.get("interactive") is not False:
                raise AssertionError(f"headless interactive fact changed: {headless_caps}")
            if headless_caps.get("tier") != EXPECTED_TIER:
                raise AssertionError(f"headless tier changed: {headless_caps}")
            emit("headless-agent-list", {"start": started, "agent": headless})
            print(
                f"RESULT PASS {SCENARIO} headless-agent-list "
                f"interactive=false tier={EXPECTED_TIER}",
                flush=True,
            )

            # Acceptance 2: the real daemon's public interactive registration seam.
            registered = daemon.request(
                "session.register",
                {
                    "actor": INTERACTIVE,
                    "sessionRef": "capmx-controlled-session",
                    "cwd": str(ROOT),
                    "command": [
                        "pi", "--provider", MODEL_PROVIDER, "--model", MODEL,
                    ],
                    "source": "capmx-controlled-e2e",
                    "runtime": "pi_interactive",
                    "channelConfirmed": True,
                },
            )
            listing = daemon.command("agent", "list", "--json")
            interactive = agent_by_name(listing, INTERACTIVE)
            interactive_caps = interactive.get("capabilities")
            if not isinstance(interactive_caps, dict):
                raise AssertionError(f"interactive capabilities are not JSON: {interactive}")
            if interactive_caps.get("interactive") is not True:
                raise AssertionError(f"interactive registration fact changed: {interactive_caps}")
            if interactive_caps.get("tier") != EXPECTED_TIER:
                raise AssertionError(f"interactive tier changed: {interactive_caps}")
            emit(
                "interactive-agent-list",
                {"registration": registered, "agent": interactive},
            )
            print(
                f"RESULT PASS {SCENARIO} interactive-agent-list "
                f"interactive=true tier={EXPECTED_TIER}",
                flush=True,
            )

            # Acceptance 3: a role=execute taskbook is refused before run creation.
            workflow = root / "execute-to-interactive.yaml"
            workflow.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "name": "capmx-execute-interactive-refusal",
                        "task": "controlled dispatch gate proof",
                        "targets": [{"name": INTERACTIVE, "role": "execute"}],
                        "await": {"kind": "reply", "match": "ACCEPTED"},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            refused = daemon.command(
                "workflow", "run", str(workflow), "--from", DRIVER,
                "--yes", "--json", exits=(1,),
            )
            if refused.get("code") != "DISPATCH_ROLE_MISMATCH":
                raise AssertionError(f"execute->interactive returned wrong result: {refused}")
            if "交互会话只做 plan/dispatch/review" not in str(refused.get("error")):
                raise AssertionError(f"role mismatch guidance changed: {refused}")
            runs = daemon.command("workflow", "list", "--json")
            if runs.get("runs") != []:
                raise AssertionError(f"refused dispatch allocated a workflow run: {runs}")
            emit("execute-interactive-refusal", {"refusal": refused, "runs": runs})
            print(
                f"RESULT PASS {SCENARIO} execute-interactive-refusal "
                "code=DISPATCH_ROLE_MISMATCH runs=0",
                flush=True,
            )

            daemon.command("agent", "stop", "--all", "--json")

    print(
        f"RESULT PASS {SCENARIO} checks=3 isolated=1 fake_harness=0 cleanup=1 sha={sha}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"RESULT FAIL {SCENARIO} {error}", flush=True)
        raise SystemExit(1) from error
