"""At most two owned E2E-024 case processes, drained on every exit path.

Each worker has separate HOME/XDG roots. Only PID metadata from the owned
case roots is collected; worker failure or interruption stops all launched
workers and confirms their daemon and sidecar processes have exited.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support.runtime.environment import isolated_environment


MAX_CASE_WORKERS = 2


def _collect_pids(case_roots: list[Path], owned: dict[int, str]) -> None:
    for case in case_roots:
        for path in (case / "a/state/daemon.json", case / "b/state/daemon.json",
                     case / "forwarding-sidecar.pid", case / "forwarding-sidecar-b.pid"):
            try:
                text = path.read_text()
                pid = json.loads(text).get("pid") if path.suffix == ".json" else int(text)
            except (OSError, ValueError, TypeError):
                continue
            if type(pid) is int and pid > 1:
                owned[pid] = str(path.relative_to(case.parent))


def _result(log: Path, *, code: int, index: int, evidence_root: Path) -> dict:
    evidence = log.parent / f"case-{index}" / "forwarding-evidence.json"
    if evidence.is_file():
        evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        saved = evidence_root / f"case-{index}.json"
        shutil.copyfile(evidence, saved)
        print(f"RESULT evidence E2E-024 case-{index} full={saved}", flush=True)
    results = []
    for line in log.read_text().splitlines():
        print(line, flush=True)
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "peer-restart-path":
            results.append(event)
    if code != 0 or len(results) != 1 or not evidence.is_file():
        raise AssertionError(f"case-{index} failed: exit={code}, results={len(results)}, fullEvidence={evidence.is_file()}")
    return results[0]


def run_cases(root: Path, cases: tuple[tuple[str, float], ...], *, runner: Path,
              executable: str, stop_pid, interrupted, evidence_root: Path) -> list[dict]:
    case_roots = [root / f"case-{index}" for index in range(1, len(cases) + 1)]
    pending = list(enumerate(cases, start=1))
    active = {}
    workers = []
    owned: dict[int, str] = {}
    results = {}
    cleanup_errors = []
    try:
        while pending or active:
            if error := interrupted():
                raise error
            while pending and len(active) < MAX_CASE_WORKERS:
                if error := interrupted():
                    raise error
                index, (mode, delay) = pending.pop(0)
                log = root / f"case-{index}.worker.log"
                stream = log.open("w")
                env = isolated_environment(root / f"worker-{index}-environment")
                env["HYPRIAL_BIN"] = executable
                env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
                try:
                    process = subprocess.Popen(
                        [sys.executable, str(runner), "--case", str(case_roots[index - 1]), mode, str(delay)],
                        env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
                    )
                except BaseException:
                    stream.close()
                    raise
                worker = (index, process, stream, log)
                workers.append(worker)
                active[index] = worker
                (root / "case-workers.json").write_text(json.dumps(
                    [{"index": row[0], "pid": row[1].pid} for row in workers]
                ))
            _collect_pids(case_roots, owned)
            for index, (_, process, stream, log) in tuple(active.items()):
                code = process.poll()
                if code is not None:
                    stream.close()
                    results[index] = _result(log, code=code, index=index, evidence_root=evidence_root)
                    del active[index]
            if active:
                time.sleep(0.05)
        return [results[index] for index in range(1, len(cases) + 1)]
    finally:
        original_error = sys.exc_info()[1]
        _collect_pids(case_roots, owned)
        for _index, process, _stream, _log in workers:
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for index, process, stream, _log in workers:
            try:
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    _collect_pids(case_roots, owned)
                    process.kill()
                    process.wait(timeout=5)
                    cleanup_errors.append(f"case-{index} required forced worker shutdown")
            except BaseException as error:
                cleanup_errors.append(f"case-{index} worker: {type(error).__name__}: {error}")
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            finally:
                stream.close()
        _collect_pids(case_roots, owned)
        for pid, label in owned.items():
            try:
                stop_pid(pid, label=label)
            except BaseException as error:
                cleanup_errors.append(f"{label}: {type(error).__name__}: {error}")
        if cleanup_errors:
            if original_error is None:
                raise AssertionError(f"case cleanup failed: {cleanup_errors}")
            original_error.add_note(f"case cleanup failed: {cleanup_errors}")
            print(f"RESULT diagnostic E2E-024 case cleanup failed: {cleanup_errors}")
        if original_error is None and (error := interrupted()) is not None:
            raise error
