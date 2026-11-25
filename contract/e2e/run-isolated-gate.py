#!/usr/bin/env python3
"""Isolated-macos gate driver: every scenario runs, none rides on another.

E2E-001 used to be the first of twelve sequential workflow steps, so one red
there meant E2E-002..E2E-020 never ran and the gate produced NO reading for
them — for weeks (spec-e2e001, 2026-09-14).  The fix is deliberately NOT a
workflow-level `continue-on-error` / `if: always()` (hq-adjutant ruling,
追加 2): the scenario loop lives HERE, in the runner layer, with three
properties the workflow cannot express:

  1. every scenario executes and prints its own RESULT lines, whatever the
     previous one did;
  2. the summary row lists each scenario PASS / FAIL / SKIP, and ANY FAIL
     makes the driver exit 1 — later greens cannot dilute an earlier red;
  3. a scenario that depends on a predecessor's state is explicitly SKIPped
     with reason `前置失败` when that predecessor did not pass — never run
     into a half-dead environment to produce a fake green.

The per-scenario log files (diag/E2E-NNN.log) and steps.tsv rows keep the
exact names and formats the workflow's summary/collect steps already parse,
so nothing downstream of this driver changes shape.

Scenario set and order mirror the previous workflow steps one-for-one, with
E2E-009 joined here because it needs no credential and therefore must not ride
on the controlled gate's preflight.  Dependencies (currently none declared in
scenarios.json) live in DEPENDS_ON below.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import BYPASS_ENVIRONMENT

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "contract/e2e"

#: (scenario id, runner script) in execution order — the same order the
#: per-scenario workflow steps used, so timings stay comparable.
SCENARIOS: tuple[tuple[str, str], ...] = (
    ("E2E-001", "run-two-machine-roundtrip.py"),
    ("E2E-002", "run-daemon-restart-survival.py"),
    ("E2E-003", "run-pin-restart-survival.py"),
    ("E2E-005", "run-uv-install-flow.py"),
    ("E2E-007", "run-four-lark-adapters.py"),
    ("E2E-009", "run-first-run-no-agent.py"),
    ("E2E-010", "run-upgrade-continuity.py"),
    ("E2E-011", "run-lark-app-onboarding.py"),
    ("E2E-015", "run-pac-workflow.py"),
    ("E2E-016", "run-selfdrive-routine.py"),
    ("E2E-017", "run-outbox-deadletter.py"),
    ("E2E-018", "run-pac-graph-slice.py"),
    ("E2E-020", "run-pac-slice-2.py"),
    ("E2E-021", "run-orgfs-forwarding-redial.py"),
    ("E2E-024", "run-peer-restart-relearn.py"),
    ("E2E-028", "run-remote-drop-no-rebuild.py"),
    ("E2E-029", "run-tailcat-org-admission-deny.py"),
    ("E2E-030", "run-tailcat-org-join-resume.py"),
    ("E2E-031", "run-group-conversation.py"),
)

#: Scenarios whose environment is left behind by an earlier scenario.  A
#: scenario here runs only when every named dependency PASSED; otherwise it
#: is reported SKIP 前置失败 and never started (no fake greens, 追加 2.3).
#: scenarios.json declares no dependencies today — the mechanism is here so
#: the first real dependency lands as data, not as new control flow.
DEPENDS_ON: dict[str, tuple[str, ...]] = {}

#: The per-scenario workflow steps did NOT all export the same environment:
#: every step but E2E-005 and E2E-010 set HYPRIAL_BIN.  The driver must keep
#: reproducing that difference exactly (批复 §3a) — a scenario that never
#: saw HYPRIAL_BIN must not start seeing it just because the gate is now one
#: process.  Pinned by test_env_differences_match_the_old_workflow_steps.
NO_HYPRIAL_BIN = frozenset({"E2E-005", "E2E-010"})


def diag_dir() -> Path:
    """Where per-scenario logs and steps.tsv go: the run-scoped diag/ root
    under CI, a scratch directory for standalone host runs."""

    root = os.environ.get("HYPRIAL_E2E_ROOT")
    if root:
        return Path(root) / "diag"
    return Path(tempfile.gettempdir()) / "hyprial-e2e-gate-diag"


class StepsLog:
    """steps.tsv rows in the format the workflow's summary step parses:
    ``seq\\tstep\\trc\\ttook`` (seq maintained per file)."""

    def __init__(self, *paths: Path) -> None:
        self._paths = [path for path in paths if path is not None]

    def append(self, step: str, rc: int, took_seconds: float) -> None:
        for path in self._paths:
            try:
                sequence = 0
                if path.exists():
                    with path.open("r", encoding="utf-8") as handle:
                        sequence = sum(1 for _ in handle)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{sequence + 1}\t{step}\t{rc}\t{int(took_seconds)}s\n")
            except OSError:
                # The trace file is best-effort diagnostics; a write failure
                # must never turn a scenario verdict into a driver crash.
                continue


#: Gate exit code when every scenario that ran passed but some refused to run.
INCOMPLETE_EXIT = 4


def scenario_environment(scenario: str) -> dict[str, str]:
    """The child's environment: this process's environment, with the
    per-scenario differences the old workflow steps had (批复 §3a)."""

    environment = dict(os.environ)
    # No isolation bypass ever travels through the gate, whatever put it in
    # this process's environment (workflow env, runner, launchd, wrapper).
    for name in BYPASS_ENVIRONMENT:
        environment.pop(name, None)
    if scenario in NO_HYPRIAL_BIN:
        environment.pop("HYPRIAL_BIN", None)
    return environment


def run_scenario(scenario: str, script: str, *, log_path: Path, echo) -> int:
    """Run one scenario runner, streaming its output to the log and stdout.

    Under CI the inherited environment is exactly what the per-scenario
    workflow steps used to set (HYPRIAL_HOME, proxies, ...); the only
    per-step difference — HYPRIAL_BIN — is restored per scenario above, so
    scenario behavior does not depend on who launched the gate.
    """

    with subprocess.Popen(
        [sys.executable, str(SCENARIO_DIR / script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(ROOT),
        env=scenario_environment(scenario),
    ) as process, log_path.open("w", encoding="utf-8") as handle:
        assert process.stdout is not None
        for line in process.stdout:
            handle.write(line)
            handle.flush()
            echo(line)
        return process.wait()


def precompile_bytecode_targets() -> list[Path]:
    """The trees whose first import would otherwise compile inside a budget.

    CI builds the gate venv with ``uv sync`` (no ``--compile-bytecode``) from
    a fresh checkout, so the FIRST daemon any scenario starts pays the whole
    compile of hyprial and its dependencies before its first log line.  On
    2026-09-27 that was E2E-002: ``hyprial init`` waited 15 s, the daemon
    logged its first line at ~15 s and was ready 0.65 s later (task 35833,
    loadavg ~15.7).  Measured locally from a fresh CI-shaped venv: ~8 s of
    compile on the first run, none after.
    """

    targets = [ROOT / "src"]
    binary = os.environ.get("HYPRIAL_BIN")
    venv = Path(binary).resolve().parent.parent if binary else Path(sys.prefix)
    targets.extend(sorted(venv.glob("lib/python*/site-packages")))
    return [target for target in targets if target.is_dir()]


def precompile_bytecode(echo) -> None:
    """Compile once, before any scenario starts a daemon.  Best effort: a
    failure here is reported and never changes a scenario verdict.

    Runs ``python -m compileall -j 0`` in a CHILD process: the parallel
    worker pool (and its pipes) lives and dies there, so nothing leaks into
    the gate process -- or into a test that calls ``main()``, where the
    suite's fd guard would count a lingering pool pipe (task 36113)."""

    started = time.monotonic()
    try:
        targets = precompile_bytecode_targets()
        if not targets:
            echo("RESULT NOTE gate precompile skipped: no targets\n")
            return
        completed = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "-j", "0", *map(str, targets)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
            check=False,
        )
        echo(
            f"RESULT NOTE gate precompile {'ok' if completed.returncode == 0 else 'partial'} "
            f"targets={len(targets)} seconds={time.monotonic() - started:.1f}\n"
        )
    except Exception as error:  # noqa: BLE001 - a warm-up must never fail the gate
        echo(f"RESULT NOTE gate precompile skipped: {type(error).__name__}: {error}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenarios",
        nargs="*",
        help="scenario ids to run (default: all, in gate order)",
    )
    args = parser.parse_args(argv)
    known = dict(SCENARIOS)
    selected = args.scenarios or list(known)
    unknown = [item for item in selected if item not in known]
    if unknown:
        parser.error(f"unknown scenario ids: {', '.join(unknown)}")

    diag = diag_dir()
    diag.mkdir(parents=True, exist_ok=True)
    run_id = os.environ.get("GITHUB_RUN_ID")
    steps = StepsLog(diag / "steps.tsv")
    if run_id:
        steps = StepsLog(diag / "steps.tsv", Path(f"/tmp/hyprial-e2e-trace-{run_id}.tsv"))

    def echo(line: str) -> None:
        sys.stdout.write(line)
        sys.stdout.flush()

    precompile_bytecode(echo)

    verdicts: dict[str, str] = {}
    interrupted = False
    for scenario, script in SCENARIOS:
        if scenario not in selected:
            continue
        unmet = [
            dependency
            for dependency in DEPENDS_ON.get(scenario, ())
            if verdicts.get(dependency) != "PASS"
        ]
        if unmet:
            verdicts[scenario] = "SKIP"
            print(
                f"RESULT SKIP {scenario} 前置失败:{','.join(unmet)} "
                f"({script} not started)"
            )
            steps.append(scenario, 3, 0.0)  # 3 = NOT-RUN in the gate vocabulary
            continue
        started = time.monotonic()
        try:
            returncode = run_scenario(
                scenario, script, log_path=diag / f"{scenario}.log", echo=echo
            )
        except KeyboardInterrupt:
            interrupted = True
            verdicts[scenario] = "FAIL"
            steps.append(scenario, 130, time.monotonic() - started)
            print(f"RESULT FAIL {scenario} interrupted")
            break
        # Exit 3 = the scenario refused to start on this machine: it would
        # run a daemon that cannot be network-isolated (docs/network-
        # isolation.md; E2E-005/E2E-010 start released builds that predate
        # HYPRIAL_NETWORK_ISOLATED).  Shown by name, never folded into FAIL:
        # a FAIL here would make HYPRIAL_E2E_ALLOW_UNISOLATED_DAEMON=1 the
        # cheapest way to a green gate, defeating the refusal it overrides.
        verdicts[scenario] = (
            "PASS" if returncode == 0 else "REFUSED" if returncode == 3 else "FAIL"
        )
        steps.append(scenario, returncode, time.monotonic() - started)

    summary = " ".join(
        f"{scenario}={verdicts.get(scenario, 'NOT-RUN')}"
        for scenario, _ in SCENARIOS
        if scenario in selected
    )
    if any(value == "FAIL" for value in verdicts.values()):
        print(f"RESULT FAIL GATE isolated-macos {summary}")
        return 1
    if interrupted:
        print(f"RESULT ABORTED GATE isolated-macos {summary}")
        return 130
    refused = [scenario for scenario, value in verdicts.items() if value == "REFUSED"]
    if refused:
        # Not PASS: those scenarios did not run.  Not FAIL: nothing that ran
        # failed.  Its own line and exit code keep "not run" and "ran and
        # broke" apart (hq-adjutant 2026-09-23).
        print(
            f"RESULT INCOMPLETE GATE isolated-macos {summary} "
            f"(REFUSED on this machine, not run: {','.join(refused)})"
        )
        return INCOMPLETE_EXIT
    print(f"RESULT PASS GATE isolated-macos {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
