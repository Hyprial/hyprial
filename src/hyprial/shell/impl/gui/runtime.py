"""Small, identity-fenced lifecycle for the bundled local GUI."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from hyprial.kernel import ipc_errors, atomic_json_write, lock_exclusive, unlock
from hyprial.daemon import (
    OwnerProcessStatus as _OwnerProcessStatus,
    owner_process_status as _owner_process_status,
    read_process_identity as _read_process_identity,
)

from . import unpack
from .bundle import PRODUCT_INPUTS
from .errors import GuiError
from .product import product_bundle


_PROCESS_SCHEMA = "hyprial.gui-process/v1"
_LAUNCH_SCHEMA = "hyprial.gui-launch/v1"
_READY_TIMEOUT = 30.0
_STOP_TIMEOUT = 10.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _app_root(hyprial_home: Path, app: str) -> Path:
    return hyprial_home / "apps" / app


def _runtime_root(hyprial_home: Path, app: str, component: str | None) -> Path:
    if component not in (None, "dashboard", "gui") or (component is not None and app != "gui"):
        raise GuiError(ipc_errors.INVALID_ARGUMENT, "Unknown GUI runtime component")
    root = _app_root(hyprial_home, app)
    # Dashboard path is retained only for ownership-checked retirement.
    # Reuse the existing ownership-fenced record so a legacy live process is
    # not forgotten merely because the frontend implementation was replaced.
    return root / "runtimes" / "dashboard" if component == "dashboard" else root


@contextmanager
def _lifecycle_lock(app_root: Path) -> Iterator[None]:
    app_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(app_root / "app.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        lock_exclusive(descriptor)
        yield
    finally:
        unlock(descriptor)
        os.close(descriptor)


def _read_record(app_root: Path) -> dict[str, Any] | None:
    path = app_root / "process.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise GuiError("GUI_STATE_INVALID", f"cannot read GUI process state: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema") != _PROCESS_SCHEMA:
        raise GuiError("GUI_STATE_INVALID", "GUI process state has an invalid schema")
    if not isinstance(raw.get("pid"), int) or raw["pid"] <= 0:
        raise GuiError("GUI_STATE_INVALID", "GUI process state has an invalid pid")
    if not isinstance(raw.get("processIdentity"), str) or not raw["processIdentity"]:
        raise GuiError("GUI_STATE_INVALID", "GUI process state has no process identity")
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
        raise GuiError("GUI_START_FAILED", "GUI emitted invalid launch information")
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith("http://"):
        raise GuiError("GUI_START_FAILED", "GUI emitted an invalid HTTP URL")
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


def _native_gui_command(launch: "GuiLaunch") -> list[str]:
    """Select the native product only after the unpacked tree is validated."""
    source = launch.cwd
    try:
        package = json.loads((source / "package.json").read_text(encoding="utf-8"))
        if (package.get("name") != "@hyprial/gui" or package.get("type") != "module"
                or package.get("engines", {}).get("node") != ">=24"
                or package.get("scripts", {}).get("start") != "node product/server.mjs"):
            raise ValueError("not the independent GUI producer")
        for relative in PRODUCT_INPUTS:
            file = source / relative
            if (not file.is_file() or file.is_symlink()
                    or not file.resolve().is_relative_to(source.resolve())):
                raise ValueError(f"missing native GUI input: {relative}")
    except (OSError, ValueError, AttributeError, TypeError) as error:
        raise GuiError(
            "GUI_PRODUCT_UNAVAILABLE",
            "Unpacked GUI is not a complete independent product; run hyprial gui upgrade to unpack a fresh copy",
        ) from error
    return [_node_executable(launch.env, source), str(source / "product/server.mjs")]


def _node_executable(environment: dict[str, str], source: Path) -> str:
    node = shutil.which("node", path=environment.get("PATH", os.defpath))
    if node is None:
        raise GuiError("GUI_START_FAILED", "The independent GUI requires Node >=24")
    try:
        result = subprocess.run(
            [node, "--version"], cwd=source, env=environment,
            capture_output=True, text=True, timeout=5, check=False,
        )
        major = int(result.stdout.strip().removeprefix("v").split(".")[0])
        if result.returncode != 0 or major < 24:
            raise ValueError("unsupported Node runtime")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise GuiError("GUI_START_FAILED", "The independent GUI requires Node >=24") from error
    return node


def _terminate_and_reap(process: subprocess.Popen[bytes], identity: str | None) -> None:
    """Bound cleanup to this launched child; never signal an unknown record."""
    if process.poll() is None:
        if identity is None:
            process.terminate()  # Fresh unreaped child, not a recovered PID record.
        else:
            _terminate_if_owned(process.pid, identity)
    try:
        process.wait(timeout=_STOP_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        if identity is not None and _owner_process_status(process.pid, identity) is not _OwnerProcessStatus.ALIVE:
            raise GuiError("GUI_PROCESS_UNKNOWN", "Cannot verify failed GUI child ownership; refusing to signal it")
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise GuiError("GUI_STOP_TIMEOUT", "Failed GUI child did not exit after bounded cleanup") from error


@dataclass(frozen=True, slots=True)
class GuiLaunch:
    """The exact launch of the unpacked, wheel-bundled GUI."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    source_commit: str


def _prepare_gui_launch(hyprial_home: Path) -> GuiLaunch:
    """Describe the launch of the unpacked GUI from its own bundled manifest.

    The wheel bundle is the authority for the launch declaration; the unpacked
    tree must still contain the declared start script as a regular file.
    """

    source = unpack.gui_source(hyprial_home)
    if not source.is_dir():
        raise GuiError(
            "GUI_PRODUCT_UNAVAILABLE",
            "the bundled GUI is not unpacked yet; run hyprial gui start or hyprial gui upgrade",
        )
    bundle = product_bundle()
    script = source.joinpath(*Path(bundle.launch[1]).parts)
    if not script.is_file() or script.is_symlink():
        raise GuiError(
            "GUI_PRODUCT_UNAVAILABLE",
            f"the unpacked GUI is missing its launch script {bundle.launch[1]!r}",
        )
    env = dict(os.environ)
    env["HYPRIAL_HOME"] = str(hyprial_home)
    env["HYPRIAL_SOURCE_COMMIT"] = bundle.commit
    env["HYPRIAL_SOURCE_VERSION"] = bundle.version
    return GuiLaunch(argv=bundle.launch, cwd=source, env=env, source_commit=bundle.commit)


def start_gui_background(hyprial_home: Path, app: str = "gui", *, component: str | None = None) -> dict[str, Any]:
    """Start one detached GUI and return only after its HTTP endpoint responds."""

    if component == "dashboard":
        raise GuiError(ipc_errors.INVALID_ARGUMENT, "Legacy GUI components are retired; use hyprial gui")
    app_root = _runtime_root(hyprial_home, app, component)
    with _lifecycle_lock(app_root):
        existing = _read_record(app_root)
        if existing is not None:
            state = _record_status(existing)
            if state == "running":
                if (existing.get("product") != "@hyprial/gui"
                        or existing.get("component") == "dashboard"):
                    raise GuiError(
                        "GUI_START_FAILED",
                        "A retired GUI process is still running; stop it before starting the independent GUI",
                    )
                return {**gui_status(hyprial_home, app, component=component), "alreadyRunning": True}
            if state == "unknown":
                raise GuiError(
                    "GUI_PROCESS_UNKNOWN",
                    "cannot verify the recorded GUI process identity; refusing to start another",
                )

        unpack.ensure_unpacked(hyprial_home, product_bundle())
        launch = _prepare_gui_launch(hyprial_home)
        command = _native_gui_command(launch)
        log_path = app_root / f"{component or app}.log"
        launch_info = app_root / f".launch-{uuid4().hex}.json"
        environment = dict(launch.env)
        environment["HYPRIAL_GUI_LAUNCH_INFO_FILE"] = str(launch_info)
        environment["HYPRIAL_GUI_NO_OPEN"] = "1"
        with log_path.open("ab", buffering=0) as log_stream:
            process = subprocess.Popen(
                command,
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
            _terminate_and_reap(process, None)
            launch_info.unlink(missing_ok=True)
            raise GuiError(
                "GUI_START_FAILED",
                f"GUI exited before its process identity was recorded; see {log_path}",
            )
        record: dict[str, Any] = {
            "schema": _PROCESS_SCHEMA,
            "pid": process.pid,
            "processIdentity": identity,
            "sourceCommit": launch.source_commit,
            "product": "@hyprial/gui",
            "component": component,
            "url": None,
            "logPath": str(log_path),
            "startedAt": _now(),
        }
        deadline = time.monotonic() + _READY_TIMEOUT
        url: str | None = None
        try:
            atomic_json_write(app_root / "process.json", record)
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
            _terminate_and_reap(process, identity)
            raise
        finally:
            launch_info.unlink(missing_ok=True)
        _terminate_and_reap(process, identity)
        raise GuiError(
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
            raise GuiError(
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
        raise GuiError(
            "GUI_STOP_TIMEOUT",
            f"GUI pid {record['pid']} did not stop within {_STOP_TIMEOUT:g}s",
        )
