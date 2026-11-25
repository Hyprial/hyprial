#!/usr/bin/env python3
"""E2E-009: a clean first run creates no agent and no messaging configuration.

``hyprial init`` on a disposable home creates the home, starts the daemon and
stops there: no agent is registered, no adapter is configured and no adapter
request is issued.  The retired ``hyprial init onboarding`` group accepts
neither its subcommands nor any of the old step ids; every such argv exits 2
with no business result before any daemon contact.

This scenario needs no App credential, tenant authorization, harness login or
model reply, and it never reads a production home.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (
    IsolatedDaemon,
    hyprial_executable,
    launch_result_is_ready,
    scenario_tempdir,
)

SCENARIO = "E2E-009"
NODE = "e2e-node"
FIRST_RUN_NAMES = ("squire", "assistant", "default", "worker")
OLD_STEP_IDS = (
    "runtime-ready",
    "default-agent",
    "worker-agent",
    "squire",
    "lark-app",
    "lark-authorize",
    "lark-route",
    "lark-pin",
    "lark-roundtrip",
)
RETIRED_COMMANDS = (
    ("init", "onboarding"),
    ("init", "onboarding", "plan", "--json"),
    ("init", "onboarding", "apply", "--auto", "--json"),
    ("init", "onboarding", "harnesses", "--json"),
    ("init", "onboarding", "harness-select", "pi", "--json"),
    ("init", "onboarding", "harness-install", "pi", "--consent", "--json"),
)


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str) -> None:
        self.checks.append((status, name, detail))
        print(f"{status} {SCENARIO} {name} {detail}", flush=True)

    def finish(self) -> int:
        counts = {
            status: sum(check[0] == status for check in self.checks)
            for status in ("PASS", "FAIL", "BLOCKED")
        }
        overall = "FAIL" if counts["FAIL"] else ("BLOCKED" if counts["BLOCKED"] else "PASS")
        print(
            f"RESULT {overall} {SCENARIO} first-run-no-agent "
            f"pass={counts['PASS']} fail={counts['FAIL']} "
            f"blocked={counts['BLOCKED']}",
            flush=True,
        )
        return 1 if counts["FAIL"] else (2 if counts["BLOCKED"] else 0)


def _raw(
    hyprial: Path, env: dict[str, str], *argv: str, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(hyprial), *argv],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
    )


def _agents(daemon: IsolatedDaemon) -> list[dict[str, object]]:
    payload = daemon.command("agent", "list", "--json")
    agents = payload.get("agents")
    if not isinstance(agents, list):
        raise AssertionError(f"agent list returned no agents array: {payload}")
    return [row for row in agents if isinstance(row, dict)]


def _adapters(daemon: IsolatedDaemon) -> list[dict[str, object]]:
    payload = daemon.command("adapter", "list", "--json")
    adapters = payload.get("adapters")
    if not isinstance(adapters, list):
        raise AssertionError(f"adapter list returned no adapters array: {payload}")
    return [row for row in adapters if isinstance(row, dict)]


def _assert_nothing_was_created(daemon: IsolatedDaemon, label: str) -> None:
    agents = _agents(daemon)
    if agents:
        names = sorted(str(row.get("actor") or row.get("uri") or row) for row in agents)
        raise AssertionError(f"{label}: init created agent(s): {names}")
    adapters = _adapters(daemon)
    if adapters:
        names = sorted(str(row.get("name")) for row in adapters)
        raise AssertionError(f"{label}: init configured adapter(s): {names}")
    listed = _raw(
        hyprial_executable(), daemon.env, "agent", "list", "--include-user-agent", "--json"
    )
    payload = json.loads(listed.stdout)
    rows = payload.get("agents") if isinstance(payload, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        for value in (row.get("actor"), row.get("uri")):
            if isinstance(value, str) and value.rsplit(":", 1)[-1] in FIRST_RUN_NAMES:
                raise AssertionError(f"{label}: first-run agent {value!r} exists")


def _assert_retired_surface(hyprial: Path, env: dict[str, str]) -> None:
    for argv in RETIRED_COMMANDS:
        completed = _raw(hyprial, env, *argv)
        if completed.returncode != 2:
            raise AssertionError(
                f"hyprial {' '.join(argv)} exited {completed.returncode}, expected 2"
            )
        if argv[-1] == "--json":
            payload = json.loads(completed.stdout)
            if payload != {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "error": "No such command 'onboarding'.",
            }:
                raise AssertionError(
                    f"hyprial {' '.join(argv)} returned {payload!r}"
                )
        else:
            if completed.stdout != "":
                raise AssertionError(
                    f"hyprial {' '.join(argv)} wrote stdout: {completed.stdout!r}"
                )
            if "No such command 'onboarding'" not in (completed.stderr or ""):
                raise AssertionError(
                    f"hyprial {' '.join(argv)} did not report the retired group: "
                    f"{completed.stderr[-500:]!r}"
                )
    for step in OLD_STEP_IDS:
        argv = ("init", "onboarding", "apply", step, "--json")
        completed = _raw(hyprial, env, *argv)
        payload = json.loads(completed.stdout) if completed.stdout else None
        if (
            completed.returncode != 2
            or not isinstance(payload, dict)
            or payload.get("ok") is not False
            or payload.get("code") != "INVALID_ARGUMENT"
        ):
            raise AssertionError(
                f"old step id {step!r} was accepted (exit {completed.returncode}, "
                f"stdout {completed.stdout!r})"
            )


def run(report: Report, root: Path, hyprial: Path) -> None:
    daemon = IsolatedDaemon(hyprial, root, NODE)
    try:
        started = daemon.start()
        if not launch_result_is_ready(started):
            raise AssertionError(f"init did not report a ready daemon: {started}")
        _assert_nothing_was_created(daemon, "first init")
        report.add("PASS", "first-run.no-agent", "clean init registered no agent and no adapter")

        again = daemon.command("init", "--no-service", "--json")
        if again.get("alreadyRunning") is not True:
            raise AssertionError(f"a second init did not reuse the daemon: {again}")
        _assert_nothing_was_created(daemon, "second init")
        report.add("PASS", "first-run.idempotent", "a repeated init created nothing")

        daemon.stop()
        restart = daemon.start()
        if not launch_result_is_ready(restart):
            raise AssertionError(f"restart did not report a ready daemon: {restart}")
        _assert_nothing_was_created(daemon, "daemon restart")
        report.add("PASS", "first-run.after-restart", "a fresh daemon restore created nothing")

        _assert_retired_surface(hyprial, daemon.env)
        report.add(
            "PASS",
            "first-run.retired-surface",
            "every retired onboarding command and step id exits 2 with no business result",
        )
    finally:
        daemon.stop()


def main() -> int:
    report = Report()
    hyprial = hyprial_executable()
    with scenario_tempdir("e2e-009") as temporary:
        run(report, temporary, hyprial)
    return report.finish()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, json.JSONDecodeError) as error:
        print(f"RESULT FAIL {SCENARIO} first-run-no-agent {error}")
        raise SystemExit(1) from error
