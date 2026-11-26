"""Run the six installation and lifecycle smoke checks on Windows.

Decision (2026-11-26, tier 1 smoke).  The smoke runner owns all multi-command logic so
workflow steps can keep one native command and explicit exit-code handling.
Both legs run step 2 with
``hyprial init --json --no-service`` and step 6 with unified teardown
(``hyprial daemon stop --json``, ``probe_process`` over recorded pids, and
``Win32_Process`` command-line scan over ``%USERPROFILE%\\pt\\s``); step 3 checks
``hyprial daemon status --json`` first and only falls back to ``daemon start``
when ``running`` is false.  Step 5 declares that only pipe-EOF stdin stop is
covered (ConPTY/terminal hosts remain uncovered while KP-136 is unfixed).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.resources
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

PRIVATE_PATTERN = re.compile(r"(?:[A-Za-z]:)?[\\/]Users[\\/][^\s\"']+")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
STDIN_STOP_SCOPE = "pipe-eof-only; ConPTY/terminal-host not covered (KP-136 unfixed)"
HOST_RUNTIME_FILES = (
    "launch.url",
    "launch.ready",
    "host.log",
    "server.json",
    "request-http.json",
)


def _display(value: str, home: Path) -> str:
    redacted = value.replace(str(home), "%USERPROFILE%\\pt\\s")
    redacted = redacted.replace(str(Path.home()), "%USERPROFILE%")
    return PRIVATE_PATTERN.sub("%USERPROFILE%", redacted)


def _hyprial_command() -> list[str]:
    if getattr(os, "name", "") == "nt":
        candidate = Path(sys.executable).with_name("hyprial.exe")
        if candidate.is_file():
            return [str(candidate)]
    return [sys.executable, "-m", "hyprial.cli"]


def _parse_json_stdout(stdout: str, label: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise RuntimeError(f"{label} produced empty JSON stdout")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{label} produced invalid JSON stdout: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} JSON output is not an object")
    return payload


def _extract_pids(payload: dict[str, Any]) -> list[int]:
    found: list[int] = []
    for key in ("pid", "frontend_pid", "backend_pid", "assets_pid", "survivingPid"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            found.append(value)
    daemon = payload.get("daemon")
    if isinstance(daemon, dict):
        value = daemon.get("pid")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            found.append(value)
    return found


def _pid_alive(pid: int) -> bool:
    kernel = importlib.import_module("hyprial.kernel")
    try:
        kernel.probe_process(pid)
    except (ProcessLookupError, OSError):
        return False
    return True


def _port_listening(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0


def _emit(line: str, log_lines: list[str], *, home: Path) -> None:
    safe = _display(line, home)
    log_lines.append(safe)
    print(safe, flush=True)


def run_command(
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    home: Path,
    log_lines: list[str],
) -> tuple[int, str, str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    _emit(f"SMOKE_COMMAND {' '.join(command)} rc={completed.returncode}", log_lines, home=home)
    combined = (completed.stdout + completed.stderr).strip()
    if combined:
        _emit(combined[-6000:], log_lines, home=home)
    return completed.returncode, completed.stdout, completed.stderr


def verify_release(expected_commit: str) -> None:
    actors = importlib.import_module("hyprial._core.actors")
    if getattr(actors, "ENGINE_ABI_VERSION", None) != 1:
        raise RuntimeError(f"unexpected ENGINE_ABI_VERSION {getattr(actors, 'ENGINE_ABI_VERSION', None)!r}")
    if not COMMIT_PATTERN.fullmatch(expected_commit):
        raise RuntimeError(f"expected commit must be a 40-char hex sha, got {expected_commit!r}")
    resource = importlib.resources.files("hyprial._gui").joinpath("release.json")
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    if manifest.get("sourceCommit") != expected_commit:
        raise RuntimeError(
            f"release.json sourceCommit {manifest.get('sourceCommit')!r} != {expected_commit!r}"
        )
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("release.json files must be a non-empty list")
    root = Path(str(resource)).parent
    for entry in entries:
        path = root / str(entry["path"])
        data = path.read_bytes()
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"GUI manifest mismatch for {entry['path']}")


def process_command_lines(home: Path) -> list[dict[str, Any]]:
    if getattr(os, "name", "") != "nt":
        return []
    query = (
        "$needle = [regex]::Escape($env:HYPRIAL_PROC_NEEDLE); "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match $needle } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    env = dict(os.environ)
    env["HYPRIAL_PROC_NEEDLE"] = str(home)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        return [{"error": completed.stderr.strip()}]
    if not completed.stdout.strip():
        return []
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return [{"error": f"invalid Win32_Process JSON: {error}"}]
    rows = value if isinstance(value, list) else [value]
    current_pid = os.getpid()
    return [row for row in rows if isinstance(row, dict) and row.get("ProcessId") != current_pid]


def _post_json(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> tuple[int, dict[str, Any], Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload, response.headers


def step_one(expected_commit: str) -> None:
    verify_release(expected_commit)


def step_two(
    home: Path,
    env: dict[str, str],
    cwd: Path,
    log_lines: list[str],
    recorded_pids: set[int],
) -> dict[str, Any]:
    code, stdout, _ = run_command(
        [*_hyprial_command(), "init", "--json", "--no-service"],
        env=env,
        cwd=cwd,
        home=home,
        log_lines=log_lines,
    )
    payload = _parse_json_stdout(stdout, "hyprial init") if stdout.strip() else {}
    recorded_pids.update(_extract_pids(payload))
    if code != 0:
        raise RuntimeError(f"hyprial init failed: rc={code} code={payload.get('code')!r}")
    if payload.get("ok") is not True:
        raise RuntimeError(f"hyprial init did not return ok=true: {payload}")
    kernel = importlib.import_module("hyprial.kernel")
    kernel.secure_private_directory(home, create=False)
    return payload


def step_three(
    home: Path,
    env: dict[str, str],
    cwd: Path,
    log_lines: list[str],
    recorded_pids: set[int],
) -> dict[str, Any]:
    code, stdout, _ = run_command(
        [*_hyprial_command(), "daemon", "status", "--json"],
        env=env,
        cwd=cwd,
        home=home,
        log_lines=log_lines,
    )
    if code != 0:
        raise RuntimeError(f"daemon status failed: {code}")
    status = _parse_json_stdout(stdout, "hyprial daemon status")
    raw_daemon = status.get("daemon")
    daemon: dict[str, Any] = raw_daemon if isinstance(raw_daemon, dict) else {}
    if daemon.get("running") is not True:
        start_code, start_stdout, _ = run_command(
            [*_hyprial_command(), "daemon", "start", "--json"],
            env=env,
            cwd=cwd,
            home=home,
            log_lines=log_lines,
        )
        if start_stdout.strip():
            recorded_pids.update(_extract_pids(_parse_json_stdout(start_stdout, "hyprial daemon start")))
        if start_code != 0:
            raise RuntimeError(f"daemon start failed: {start_code}")
        code, stdout, _ = run_command(
            [*_hyprial_command(), "daemon", "status", "--json"],
            env=env,
            cwd=cwd,
            home=home,
            log_lines=log_lines,
        )
        if code != 0:
            raise RuntimeError(f"daemon status after start failed: {code}")
        status = _parse_json_stdout(stdout, "hyprial daemon status")
        raw_daemon = status.get("daemon")
        daemon = raw_daemon if isinstance(raw_daemon, dict) else {}
    recorded_pids.update(_extract_pids(status))
    if status.get("ok") is not True or daemon.get("running") is not True:
        raise RuntimeError(f"daemon is not running: {status}")
    socket_value = daemon.get("socket")
    if not isinstance(socket_value, str) or not socket_value:
        raise RuntimeError(f"daemon status missing socket path: {status}")
    endpoint_path = Path(socket_value)
    if getattr(os, "name", "") == "nt":
        endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
        pipe = endpoint.get("pipe")
        if endpoint.get("transport") != "windows-pipe-v1" or not isinstance(pipe, str) or not pipe.startswith("\\\\.\\pipe\\"):
            raise RuntimeError(f"daemon endpoint is not a Windows named pipe: {endpoint}")
        status["endpoint"] = endpoint
    elif not endpoint_path.exists():
        raise RuntimeError(f"daemon socket does not exist: {endpoint_path}")
    return status


def step_four(
    home: Path,
    env: dict[str, str],
    cwd: Path,
    stderr_path: Path,
    log_lines: list[str],
    recorded_pids: set[int],
) -> tuple[subprocess.Popen[str], dict[str, Any], Path]:
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_stream = stderr_path.open("w", encoding="utf-8")
    command = [*_hyprial_command(), "gui", "--no-open", "--json", "--port", "0", "--stdin-stop"]
    _emit(f"SMOKE_COMMAND {' '.join(command)} rc=spawn", log_lines, home=home)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_stream,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stderr_stream.close()
    recorded_pids.add(process.pid)
    try:
        stdout_stream = process.stdout
        if stdout_stream is None:
            raise RuntimeError("GUI stdout is unavailable")
        first: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: first.put(stdout_stream.readline()), daemon=True).start()
        try:
            line = first.get(timeout=90)
        except queue.Empty as error:
            raise RuntimeError("GUI startup record timed out") from error
        if not line:
            tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise RuntimeError(f"GUI did not emit a startup record: {_display(tail, home)}")
        record = json.loads(line)
        if record.get("ok") is not True:
            raise RuntimeError(f"GUI startup failed: {record}")
        recorded_pids.update(_extract_pids(record))
        port = record.get("port")
        if not isinstance(port, int) or port <= 0:
            raise RuntimeError(f"GUI startup record has invalid port: {record}")
        raw_url = record.get("url")
        origin = str(raw_url).rstrip("/") if isinstance(raw_url, str) and raw_url else f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(origin + "/", timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"GUI root returned {response.status}")
        handoff_value = record.get("handoff_path")
        if not isinstance(handoff_value, str) or not handoff_value:
            raise RuntimeError("GUI startup record missing handoff_path")
        handoff = Path(handoff_value)
        runtime_dir = handoff.parent
        launch = handoff.read_text(encoding="utf-8").strip()
        if "#grant=" not in launch:
            raise RuntimeError("GUI handoff file missing #grant=")
        grant = urllib.parse.unquote(launch.split("#grant=", 1)[1])
        status_code, opened, headers = _post_json(
            origin + "/daemon/session",
            {"grant": grant},
            {"Origin": origin},
        )
        if status_code != 200:
            raise RuntimeError(f"GUI session open returned {status_code}: {opened}")
        cookies = "; ".join(value.split(";", 1)[0] for value in headers.get_all("Set-Cookie") or [])
        if not cookies:
            raise RuntimeError("GUI session open did not set cookies")
        raw_catalog = opened.get("catalog")
        catalog: dict[str, Any] = raw_catalog if isinstance(raw_catalog, dict) else {}
        session_headers = {
            "Origin": origin,
            "Cookie": cookies,
            "X-Hyprial-View": str(opened.get("view", "")),
            "X-Hyprial-Generation": str(opened.get("generation", "")),
            "X-Hyprial-Csrf": str(opened.get("csrf", "")),
            "X-Hyprial-Catalog-Digest": str(catalog.get("digest", "")),
        }
        req_status, relayed, _ = _post_json(
            origin + "/daemon/request/onboarding.status",
            {},
            session_headers,
        )
        if req_status != 200 or relayed.get("ok") is not True or relayed.get("method") != "onboarding.status":
            raise RuntimeError(f"GUI daemon request failed: status={req_status} payload={relayed}")
        return process, record, runtime_dir
    except Exception:
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        raise


def step_five(
    process: subprocess.Popen[str],
    record: dict[str, Any],
    runtime_dir: Path,
) -> None:
    if process.stdin is None:
        raise RuntimeError("GUI stdin is unavailable")
    process.stdin.close()
    code = process.wait(timeout=60)
    if code != 0:
        raise RuntimeError(f"GUI stdin stop returned {code}")
    host = str(record.get("host") or "127.0.0.1")
    port = int(record["port"])
    gui_pids = _extract_pids(record)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in gui_pids) and not _port_listening(host, port):
            break
        time.sleep(0.1)
    still_alive = [pid for pid in gui_pids if _pid_alive(pid)]
    if still_alive:
        raise RuntimeError(f"GUI child process survived stdin stop: {still_alive}")
    if _port_listening(host, port):
        raise RuntimeError(f"GUI port {host}:{port} is still listening after stdin stop")
    kernel = importlib.import_module("hyprial.kernel")
    lock_path = runtime_dir / "supervisor.lock"
    if lock_path.exists():
        lock = kernel.ProcessFileLock(lock_path)
        lock.acquire(0.0)
        lock.release()
    leftovers = [name for name in HOST_RUNTIME_FILES if (runtime_dir / name).exists()]
    if leftovers:
        raise RuntimeError(f"GUI host runtime files survived stop: {leftovers}")


def step_six(
    home: Path,
    env: dict[str, str],
    cwd: Path,
    log_lines: list[str],
    recorded_pids: set[int],
) -> None:
    stop_error: str | None = None
    code, stdout, _ = run_command(
        [*_hyprial_command(), "daemon", "stop", "--json"],
        env=env,
        cwd=cwd,
        home=home,
        log_lines=log_lines,
    )
    if stdout.strip():
        try:
            recorded_pids.update(_extract_pids(_parse_json_stdout(stdout, "hyprial daemon stop")))
        except RuntimeError as error:
            stop_error = str(error)
    if code != 0 and stop_error is None:
        stop_error = f"daemon stop failed: {code}"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and any(_pid_alive(pid) for pid in recorded_pids):
        time.sleep(0.2)
    alive_recorded = sorted(pid for pid in recorded_pids if _pid_alive(pid))
    proc_matches = process_command_lines(home)
    match_pids = sorted(
        int(row["ProcessId"])
        for row in proc_matches
        if isinstance(row.get("ProcessId"), int) and int(row["ProcessId"]) > 0
    )
    survivor_pids = sorted(set(alive_recorded) | set(match_pids))
    if survivor_pids or proc_matches:
        if getattr(os, "name", "") == "nt":
            for pid in survivor_pids:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                )
        raise RuntimeError(
            f"processes survived daemon stop: alive_pids={alive_recorded} wmi={proc_matches}"
        )
    if stop_error is not None:
        raise RuntimeError(stop_error)


def run_smoke(args: argparse.Namespace) -> int:
    home = Path.home() / "pt" / "s"
    home.parent.mkdir(parents=True, exist_ok=True)
    results_dir = args.results if args.results is not None else Path.cwd() / "smoke-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    env = dict(os.environ)
    env["HYPRIAL_HOME"] = str(home)
    env.pop("HARNESS_STATE_DIR", None)
    env["NO_COLOR"] = "1"
    env["PYTHONUTF8"] = "1"
    cwd = Path.cwd()
    step_states: dict[str, str] = {}
    step_errors: dict[str, str] = {}
    recorded_pids: set[int] = set()
    process: subprocess.Popen[str] | None = None
    gui_record: dict[str, Any] | None = None
    runtime_dir: Path | None = None
    selected = ["1", "2", "6"] if args.leg == "arm" else ["1", "2", "3", "4", "5", "6"]
    started = time.monotonic()
    try:
        kernel = importlib.import_module("hyprial.kernel")
        kernel.secure_private_directory(home, create=True)
        for step in selected:
            try:
                if step == "1":
                    step_one(args.expected_commit)
                elif step == "2":
                    step_two(home, env, cwd, log_lines, recorded_pids)
                elif step == "3":
                    step_three(home, env, cwd, log_lines, recorded_pids)
                elif step == "4":
                    stderr_path = results_dir / f"gui-{args.leg}-stderr.log"
                    process, gui_record, runtime_dir = step_four(
                        home, env, cwd, stderr_path, log_lines, recorded_pids
                    )
                elif step == "5":
                    if process is None or gui_record is None or runtime_dir is None:
                        raise RuntimeError("GUI process was not started")
                    step_five(process, gui_record, runtime_dir)
                    process = None
                else:
                    step_six(home, env, cwd, log_lines, recorded_pids)
                step_states[step] = "passed"
                _emit(f"SMOKE_STEP {step} passed", log_lines, home=home)
            except Exception as error:
                step_states[step] = "failed"
                step_errors[step] = _display(str(error), home)
                _emit(f"SMOKE_STEP {step} failed {error}", log_lines, home=home)
                break
    except Exception as error:
        step_states.setdefault("1", "failed")
        step_errors["setup"] = _display(str(error), home)
        _emit(f"SMOKE_STEP setup failed {error}", log_lines, home=home)
    finally:
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
        if "6" not in step_states:
            try:
                step_six(home, env, cwd, log_lines, recorded_pids)
                step_states["6"] = "passed"
                _emit("SMOKE_STEP 6 passed", log_lines, home=home)
            except Exception as error:
                step_states["6"] = "failed"
                step_errors["6"] = _display(str(error), home)
                _emit(f"SMOKE_STEP 6 failed {error}", log_lines, home=home)
    passed = all(step_states.get(step) == "passed" for step in selected) and not step_errors
    payload = {
        "leg": args.leg,
        "steps": step_states,
        "errors": step_errors,
        "exitCode": 0 if passed else 1,
        "pids": sorted(recorded_pids),
        "duration": round(time.monotonic() - started, 3),
        "stdinStopScope": STDIN_STOP_SCOPE,
    }
    done_line = f"SMOKE_DONE {json.dumps(payload, sort_keys=True)}"
    _emit(done_line, log_lines, home=home)
    (results_dir / f"smoke-{args.leg}.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    (results_dir / f"smoke-{args.leg}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return int(payload["exitCode"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--leg", choices=("x64", "arm"), required=True)
    parser.add_argument("--expected-commit", default=os.environ.get("SMOKE_EXPECT_COMMIT", ""))
    parser.add_argument("--results", type=Path, default=None)
    return run_smoke(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
