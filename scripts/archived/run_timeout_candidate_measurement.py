#!/usr/bin/env python3
"""Run one timeout candidate in one quota-matched container.

This is intentionally a host-side safety orchestrator.  Per the 2026-09-14
follow-up 11 it measures opportunistically: the start/continue criterion is the
runner-side one — every runner docker-in-docker container must answer a
rc-checked ``docker ps`` (via ``DOCKER_HOST=tcp://127.0.0.1:2375``) showing zero
``FORGEJO-ACTIONS-TASK`` job containers, and the host must show no other
measurement container.  Any read error is fail-closed as "unreadable": no start,
and a running attempt is stopped by its exact container ID.  The Forgejo
tasks/runs APIs are recorded as reference only; they no longer gate.

Run ``gate-probe`` while a CI job is known to be running and archive the output
before trusting any all-empty reading (positive control, follow-up 11).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time
from typing import Any

TASKS_API = "repos/HyprialOS/harness-bridge/actions/tasks?limit=30"
RUNS_API = "repos/HyprialOS/harness-bridge/actions/runs?limit=30"
ACTIVE_STATUSES = {
    "running",
    "waiting",
    "queued",
    "pending",
    "requested",
    "in_progress",
}
POLL_SECONDS = 60.0
MAX_CONSECUTIVE_FAILURES = 5
IMAGE = "python:3.12-bookworm"
DIND_NAME_FILTER = "docker-in-docker"
JOB_NAME_FILTER = "FORGEJO-ACTIONS-TASK"
MEASUREMENT_NAME_PREFIX = "hyprial-timeout-"
FIRST_BATCH = (
    "daemon-ready-15s",
    "home-guard-15s",
    "e2e-process-tree-420s",
)
TARGETS = {
    "daemon-ready-15s": (
        "tests/test_daemon_ipc_resilience.py::"
        "test_real_init_returns_before_delayed_adapter_is_ready_then_converges_online"
    ),
    "home-guard-15s": (
        "tests/test_home_guard.py::"
        "test_two_daemons_share_one_home_then_real_exit_allows_takeover"
    ),
}


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("coordination times must include a UTC offset")
    return parsed


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _record(path: Path, event: str, **fields: object) -> None:
    row = {"at": _now().isoformat(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()


def _api_reference(endpoint: str) -> list[dict[str, Any]]:
    process = subprocess.run(
        ["tea", "api", "--login", "code.hyprial.com", endpoint],
        check=True,
        capture_output=True,
        text=True,
        # Reference only (follow-up 11); keep failures cheap.  The runs
        # endpoint has been observed to hang for minutes — a short timeout is
        # recorded as api-error instead of stalling the 60s gate cadence.
        timeout=15,
    )
    rows = json.loads(process.stdout)["workflow_runs"]
    return [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "status": row.get("status"),
            "head_branch": row.get("head_branch"),
        }
        for row in rows
        if row.get("status") in ACTIVE_STATUSES
    ]


def _api_reference_state() -> dict[str, object]:
    """tasks/runs API summaries for the log only; failures never gate."""
    state: dict[str, object] = {}
    for name, endpoint in (("tasks", TASKS_API), ("runs", RUNS_API)):
        try:
            state[name] = _api_reference(endpoint)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            state[name] = f"api-error:{type(error).__name__}"
    return state


def _host_containers() -> list[tuple[str, str]]:
    process = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}} {{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    rows: list[tuple[str, str]] = []
    for line in process.stdout.splitlines():
        cid, _, name = line.strip().partition(" ")
        if cid:
            rows.append((cid, name))
    return rows


def _gate_state(own_container_id: str = "") -> dict[str, Any]:
    """Follow-up 11 criterion; every unreadable reading is fail-closed.

    ``idle`` is True only when every runner dind answers rc=0 with zero
    ``FORGEJO-ACTIONS-TASK`` containers and the host shows no measurement
    container other than ``own_container_id``.
    """
    state: dict[str, Any] = {"idle": False, "dind": [], "other_measurement": []}
    try:
        host = _host_containers()
    except (OSError, subprocess.SubprocessError) as error:
        state["host_error"] = type(error).__name__
        return state
    dinds = [(cid, name) for cid, name in host if DIND_NAME_FILTER in name]
    state["other_measurement"] = [
        {"id": cid, "name": name}
        for cid, name in host
        if name.startswith(MEASUREMENT_NAME_PREFIX)
        and not (own_container_id and cid == own_container_id[: len(cid)])
    ]
    if not dinds:
        state["dind_error"] = "no docker-in-docker containers enumerated"
        return state
    idle = not state["other_measurement"]
    for cid, name in dinds:
        try:
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    cid,
                    "sh",
                    "-c",
                    "DOCKER_HOST=tcp://127.0.0.1:2375 docker ps"
                    f" --filter name={JOB_NAME_FILTER}"
                    " --format '{{.ID}} {{.Names}}'",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            reading = {
                "dind": name,
                "rc": probe.returncode,
                "jobs": probe.stdout.strip().splitlines(),
                "stderr": probe.stderr.strip()[:200],
            }
        except (OSError, subprocess.SubprocessError) as error:
            reading = {"dind": name, "rc": None, "error": type(error).__name__}
        state["dind"].append(reading)
        if reading.get("rc") != 0 or reading.get("jobs"):
            idle = False
    state["idle"] = idle
    return state


def _record_gate(path: Path, event: str, state: dict[str, Any]) -> None:
    _record(path, event, gate=state, api_reference=_api_reference_state())


def _within_window(start: datetime | None, end: datetime | None) -> bool:
    if start is None or end is None:
        return True
    return start <= _now() <= end


def _wait_for_gate(start: datetime | None, end: datetime | None, log: Path) -> None:
    paused = False
    while True:
        if not _within_window(start, end):
            raise RuntimeError("outside the dispatcher-supplied measurement window")
        state = _gate_state()
        if state["idle"]:
            _record_gate(log, "RESUME" if paused else "IDLE_CONFIRMED", state)
            return
        paused = True
        _record_gate(log, "PAUSE", state)
        time.sleep(POLL_SECONDS)


def _container_command(candidate: str, count: int) -> str:
    setup = (
        "python -m pip install --quiet uv && "
        "UV_PROJECT_ENVIRONMENT=/venv UV_CACHE_DIR=/uv-cache "
        "uv sync --frozen --extra test --project /workspace"
    )
    if candidate in TARGETS:
        target = TARGETS[candidate]
        measure = (
            "rm -f /results/measurements.jsonl; "
            f"for iteration in $(seq 0 {count - 1}); do "
            f"HYPRIAL_TIMEOUT_MEASURE_CANDIDATE={candidate} "
            "HYPRIAL_TIMEOUT_MEASURE_ITERATION=$iteration "
            "HYPRIAL_TIMEOUT_MEASURE_RESULTS=/results/measurements.jsonl "
            "/venv/bin/python -m pytest -q -s -p no:cacheprovider "
            "-p scripts.timeout_candidate_measurement "
            f"--basetemp=/tmp/pytest-$iteration {target}; "
            "done"
        )
    else:
        measure = (
            "rm -f /results/measurements.jsonl; "
            "HYPRIAL_TIMEOUT_MEASURE_RESULTS=/results/measurements.jsonl "
            "/venv/bin/python scripts/timeout_candidate_measurement.py "
            f"e2e-process-tree --count {count} --probe-budget 0.05 "
            "--descendant-hold 0.20"
        )
    verify = (
        "; /venv/bin/python -c \"import json,pathlib; "
        "p=pathlib.Path('/results/measurements.jsonl'); "
        f"rows=[json.loads(x) for x in p.read_text().splitlines()]; "
        f"assert len(rows)=={count}, len(rows)\""
    )
    return setup + " && { " + measure + verify + "; }"


def _remove_volume(name: str, log: Path) -> None:
    process = subprocess.run(
        ["docker", "volume", "rm", "-f", name],
        check=False,
        capture_output=True,
        text=True,
    )
    _record(log, "VOLUME_REMOVED", volume=name, returncode=process.returncode)


def _summarize(results: Path, candidate: str, count: int) -> dict[str, object]:
    readings = [
        json.loads(line)
        for line in (results / "measurements.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(readings) != count:
        raise RuntimeError(f"expected {count} readings, found {len(readings)}")
    elapsed = sorted(float(row["elapsed_seconds"]) for row in readings)
    budget_key = (
        "probe_budget_seconds"
        if candidate == "e2e-process-tree-420s"
        else "budget_seconds"
    )
    budget = float(readings[0][budget_key])
    summary: dict[str, object] = {
        "candidate": candidate,
        "count": count,
        "budget_seconds": budget,
        "p50_seconds": statistics.median(elapsed),
        "p90_seconds": elapsed[math.ceil(0.90 * count) - 1],
        "max_seconds": elapsed[-1],
        "over_budget": sum(value > budget for value in elapsed),
    }
    (results / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _container_exists(container_id: str) -> bool:
    residue = subprocess.run(
        ["docker", "ps", "-a", "--quiet", "--filter", f"id={container_id}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bool(residue)


def _run_attempt(
    candidate: str,
    count: int,
    results: Path,
    log: Path,
    start: datetime | None,
    end: datetime | None,
    attempt: int,
) -> str:
    repo = Path(__file__).resolve().parents[1]
    cidfile = results / f"attempt-{attempt}.cid"
    output = results / f"attempt-{attempt}.container.log"
    name_prefix = f"{MEASUREMENT_NAME_PREFIX}{os.getpid()}-{attempt}"
    venv_volume = name_prefix + "-venv"
    cache_volume = name_prefix + "-cache"
    state = _gate_state()
    _record_gate(log, "PRESTART_CHECK", state)
    if not state["idle"]:
        _record_gate(log, "PAUSE", state)
        return "retry"

    created_volumes: list[str] = []
    try:
        for volume in (venv_volume, cache_volume):
            subprocess.run(
                ["docker", "volume", "create", volume],
                check=True,
                capture_output=True,
            )
            created_volumes.append(volume)
    except BaseException:
        for volume in created_volumes:
            _remove_volume(volume, log)
        raise
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name_prefix,
        "--platform=linux/arm64",
        "--cpus=2",
        "--memory=4g",
        "--cidfile",
        str(cidfile),
        "-v",
        f"{repo}:/workspace:ro",
        "-v",
        f"{results}:/results",
        "-v",
        f"{venv_volume}:/venv",
        "-v",
        f"{cache_volume}:/uv-cache",
        "-w",
        "/workspace",
        IMAGE,
        "bash",
        "-lc",
        _container_command(candidate, count),
    ]
    _record(
        log,
        "CONTAINER_START",
        candidate=candidate,
        attempt=attempt,
        container_name=name_prefix,
        platform="linux/arm64",
        cpus=2,
        memory="4g",
        count=count,
    )
    try:
        with output.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
    except BaseException:
        for volume in created_volumes:
            _remove_volume(volume, log)
        raise
    next_poll = time.monotonic() + POLL_SECONDS
    try:
        while process.poll() is None:
            delay = next_poll - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_poll += POLL_SECONDS
            if process.poll() is not None:
                break
            own_cid = (
                cidfile.read_text(encoding="utf-8").strip() if cidfile.exists() else ""
            )
            if not _within_window(start, end):
                state = {"idle": False, "reason": "coordination-window-ended"}
            else:
                state = _gate_state(own_container_id=own_cid)
            _record_gate(log, "POLL", state)
            if not state["idle"]:
                if not cidfile.exists():
                    raise RuntimeError("container ID unavailable during required stop")
                cid = cidfile.read_text(encoding="utf-8").strip()
                _record(log, "PAUSE", container_id=cid, gate=state)
                # The container may exit on its own while the (multi-second)
                # gate check runs; ``--rm`` then removes it before this stop.
                # "No such container" therefore means the goal state, but a
                # stop that fails for any other reason must not drop the guard.
                stop = subprocess.run(
                    ["docker", "stop", cid], check=False, capture_output=True, text=True
                )
                process.wait()
                if _container_exists(cid):
                    time.sleep(2.0)  # let a concurrent --rm cleanup finish
                    if _container_exists(cid):
                        raise RuntimeError(
                            f"pause could not stop container {cid}: {stop.stderr.strip()}"
                        )
                partial = results / "measurements.jsonl"
                if partial.exists():
                    shutil.move(partial, results / f"attempt-{attempt}.partial.jsonl")
                return "retry"
        returncode = process.wait()
        _record(log, "CONTAINER_END", attempt=attempt, returncode=returncode)
        if returncode != 0:
            # Transient setup failures (e.g. a pip index hiccup) must not kill
            # an unattended overnight run; the caller bounds the retries.
            _record(log, "CONTAINER_FAILED", attempt=attempt, returncode=returncode,
                    container_log=str(output))
            return "failed"
        state = _gate_state()
        _record_gate(log, "POSTRUN_CHECK", state)
        if not state["idle"]:
            _record_gate(log, "PAUSE", state)
            completed = results / "measurements.jsonl"
            if completed.exists():
                shutil.move(completed, results / f"attempt-{attempt}.partial.jsonl")
            return "retry"
        return "complete"
    finally:
        if process.poll() is None and cidfile.exists():
            # Stop only the exact ID written by docker; never use a name pattern.
            subprocess.run(
                ["docker", "stop", cidfile.read_text(encoding="utf-8").strip()],
                check=False,
            )
            process.wait()
        for volume in created_volumes:
            _remove_volume(volume, log)
        cid = cidfile.read_text(encoding="utf-8").strip() if cidfile.exists() else ""
        residue = subprocess.run(
            ["docker", "ps", "-a", "--quiet", "--filter", f"id={cid}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _record(log, "RESIDUE_CHECK", container_id=cid, residue=residue)
        if residue:
            raise RuntimeError(f"--rm container residue remains: {residue}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "candidate",
        choices=(*FIRST_BATCH, "all-first-batch", "gate-probe"),
    )
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--window-start", type=_parse_time, default=None)
    parser.add_argument("--window-end", type=_parse_time, default=None)
    parser.add_argument("--results-dir", type=Path, default=None)
    args = parser.parse_args()
    if args.candidate == "gate-probe":
        # One rc-checked reading of the runner-side criterion, for the
        # positive-control archive and for ad-hoc idle checks.
        print(json.dumps({"gate": _gate_state(), "api_reference": _api_reference_state()}, indent=2, sort_keys=True))
        return
    if args.results_dir is None:
        parser.error("--results-dir is required for measurement candidates")
    if args.count < 30:
        parser.error("container candidate distributions require N >= 30")
    if (args.window_start is None) != (args.window_end is None):
        parser.error("window start and end must be supplied together")
    if args.window_end is not None and args.window_end <= args.window_start:
        parser.error("window end must be after its start")
    args.results_dir.mkdir(parents=True, exist_ok=True)
    candidates = FIRST_BATCH if args.candidate == "all-first-batch" else (args.candidate,)
    for candidate in candidates:
        results = (
            args.results_dir / candidate
            if args.candidate == "all-first-batch"
            else args.results_dir
        )
        results.mkdir(parents=True, exist_ok=True)
        log = results / "control.jsonl"
        if log.exists():
            parser.error(f"results directory already contains {log}")

        attempt = 1
        consecutive_failures = 0
        while True:
            _wait_for_gate(args.window_start, args.window_end, log)
            outcome = _run_attempt(
                candidate,
                args.count,
                results,
                log,
                args.window_start,
                args.window_end,
                attempt,
            )
            if outcome == "complete":
                summary = _summarize(results, candidate, args.count)
                _record(
                    log,
                    "COMPLETE",
                    candidate=candidate,
                    count=args.count,
                    summary=summary,
                )
                break
            attempt += 1
            if outcome == "failed":
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"{candidate}: {consecutive_failures} consecutive failed "
                        "attempts; see the newest attempt-*.container.log"
                    )
            else:
                consecutive_failures = 0


if __name__ == "__main__":
    main()
