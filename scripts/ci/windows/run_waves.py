"""Run Windows pytest waves with process-tree and ledger accounting.

Decision (2026-11-26, tier 1 smoke and tier 2 full suite).  The wrapper is the
single owner of the fixed pytest arguments, watchdog loop, Job Object teardown,
and manifest output.  Ten-state classification, ledger parsing, plan loading, and
JUnit reconciliation live in scripts/ci/windows/wave_state.py so every script
stays well under 1000 lines.  Argsfiles carry file paths only (never nodeids);
partial-file reruns pass --deselect on argv so union XMLs stay duplicate-free.
Tier 1 enforces strict contract and non-contract skip rules; tier 2 executes
multi-wave shard plans from plan-manifest.json, forwards HYPRIAL_TEST_ACL_PROBE=1
when --acl-probe or WINDOWS_SUITE_ACL_PROBE is enabled, and keeps full ten-state
hard-red classification without failing on legitimate conditional skips.  Windows
uses a kill-on-close Job Object without BREAKAWAY_OK via _winapi.CreateProcess
with CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP, while Linux keeps a real
subprocess path so self-tests exercise the wrapper directly.

Decision (2026-11-27, tier 2 full suite).  The manifest key collectionSkippedFiles
lists, with a count, every planned file reported only through a collection-time
module skip (the four-condition rule in wave_state.py), so the summary can show it.

Decision (2026-11-27, tier 2 full suite).  A repeated nodeid in the XML of one
attempt is still a hard red that nothing offsets, but it no longer stops the
waves after it.  Stopping only lost evidence: every later wave of the shard went
unrun while the status was already red.  The attempt that raised is recorded as
unreported-files with the raised text in details, its files stay pending for the
next attempt under the ordinary rerun rule, and the wave loop goes on to the
next wave after the last attempt.

The manifest keeps the two kinds of duplicate apart.  duplicateRuns lists every
attempt that raised inside the wave loop, each as wave, run and the raised text.
duplicateUnion holds what the final read of all attempt XMLs together raised.
duplicateResult is the first entry of duplicateRuns, else duplicateUnion, so a
union duplicate cannot hide a run-time one; summarize.py rejects each of the three.

When the final read raises, the strict skip checks that follow it are skipped.
They only ever turn a passed status into failed, and the status cannot be passed
at that point: the raise itself sets it to unreported-files unless it is already
a hard state.  That is the state in which they are skipped, and it is a hard
red whatever they would have found.  The files still pending are reported
through unreported.xml as in every other case.  The assignment is a guard, not
a change of outcome: a wave is rerun only while one of its files is unfinished,
and the attempt that leaves a file unfinished is always a hard red, so whenever
the final read raises the status is already a hard red.  It is written down as
code because that order is a property of the rerun rule, not of this branch.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.ci.windows.wave_state import (
        CONTRACT_TEST_FILE,
        EXPECTED_CONTRACT_SKIPS,
        HARD_STATES,
        JUnitResult,
        RunOutcome,
        classify_run,
        is_offsettable,
        load_planned_waves,
        merge_wave_status,
        read_junit,
        read_jsonl,
        synthesize_unreported,
    )
except ModuleNotFoundError:
    _wave_state = importlib.import_module("wave_state")
    CONTRACT_TEST_FILE = _wave_state.CONTRACT_TEST_FILE
    EXPECTED_CONTRACT_SKIPS = _wave_state.EXPECTED_CONTRACT_SKIPS
    HARD_STATES = _wave_state.HARD_STATES
    JUnitResult = _wave_state.JUnitResult
    RunOutcome = _wave_state.RunOutcome
    classify_run = _wave_state.classify_run
    is_offsettable = _wave_state.is_offsettable
    load_planned_waves = _wave_state.load_planned_waves
    merge_wave_status = _wave_state.merge_wave_status
    read_junit = _wave_state.read_junit
    read_jsonl = _wave_state.read_jsonl
    synthesize_unreported = _wave_state.synthesize_unreported

__all__ = [
    "CONTRACT_TEST_FILE",
    "EXPECTED_CONTRACT_SKIPS",
    "HARD_STATES",
    "JUnitResult",
    "RunOutcome",
    "build_pytest_command",
    "classify_run",
    "is_offsettable",
    "main",
    "read_junit",
    "read_jsonl",
    "run_waves",
    "synthesize_unreported",
    "validate_durations",
]

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def validate_durations(
    watchdog_seconds: int,
    idle_seconds: int,
    wave_minutes: int,
    deadline_minutes: int,
    step_minutes: int,
    job_minutes: int,
) -> None:
    values = (
        watchdog_seconds,
        idle_seconds,
        wave_minutes * 60,
        deadline_minutes * 60,
        step_minutes * 60,
        job_minutes * 60 - 15 * 60,
    )
    if not all(isinstance(value, int) and value > 0 for value in values):
        raise ValueError("duration values must be positive integers")
    if not all(left < right for left, right in zip(values[:5], values[1:5])) or values[4] > values[5]:
        raise ValueError("duration order must be T < idle < wave < deadline < step <= job - 15m")


def _windows_kernel32() -> Any:
    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
    kernel32.ResumeThread.restype = ctypes.c_uint32
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _create_job_object() -> tuple[Any, Any]:
    kernel32 = _windows_kernel32()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(getattr(ctypes, "get_last_error")(), "CreateJobObjectW failed")
    limits = _JobObjectExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = getattr(ctypes, "get_last_error")()
        kernel32.CloseHandle(handle)
        raise OSError(error, "SetInformationJobObject failed")
    return handle, kernel32


def _query_windows_processes(needles: Sequence[str]) -> list[dict[str, Any]]:
    if getattr(os, "name", "") != "nt" or not needles:
        return []
    query = (
        "$patterns = @(ConvertFrom-Json $env:HYPRIAL_PROC_NEEDLES) | "
        "ForEach-Object { [regex]::Escape($_) }; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "$cmd = $_.CommandLine; "
        "$_.ProcessId -ne $PID -and $cmd -and ($patterns | Where-Object { $cmd -match $_ }) "
        "} | Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    env = dict(os.environ)
    env["HYPRIAL_PROC_NEEDLES"] = json.dumps(list(needles))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode or not completed.stdout.strip():
        return []
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    rows = value if isinstance(value, list) else [value]
    current_pid = os.getpid()
    return [row for row in rows if isinstance(row, dict) and row.get("ProcessId") != current_pid]


def _ledger_activity_signature(ledger_dir: Path) -> tuple[tuple[str, int, int], ...]:
    items: list[tuple[str, int, int]] = []
    for path in sorted(ledger_dir.glob("*.jsonl")):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(items)


class _ManagedChild:
    def __init__(
        self,
        *,
        command: Sequence[str],
        output: Path,
        env: dict[str, str],
        cwd: Path,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        self._out_stream = output.open("wb")
        self._devnull_stream: Any = None
        self._process: subprocess.Popen[bytes] | None = None
        self._job_handle: Any = None
        self._kernel32: Any = None
        self._win_process: Any = None
        self._winapi: Any = None
        self.returncode: int | None = None
        if getattr(os, "name", "") == "nt":
            self._launch_windows(command, env=env, cwd=cwd)
        else:
            self._process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=self._out_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _launch_windows(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str],
        cwd: Path,
    ) -> None:
        winapi = importlib.import_module("_winapi")
        msvcrt = importlib.import_module("msvcrt")
        set_inheritable = getattr(os, "set_handle_inheritable")
        self._winapi = winapi
        try:
            self._devnull_stream = open(os.devnull, "rb")
            in_handle = msvcrt.get_osfhandle(self._devnull_stream.fileno())
            out_handle = msvcrt.get_osfhandle(self._out_stream.fileno())
            set_inheritable(in_handle, True)
            set_inheritable(out_handle, True)
            startupinfo = getattr(subprocess, "STARTUPINFO")()
            startupinfo.dwFlags |= winapi.STARTF_USESTDHANDLES
            startupinfo.hStdInput = in_handle
            startupinfo.hStdOutput = out_handle
            startupinfo.hStdError = out_handle
            startupinfo.lpAttributeList = {"handle_list": [in_handle, out_handle]}
            job_handle, kernel32 = _create_job_object()
            self._job_handle = job_handle
            self._kernel32 = kernel32
            cmdline = subprocess.list2cmdline(list(command))
            hp, ht, _, _ = winapi.CreateProcess(
                None,
                cmdline,
                None,
                None,
                True,
                CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP,
                env,
                str(cwd),
                startupinfo,
            )
            self._win_process = hp
            if not kernel32.AssignProcessToJobObject(job_handle, hp):
                error = getattr(ctypes, "get_last_error")()
                kernel32.TerminateProcess(hp, 1)
                winapi.CloseHandle(ht)
                raise OSError(error, "AssignProcessToJobObject failed")
            if kernel32.ResumeThread(ht) == 0xFFFFFFFF:
                error = getattr(ctypes, "get_last_error")()
                kernel32.TerminateProcess(hp, 1)
                winapi.CloseHandle(ht)
                raise OSError(error, "ResumeThread failed")
            winapi.CloseHandle(ht)
        except Exception:
            self.close()
            raise

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self._process is not None:
            self.returncode = self._process.poll()
            return self.returncode
        if self._win_process is not None and self._winapi is not None:
            result = self._winapi.WaitForSingleObject(self._win_process, 0)
            if result == self._winapi.WAIT_OBJECT_0:
                self.returncode = int(self._winapi.GetExitCodeProcess(self._win_process))
        return self.returncode

    def terminate_tree(self) -> None:
        if self._process is not None:
            if self._process.poll() is None:
                self._process.kill()
                self._process.wait()
            self.returncode = self._process.returncode
            return
        if self._job_handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(self._job_handle)
            self._job_handle = None
        if self._win_process is not None and self._winapi is not None:
            self._winapi.WaitForSingleObject(self._win_process, 5000)
            self.returncode = int(self._winapi.GetExitCodeProcess(self._win_process))

    def close(self) -> None:
        if self._job_handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(self._job_handle)
            self._job_handle = None
        if self._win_process is not None and self._winapi is not None:
            self._winapi.WaitForSingleObject(self._win_process, 5000)
            if self.returncode is None:
                self.returncode = int(self._winapi.GetExitCodeProcess(self._win_process))
            self._winapi.CloseHandle(self._win_process)
            self._win_process = None
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait()
        if self._devnull_stream is not None:
            self._devnull_stream.close()
            self._devnull_stream = None
        self._out_stream.close()


def _wait_with_watchdogs(
    child: _ManagedChild,
    *,
    ledger_dir: Path,
    idle_seconds: float,
    wave_deadline: float,
    job_deadline: float,
    snapshot_path: Path,
    process_needles: Sequence[str],
    poll_interval: float = 1.0,
) -> tuple[int, str | None]:
    last_signature = _ledger_activity_signature(ledger_dir)
    last_activity = time.monotonic()
    forced_status: str | None = None
    try:
        while True:
            code = child.poll()
            if code is not None:
                return code, None
            now = time.monotonic()
            signature = _ledger_activity_signature(ledger_dir)
            if signature != last_signature:
                last_signature = signature
                last_activity = now
            if now >= job_deadline:
                forced_status = "job-deadline"
                break
            if now >= wave_deadline or now - last_activity >= idle_seconds:
                forced_status = "controller-hang"
                break
            time.sleep(min(poll_interval, max(0.05, job_deadline - now)))
        snapshot = _query_windows_processes(process_needles)
        if snapshot:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        child.terminate_tree()
        return int(child.returncode if child.returncode is not None else 1), forced_status
    finally:
        child.close()


def _parent_alive(ppid: int) -> bool:
    if ppid <= 0:
        return True
    if getattr(os, "name", "") == "nt":
        try:
            kernel = importlib.import_module("hyprial.kernel")
            kernel.probe_process(ppid)
            return True
        except ImportError:
            return True
        except (ProcessLookupError, OSError):
            return False
    try:
        os.kill(ppid, 0)
        return True
    except OSError:
        return False


def _self_check() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if getattr(os, "name", "") == "nt":
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    ppid = os.getppid()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        **kwargs,
    )
    try:
        time.sleep(0.1)
        os.kill(child.pid, 0)
        time.sleep(0.2)
        child_alive = child.poll() is None
        parent_alive = _parent_alive(ppid)
        return {
            "passed": child_alive and parent_alive,
            "pid": child.pid,
            "parentPid": ppid,
            "childAlive": child_alive,
            "parentAlive": parent_alive,
        }
    except (OSError, KeyboardInterrupt) as error:
        return {
            "passed": False,
            "pid": child.pid,
            "parentPid": ppid,
            "childAlive": child.poll() is None,
            "parentAlive": _parent_alive(ppid),
            "error": str(error),
        }
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def build_pytest_command(
    *,
    workers: int,
    junit: Path,
    basetemp: Path,
    argsfile: Path,
    deselect: Sequence[str] = (),
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-m",
        "not gui_acceptance",
        "-n",
        str(workers),
        "--dist=loadfile",
        "--max-worker-restart=0",
        "--continue-on-collection-errors",
        "-p",
        "no:cacheprovider",
        "-o",
        "faulthandler_timeout=0",
        "-o",
        "junit_family=xunit2",
        f"--junitxml={junit}",
        f"--basetemp={basetemp}",
    ]
    for nodeid in deselect:
        command.append(f"--deselect={nodeid}")
    command.append(f"@{argsfile}")
    return command


def _run_one(
    *,
    files: Sequence[str],
    deselect: Sequence[str],
    args: argparse.Namespace,
    run_number: int,
    wave_number: int,
    wave_deadline: float,
    job_deadline: float,
    root: Path,
) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
    shard_tag = (
        int(getattr(args, "shard", 0))
        if getattr(args, "plan_manifest", None) is not None
        else 1
    )
    argsfile = args.results / f"shard-{shard_tag}-wave-{wave_number}-run-{run_number}.args"
    argsfile.parent.mkdir(parents=True, exist_ok=True)
    argsfile.write_text("\n".join(files) + "\n", encoding="utf-8")
    junit = args.results / f"shard-{shard_tag}-wave-{wave_number}-run-{run_number}.xml"
    log = args.results / f"shard-{shard_tag}-wave-{wave_number}-run-{run_number}.log"
    snapshot_path = args.results / f"hang-snapshot-wave-{wave_number}-run-{run_number}.json"
    ledger = args.ledger_root / f"wave-{wave_number}-run-{run_number}"
    ledger.mkdir(parents=True, exist_ok=True)
    base = args.basetemp_root / f"b{shard_tag}w{wave_number}r{run_number}"
    env = dict(os.environ)
    env.update(
        {
            "HYPRIAL_TEST_LEDGER_DIR": str(ledger),
            "HYPRIAL_TEST_WATCHDOG_SECONDS": str(args.watchdog_seconds),
            "HYPRIAL_TEST_TMP_ROOT": str(args.basetemp_root),
            "PYTHONUTF8": "1",
            "NO_COLOR": "1",
        }
    )
    if getattr(args, "acl_probe", False):
        env["HYPRIAL_TEST_ACL_PROBE"] = "1"
    command = build_pytest_command(
        workers=args.workers,
        junit=junit,
        basetemp=base,
        argsfile=argsfile,
        deselect=deselect,
    )
    needles = [str(base), str(args.basetemp_root)]
    child = _ManagedChild(command=command, output=log, env=env, cwd=root)
    returncode, forced_status = _wait_with_watchdogs(
        child,
        ledger_dir=ledger,
        idle_seconds=float(args.idle_seconds),
        wave_deadline=wave_deadline,
        job_deadline=job_deadline,
        snapshot_path=snapshot_path,
        process_needles=needles,
    )
    leaked_processes = _query_windows_processes(needles)
    return returncode, junit, ledger, forced_status, leaked_processes


def _emit_manifest(manifest: dict[str, Any], *, report: Path, log_path: Path) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    line = f"WINDOWS_WAVES_DONE {json.dumps(manifest, sort_keys=True)}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    print(line, flush=True)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def run_waves(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    validate_durations(
        args.watchdog_seconds,
        args.idle_seconds,
        args.wave_minutes,
        args.deadline_minutes,
        args.step_minutes,
        args.job_minutes,
    )
    plan_manifest = getattr(args, "plan_manifest", None)
    shard_index = getattr(args, "shard", None)
    waves = load_planned_waves(getattr(args, "files", None), plan_manifest, shard_index)
    planned = sorted({path for wave in waves for path in wave})
    strict_skips = str(args.tier) == "1"
    args.results.mkdir(parents=True, exist_ok=True)
    args.ledger_root.mkdir(parents=True, exist_ok=True)
    args.basetemp_root.mkdir(parents=True, exist_ok=True)
    job_started = time.monotonic()
    job_deadline = job_started + args.deadline_minutes * 60
    self_check = _self_check()
    runs: list[dict[str, Any]] = []
    all_junits: list[Path] = []
    completed_files: set[str] = set()
    completed_nodeids_by_file: dict[str, set[str]] = {}
    collection_skipped_files: set[str] = set()
    leaks: list[dict[str, Any]] = []
    leaked_processes: list[dict[str, Any]] = []
    final_status = "passed"
    duplicate_runs: list[dict[str, Any]] = []
    duplicate_union: str | None = None
    stop_all = False
    for wave_number, wave_files in enumerate(waves, start=1):
        if time.monotonic() >= job_deadline:
            final_status = "job-deadline"
            break
        wave_deadline = min(job_deadline, time.monotonic() + args.wave_minutes * 60)
        wave_pending = list(wave_files)
        for attempt in range(1, 4):
            if time.monotonic() >= job_deadline:
                final_status = "job-deadline"
                stop_all = True
                break
            deselect = sorted(
                nodeid
                for file_name in wave_pending
                for nodeid in completed_nodeids_by_file.get(file_name, ())
            )
            started = time.monotonic()
            returncode, junit, ledger, forced_status, survivors = _run_one(
                files=wave_pending,
                deselect=deselect,
                args=args,
                run_number=attempt,
                wave_number=wave_number,
                wave_deadline=wave_deadline,
                job_deadline=job_deadline,
                root=root,
            )
            all_junits.append(junit)
            leaked_processes.extend(survivors)
            try:
                outcome = classify_run(
                    returncode=returncode,
                    junit_paths=[junit],
                    ledger_dir=ledger,
                    planned_files=wave_pending,
                    root=root,
                    forced_status=forced_status,
                    strict_skips=strict_skips,
                )
            except ValueError as error:
                if not str(error).startswith("duplicate-result:"):
                    raise
                duplicate_runs.append(
                    {"wave": wave_number, "run": attempt, "result": str(error)}
                )
                final_status = "unreported-files"
                runs.append(
                    {
                        "wave": wave_number,
                        "run": attempt,
                        "status": "unreported-files",
                        "offsettable": False,
                        "returncode": returncode,
                        "junit": str(junit),
                        "duration": round(time.monotonic() - started, 3),
                        "details": [str(error)],
                    }
                )
                continue
            for file_name, nodeids in outcome.nodeids_by_file.items():
                completed_nodeids_by_file.setdefault(file_name, set()).update(nodeids)
            completed_files.update(outcome.reported_files - outcome.partial_files)
            collection_skipped_files.update(outcome.collection_skipped_files)
            leaks.extend(outcome.leaks)
            runs.append(
                {
                    "wave": wave_number,
                    "run": attempt,
                    "status": outcome.status,
                    "offsettable": outcome.offsettable,
                    "returncode": returncode,
                    "junit": str(junit),
                    "duration": round(time.monotonic() - started, 3),
                    "details": list(outcome.details),
                }
            )
            final_status = merge_wave_status(final_status, outcome.status)
            wave_pending = sorted(set(wave_files) - completed_files)
            if outcome.status == "job-deadline":
                stop_all = True
                break
            if not wave_pending:
                break
        if stop_all:
            break
    pending = sorted(set(planned) - completed_files)
    try:
        union_junit = read_junit(all_junits, planned, root=root)
        if strict_skips:
            if (
                CONTRACT_TEST_FILE in completed_files
                and (union_junit.contract_skip_errors or union_junit.contract_skips != 13)
                and final_status == "passed"
            ):
                final_status = "failed"
            if union_junit.other_skips and final_status == "passed":
                final_status = "failed"
    except ValueError as error:
        duplicate_union = str(error)
        if final_status not in HARD_STATES:
            final_status = "unreported-files"
    duplicate_result = duplicate_runs[0]["result"] if duplicate_runs else duplicate_union
    if pending:
        if final_status not in HARD_STATES:
            final_status = "unreported-files"
        synthesize_unreported(args.results / "unreported.xml", pending)
    manifest: dict[str, Any] = {
        "tier": str(args.tier),
        "waveCount": len(waves),
        "aclProbe": bool(getattr(args, "acl_probe", False)),
        "status": final_status,
        "offsettable": is_offsettable(final_status),
        "duplicateResult": duplicate_result,
        "duplicateRuns": duplicate_runs,
        "duplicateUnion": duplicate_union,
        "plannedFiles": planned,
        "reportedFiles": sorted(completed_files),
        "collectionSkippedFiles": {
            "files": len(collection_skipped_files),
            "paths": sorted(collection_skipped_files),
        },
        "runs": runs,
        "leakedProcesses": leaked_processes,
        "leaks": leaks,
        "selfCheck": self_check,
        "durations": {
            "watchdog": args.watchdog_seconds,
            "idle": args.idle_seconds,
            "waveMinutes": args.wave_minutes,
            "deadlineMinutes": args.deadline_minutes,
            "stepMinutes": args.step_minutes,
            "jobMinutes": args.job_minutes,
        },
    }
    if plan_manifest is not None:
        manifest["shard"] = 0 if shard_index is None else int(shard_index)
    _emit_manifest(manifest, report=args.report, log_path=args.results / "waves.log")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", required=True)
    parser.add_argument("--files", type=Path, default=None)
    parser.add_argument("--plan-manifest", type=Path, default=None)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--acl-probe", action="store_true", default=False)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--watchdog-seconds", type=int, required=True)
    parser.add_argument("--idle-seconds", type=int, default=None)
    parser.add_argument("--wave-minutes", type=int, required=True)
    parser.add_argument("--deadline-minutes", type=int, required=True)
    parser.add_argument("--step-minutes", type=int, default=30)
    parser.add_argument("--job-minutes", type=int, default=60)
    parser.add_argument("--basetemp-root", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--ledger-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if (args.files is None) == (args.plan_manifest is None):
        parser.error("pass exactly one of --files or --plan-manifest")
    if args.idle_seconds is None:
        args.idle_seconds = args.watchdog_seconds + 300
    args.acl_probe = bool(args.acl_probe or _env_truthy("WINDOWS_SUITE_ACL_PROBE"))
    try:
        manifest = run_waves(args, Path.cwd())
    except Exception as error:
        failure = {
            "status": "job-deadline",
            "offsettable": False,
            "error": str(error),
            "selfCheck": {"passed": False},
        }
        _emit_manifest(failure, report=args.report, log_path=args.results / "waves.log")
        return 1
    if (
        manifest.get("status") != "passed"
        or manifest.get("duplicateResult")
        or not manifest.get("selfCheck", {}).get("passed")
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
