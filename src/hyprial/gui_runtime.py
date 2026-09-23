"""Small, identity-fenced lifecycle for the installed local GUI."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from hyprial.contracts import ipc_errors
from hyprial.installers import InstallError, prepare_application_launch
from hyprial.mcp.channel import (
    _OwnerProcessStatus,
    _owner_process_status,
    _read_process_identity,
)
from hyprial.persistent_config import atomic_json_write


_PROCESS_SCHEMA = "hyprial.gui-process/v1"
_LAUNCH_SCHEMA = "hyprial.gui-launch/v1"
_READY_TIMEOUT = 30.0
_STOP_TIMEOUT = 10.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _app_root(hyprial_home: Path, app: str) -> Path:
    return hyprial_home / "apps" / app


def _runtime_root(hyprial_home: Path, app: str, component: str | None) -> Path:
    if component not in (None, "dashboard", "dsh") or (component is not None and app != "gui"):
        raise InstallError(ipc_errors.INVALID_ARGUMENT, "GUI app must be dashboard or dsh")
    root = _app_root(hyprial_home, app)
    # Dashboard path is retained only for ownership-checked retirement.
    # DSH keeps its existing record path to avoid duplicating a live process.
    return root / "runtimes" / "dashboard" if component == "dashboard" else root


@contextmanager
def _lifecycle_lock(app_root: Path) -> Iterator[None]:
    app_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(app_root / "app.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_record(app_root: Path) -> dict[str, Any] | None:
    path = app_root / "process.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError("GUI_STATE_INVALID", f"cannot read GUI process state: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema") != _PROCESS_SCHEMA:
        raise InstallError("GUI_STATE_INVALID", "GUI process state has an invalid schema")
    if not isinstance(raw.get("pid"), int) or raw["pid"] <= 0:
        raise InstallError("GUI_STATE_INVALID", "GUI process state has an invalid pid")
    if not isinstance(raw.get("processIdentity"), str) or not raw["processIdentity"]:
        raise InstallError("GUI_STATE_INVALID", "GUI process state has no process identity")
    return raw


def _record_status(record: dict[str, Any]) -> str:
    try:
        stat = Path(f"/proc/{record['pid']}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        stat = ""
    comm_end = stat.rfind(")")
    if comm_end >= 0 and stat[comm_end + 2 :].split()[:1] == ["Z"]:
        return "exited"
    observed = _owner_process_status(record["pid"], record["processIdentity"])
    if observed is _OwnerProcessStatus.ALIVE:
        return "running"
    if observed in {
        _OwnerProcessStatus.PID_MISSING,
        _OwnerProcessStatus.IDENTITY_MISMATCH,
    }:
        return "exited"
    return "unknown"


def gui_status(hyprial_home: Path, app: str = "gui", *, component: str | None = None) -> dict[str, Any]:
    """Return never-started, running, exited, or fail-safe unknown state."""

    record = _read_record(_runtime_root(hyprial_home, app, component))
    if record is None:
        return {"ok": True, "name": app, "state": "never-started"}
    return {
        "ok": True,
        "name": app,
        "state": _record_status(record),
        "pid": record["pid"],
        "url": record.get("url"),
        "logPath": record.get("logPath"),
        "startedAt": record.get("startedAt"),
        "stoppedAt": record.get("stoppedAt"),
    }


def _wait_for_identity(process: subprocess.Popen[bytes]) -> str | None:
    deadline = time.monotonic() + 2.0
    while process.poll() is None and time.monotonic() < deadline:
        identity = _read_process_identity(process.pid)
        if identity is not None:
            return identity
        time.sleep(0.02)
    return None


def _read_launch_url(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != _LAUNCH_SCHEMA:
        raise InstallError("GUI_START_FAILED", "GUI emitted invalid launch information")
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith("http://"):
        raise InstallError("GUI_START_FAILED", "GUI emitted an invalid HTTP URL")
    return url


def _http_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError as error:
        # urllib raises for 4xx; authentication-required still proves readiness.
        return 400 <= error.code < 500
    except (OSError, urllib.error.URLError):
        return False


def _terminate_if_owned(pid: int, identity: str) -> None:
    if _owner_process_status(pid, identity) is _OwnerProcessStatus.ALIVE:
        os.kill(pid, signal.SIGTERM)


def start_gui_background(hyprial_home: Path, app: str = "gui", *, component: str | None = None) -> dict[str, Any]:
    """Start one detached GUI and return only after its HTTP endpoint responds."""

    if component == "dashboard":
        raise InstallError(ipc_errors.INVALID_ARGUMENT, "Dashboard is retired; use hyprial gui")
    app_root = _runtime_root(hyprial_home, app, component)
    with _lifecycle_lock(app_root):
        existing = _read_record(app_root)
        if existing is not None:
            state = _record_status(existing)
            if state == "running":
                return {**gui_status(hyprial_home, app, component=component), "alreadyRunning": True}
            if state == "unknown":
                raise InstallError(
                    "GUI_PROCESS_UNKNOWN",
                    "cannot verify the recorded GUI process identity; refusing to start another",
                )

        launch = prepare_application_launch(app, hyprial_home=hyprial_home)
        log_path = app_root / f"{component or app}.log"
        launch_info = app_root / f".launch-{uuid4().hex}.json"
        environment = dict(launch.env)
        if component is not None:
            environment["HYPRIAL_GUI_APP"] = component
        environment["HYPRIAL_GUI_LAUNCH_INFO_FILE"] = str(launch_info)
        environment["DSH_HYPRIAL_NO_OPEN"] = "1"
        with log_path.open("ab", buffering=0) as log_stream:
            process = subprocess.Popen(
                list(launch.argv),
                cwd=launch.cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        identity = _wait_for_identity(process)
        if identity is None:
            if process.poll() is None:
                process.terminate()
            raise InstallError(
                "GUI_START_FAILED",
                f"GUI exited before its process identity was recorded; see {log_path}",
            )
        record: dict[str, Any] = {
            "schema": _PROCESS_SCHEMA,
            "pid": process.pid,
            "processIdentity": identity,
            "sourceCommit": launch.source_commit,
            "component": component,
            "url": None,
            "logPath": str(log_path),
            "startedAt": _now(),
        }
        atomic_json_write(app_root / "process.json", record)

        deadline = time.monotonic() + _READY_TIMEOUT
        url: str | None = None
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                url = url or _read_launch_url(launch_info)
                if url is not None and _http_ready(url):
                    record["url"] = url
                    atomic_json_write(app_root / "process.json", record)
                    return {**gui_status(hyprial_home, app, component=component), "alreadyRunning": False}
                time.sleep(0.1)
        except Exception:
            _terminate_if_owned(process.pid, identity)
            raise
        finally:
            launch_info.unlink(missing_ok=True)
        _terminate_if_owned(process.pid, identity)
        raise InstallError(
            "GUI_START_FAILED",
            f"GUI did not become ready within {_READY_TIMEOUT:g}s; see {log_path}",
        )


def stop_gui(hyprial_home: Path, app: str = "gui", *, component: str | None = None) -> dict[str, Any]:
    """Stop only the exact process identity previously recorded by hyprial."""

    app_root = _runtime_root(hyprial_home, app, component)
    with _lifecycle_lock(app_root):
        record = _read_record(app_root)
        if record is None:
            return {"ok": True, "name": app, "state": "never-started", "stopped": False}
        state = _record_status(record)
        if state == "unknown":
            raise InstallError(
                "GUI_PROCESS_UNKNOWN",
                "cannot verify the recorded GUI process identity; refusing to signal it",
            )
        if state == "exited":
            return {**gui_status(hyprial_home, app, component=component), "stopped": False}
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except ProcessLookupError:
            record["stoppedAt"] = _now()
            atomic_json_write(app_root / "process.json", record)
            return {**gui_status(hyprial_home, app, component=component), "stopped": True}
        deadline = time.monotonic() + _STOP_TIMEOUT
        while time.monotonic() < deadline:
            if _record_status(record) == "exited":
                record["stoppedAt"] = _now()
                atomic_json_write(app_root / "process.json", record)
                return {**gui_status(hyprial_home, app, component=component), "stopped": True}
            time.sleep(0.05)
        raise InstallError(
            "GUI_STOP_TIMEOUT",
            f"GUI pid {record['pid']} did not stop within {_STOP_TIMEOUT:g}s",
        )
