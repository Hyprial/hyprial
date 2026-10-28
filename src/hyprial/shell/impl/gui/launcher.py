"""Minimal foreground launcher for the shared ``gui/`` web application.

This module deliberately does not resurrect the retired Python GUI host or the
old packaged ``src/gui`` product.  It starts the Vite/npm frontend from a GUI
project checkout and an independent Node placeholder service, passes their two
endpoints to the child processes, and owns only those child process groups.

The launcher is intentionally small: it validates the selected Git worktree,
does not write into it, and never discovers or stops processes by name or
port.  Git worktrees are a development convenience, not a security sandbox.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from hyprial.kernel import ipc_errors

from .errors import GuiError
from .project import (
    GuiProject,
    _npm_prefix,
    inspect_project,
    resolve_gui_home,
)
from .worktree import AutoGuiWorktree, auto_worktree_payload, ensure_dev_worktree

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4173
BACKEND_HOST = "127.0.0.1"
BUILD_SCRIPT = "build"
STUB_SCRIPT = "stub"
STUB_READY_PREFIX = "HYPRIAL_STUB_READY "
STUB_HEALTH_PATH = "/healthz"
FRONTEND_HEALTH_PATH = "/__gui/healthz"
FRONTEND_READY_TIMEOUT_SECONDS = 30.0
BACKEND_READY_TIMEOUT_SECONDS = 15.0
HTTP_POLL_INTERVAL_SECONDS = 0.2
STUB_READY_POLL_SECONDS = 0.1
STUB_EXIT_FLUSH_SECONDS = 1.0
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
PROCESS_KILL_TIMEOUT_SECONDS = 2.0
NOT_IMPLEMENTED_TEXT = "not implemented: hyyprial backend not wired"

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_STUB_EOF = object()

PopenFactory = Callable[..., Any]
UrlOpen = Callable[..., Any]


@dataclass(frozen=True)
class GuiLaunchPlan:
    """Everything needed to start one GUI session."""

    mode: str
    project: GuiProject
    host: str
    port: int
    url: str
    backend_endpoint: str | None
    backend_owned: bool
    backend_argv: tuple[str, ...] | None
    frontend_argv: tuple[str, ...]
    env: Mapping[str, str]
    open_browser: bool
    build_argv: tuple[str, ...] | None = None
    auto_worktree: AutoGuiWorktree | None = None

    @property
    def backend_health_url(self) -> str:
        if self.backend_endpoint is None:
            raise GuiError(
                "GUI_BACKEND_CONTRACT",
                "the placeholder backend endpoint is not available yet",
            )
        return _join_url(self.backend_endpoint, STUB_HEALTH_PATH)

    @property
    def frontend_health_url(self) -> str:
        return _join_url(self.url, FRONTEND_HEALTH_PATH)

    @property
    def hot_reload(self) -> bool:
        return self.mode == "dev"


@dataclass
class GuiChild:
    """A child process whose process group belongs to this invocation."""

    name: str
    process: Any
    group: int | None


@dataclass
class GuiSession:
    """A running foreground GUI session."""

    plan: GuiLaunchPlan
    frontend: GuiChild
    backend: GuiChild | None
    stopped: bool = False

    def wait(self, *, sleep: Callable[[float], None] = time.sleep) -> int:
        """Wait for the frontend to exit, returning its exit status.

        KeyboardInterrupt is intentionally allowed to propagate so the CLI
        turns it into exit status 130 after ``stop`` has reclaimed this
        invocation's children.
        """
        while True:
            frontend_code = self.frontend.process.poll()
            if frontend_code is not None:
                return int(frontend_code or 0)
            if self.backend is not None and self.plan.backend_owned:
                backend_code = self.backend.process.poll()
                if backend_code is not None:
                    raise GuiError(
                        "GUI_BACKEND_EXITED",
                        "the placeholder backend exited unexpectedly; stopping the GUI",
                        {
                            "backend_pid": self.backend.process.pid,
                            "backend_exit_code": int(backend_code or 0),
                        },
                    )
            sleep(HTTP_POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        """Reclaim only the frontend/backend process groups started here."""
        if self.stopped:
            return
        self.stopped = True
        # Frontend first: a dev server may otherwise keep a backend request
        # open while we are waiting for the backend to drain.
        _terminate_child(self.frontend)
        if self.backend is not None and self.plan.backend_owned:
            _terminate_child(self.backend)


def parse_action(value: str | None) -> str:
    """Map the public leaf grammar to a launch mode.

    The only public forms are the bare ``hyprial gui`` and ``hyprial gui dev``.
    Retired verbs are deliberately not accepted for compatibility.
    """
    normalized = (value or "").strip().lower()
    if normalized == "":
        return "normal"
    if normalized == "dev":
        return "dev"
    raise GuiError(
        ipc_errors.INVALID_ARGUMENT,
        "Use `hyprial gui` for the normal GUI or `hyprial gui dev` for the "
        "Vite development server; start/status/stop/upgrade are retired.",
    )


def parse_host(value: str) -> str:
    """Validate ``--ip`` without accepting an arbitrary command-line hostname."""
    normalized = (value or DEFAULT_HOST).strip()
    if normalized.lower() == "localhost":
        return DEFAULT_HOST
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError as error:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            f"--ip must be an IP address or localhost, not {value!r}",
        ) from error


def parse_port(value: int) -> int:
    """Validate ``--port``; port 0 is not usable because the URL is printed."""
    if not isinstance(value, int) or not 1 <= value <= 65535:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "--port must be an integer from 1 to 65535",
        )
    return value


def build_plan(
    action: str,
    *,
    ip: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    gui_home: Path | None = None,
    no_open: bool = False,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
    inspect: Callable[..., GuiProject] = inspect_project,
) -> GuiLaunchPlan:
    """Build a launch plan and fail before starting any child on bad input."""
    mode = parse_action(action)
    host = parse_host(ip)
    frontend_port = parse_port(port)
    source = dict(os.environ if environ is None else environ)
    auto: AutoGuiWorktree | None = None
    explicit_home = gui_home is not None or bool(
        str(source.get("HYPRIAL_GUI_HOME") or "").strip()
    )
    if mode == "dev" and not explicit_home:
        auto = ensure_dev_worktree(cwd=cwd, environ=source, which=which, run=run)
        home = auto.home
    else:
        home = resolve_gui_home(gui_home, environ=source, cwd=cwd)
    project = inspect(home, which=which, run=run)

    frontend_script = "dev" if mode == "dev" else "start"
    if frontend_script not in project.scripts:
        raise GuiError(
            "GUI_SCRIPT_MISSING",
            f"{project.home / 'package.json'} has no `{frontend_script}` script",
            {"gui_home": str(project.home), "script": frontend_script},
        )

    preset_endpoint = str(source.get("HYPRIAL_BACKEND_ENDPOINT") or "").strip()
    if preset_endpoint:
        backend_endpoint: str | None = _loopback_endpoint(preset_endpoint)
        backend_owned = False
        backend_argv: tuple[str, ...] | None = None
    else:
        if STUB_SCRIPT not in project.scripts:
            raise GuiError(
                "GUI_BACKEND_SCRIPT_MISSING",
                f"{project.home / 'package.json'} has no `{STUB_SCRIPT}` script "
                "for the independent placeholder service",
                {"gui_home": str(project.home), "script": STUB_SCRIPT},
            )
        # Port 0 is the stub's canonical contract: the OS assigns a free port
        # and the exact HYPRIAL_STUB_READY line tells us which one.  This
        # removes the reserve-then-bind race that an explicit port creates.
        backend_endpoint = None
        backend_owned = True
        backend_argv = tuple(
            [
                *_npm_prefix(project.npm),
                "run",
                STUB_SCRIPT,
                "--",
                "--host",
                BACKEND_HOST,
                "--port",
                "0",
            ]
        )

    _assert_port_free(host, frontend_port)

    url = _frontend_url(host, frontend_port)
    child_env = _child_environment(
        source,
        gui_home=project.home,
        backend_endpoint=backend_endpoint,
    )
    frontend_args = ["start"] if mode == "normal" else ["run", "dev"]
    frontend_argv = tuple(
        [
            *_npm_prefix(project.npm),
            *frontend_args,
            "--",
            "--host",
            host,
            "--port",
            str(frontend_port),
            "--strict-port",
        ]
    )
    # The delivered UI serves a previously built dist/ in normal mode.  Build
    # it in the foreground first so a clean source worktree needs no manual
    # setup step; dev mode lets Vite compile the source directly.
    build_argv = (
        (*_npm_prefix(project.npm), "run", BUILD_SCRIPT)
        if mode == "normal" and BUILD_SCRIPT in project.scripts
        else None
    )
    return GuiLaunchPlan(
        mode=mode,
        project=project,
        host=host,
        port=frontend_port,
        url=url,
        backend_endpoint=backend_endpoint,
        backend_owned=backend_owned,
        backend_argv=backend_argv,
        frontend_argv=frontend_argv,
        env=child_env,
        open_browser=not no_open,
        build_argv=build_argv,
        auto_worktree=auto,
    )


def start_session(
    plan: GuiLaunchPlan,
    *,
    json_output: bool = False,
    popen: PopenFactory | None = None,
    opener: Callable[[str], Any] | None = None,
    on_started: Callable[[GuiSession], None] | None = None,
    urlopen: UrlOpen = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    build_runner: Callable[..., Any] | None = None,
) -> GuiSession:
    """Build, start the placeholder backend and frontend, wait for readiness.

    A normal-mode production build runs first.  On any failure the
    already-started children are reclaimed before the error is returned to the
    CLI.
    """
    spawn = popen or subprocess.Popen
    backend: GuiChild | None = None
    frontend: GuiChild | None = None
    active_plan = plan
    if plan.build_argv is not None:
        _run_build(
            plan,
            json_output=json_output,
            runner=build_runner or subprocess.run,
        )
    try:
        if plan.backend_argv is not None:
            backend, endpoint = _spawn_stub(
                plan,
                plan.backend_argv,
                popen=spawn,
                sleep=sleep,
                monotonic=monotonic,
            )
            active_plan = replace(
                plan,
                backend_endpoint=endpoint,
                env=_with_backend_endpoint(plan.env, endpoint),
            )
            _wait_for_http(
                active_plan.backend_health_url,
                backend.process,
                label="placeholder backend",
                timeout=BACKEND_READY_TIMEOUT_SECONDS,
                urlopen=urlopen,
                sleep=sleep,
                monotonic=monotonic,
            )
        frontend = _spawn(
            active_plan,
            active_plan.frontend_argv,
            name="frontend",
            json_output=json_output,
            popen=spawn,
        )
        _wait_for_http(
            active_plan.frontend_health_url,
            frontend.process,
            label="GUI frontend",
            timeout=FRONTEND_READY_TIMEOUT_SECONDS,
            urlopen=urlopen,
            sleep=sleep,
            monotonic=monotonic,
        )
        session = GuiSession(plan=active_plan, frontend=frontend, backend=backend)
        if on_started is not None:
            on_started(session)
        if plan.open_browser:
            open_ = opener or webbrowser.open
            try:
                open_(plan.url)
            except Exception:
                # Opening a browser is a convenience; the URL is still printed.
                pass
        return session
    except BaseException:
        _terminate_child(frontend)
        if plan.backend_owned:
            _terminate_child(backend)
        raise


def startup_payload(session: GuiSession) -> dict[str, Any]:
    """The one machine-readable startup record emitted before the wait."""
    plan = session.plan
    return {
        "ok": True,
        "mode": plan.mode,
        "gui_home": str(plan.project.home),
        "git_worktree": str(plan.project.worktree),
        "git_branch": plan.project.branch,
        "url": plan.url,
        "host": plan.host,
        "port": plan.port,
        "hot_reload": plan.hot_reload,
        "backend_endpoint": plan.backend_endpoint or "",
        "backend_owned": plan.backend_owned,
        "frontend_pid": session.frontend.process.pid,
        "backend_pid": (
            session.backend.process.pid if session.backend is not None else None
        ),
        "node_version": plan.project.node_version,
        "npm_version": plan.project.npm_version,
        "auto_worktree": auto_worktree_payload(plan.auto_worktree),
        "business": NOT_IMPLEMENTED_TEXT,
        "note": (
            "The GUI uses the independent Node placeholder boundary; "
            "business calls are not wired to a daemon."
        ),
    }


def render_startup(payload: Mapping[str, Any]) -> str:
    """Human output for a running session; the URL is stable and copyable."""
    lines = [
        f"Hyprial GUI ({payload['mode']})",
        f"  GUI_HOME : {payload['gui_home']}",
        f"  Git      : {payload['git_branch']} at {payload['git_worktree']}",
        f"  URL      : {payload['url']}",
        f"  backend  : {payload['backend_endpoint']} "
        f"({'owned by CLI' if payload['backend_owned'] else 'external, loopback only'})",
        f"  hot reload: {'enabled' if payload['hot_reload'] else 'not used'}",
        f"  business : {payload['business']}",
        "  stop     : Ctrl-C (only this invocation's processes are reclaimed)",
    ]
    return "\n".join(lines)


def _run_build(
    plan: GuiLaunchPlan,
    *,
    json_output: bool,
    runner: Callable[..., Any],
) -> None:
    """Run the production build before serving a normal-mode session.

    JSON stdout is reserved for the single startup record, so build output goes
    to stderr in that mode.  Any failure leaves no child process behind.
    """
    argv = plan.build_argv
    if argv is None:
        return
    kwargs: dict[str, Any] = {
        "cwd": str(plan.project.home),
        "env": dict(plan.env),
        "stdin": subprocess.DEVNULL,
    }
    if json_output:
        kwargs["stdout"] = sys.stderr
        kwargs["stderr"] = subprocess.STDOUT
    try:
        result = runner(argv, **kwargs)
    except OSError as error:
        raise GuiError(
            "GUI_BUILD_FAILED",
            f"cannot run the GUI production build: {error}",
            {"argv": list(argv), "gui_home": str(plan.project.home)},
        ) from error
    returncode = getattr(result, "returncode", result)
    if returncode != 0:
        raise GuiError(
            "GUI_BUILD_FAILED",
            f"the GUI production build failed (exit {returncode})",
            {
                "argv": list(argv),
                "gui_home": str(plan.project.home),
                "returncode": returncode,
            },
        )


def _spawn(
    plan: GuiLaunchPlan,
    argv: tuple[str, ...],
    *,
    name: str,
    json_output: bool,
    popen: PopenFactory,
) -> GuiChild:
    stdout, stderr = _child_output(json_output)
    kwargs: dict[str, Any] = {
        "cwd": str(plan.project.home),
        "env": dict(plan.env),
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        process = popen(argv, **kwargs)
    except OSError as error:
        raise GuiError(
            "GUI_START_FAILED",
            f"cannot start the {name}: {error}",
            {"argv": list(argv), "gui_home": str(plan.project.home)},
        ) from error
    group = None
    if os.name != "nt":
        try:
            process_group = os.getpgid(process.pid)
        except (AttributeError, OSError):
            process_group = None
        group = process_group
    return GuiChild(name=name, process=process, group=group)


def _spawn_stub(
    plan: GuiLaunchPlan,
    argv: tuple[str, ...],
    *,
    popen: PopenFactory,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[GuiChild, str]:
    """Start the Node stub, read its exact ready line, and return its endpoint."""
    kwargs: dict[str, Any] = {
        "cwd": str(plan.project.home),
        "env": dict(plan.env),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": None,
        "close_fds": True,
        "text": True,
        "bufsize": 1,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        process = popen(argv, **kwargs)
    except OSError as error:
        raise GuiError(
            "GUI_START_FAILED",
            f"cannot start the placeholder backend: {error}",
            {"argv": list(argv), "gui_home": str(plan.project.home)},
        ) from error
    group = None
    if os.name != "nt":
        try:
            group = os.getpgid(process.pid)
        except (AttributeError, OSError):
            group = None
    child = GuiChild(name="backend", process=process, group=group)
    lines: queue.Queue[Any] = queue.Queue()
    stream = getattr(process, "stdout", None)
    if stream is None:
        _terminate_child(child)
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend did not expose its readiness stream",
        )
    thread = threading.Thread(
        target=_read_stub_lines,
        args=(stream, lines),
        name="hyprial-gui-stub-reader",
        daemon=True,
    )
    thread.start()
    try:
        return child, _wait_for_stub_ready(
            child,
            lines,
            sleep=sleep,
            monotonic=monotonic,
        )
    except BaseException:
        _terminate_child(child)
        raise


def _read_stub_lines(stream: Any, lines: queue.Queue[Any]) -> None:
    """Drain the stub pipe; forward non-contract diagnostics to stderr."""
    try:
        for line in iter(stream.readline, ""):
            if line.startswith(STUB_READY_PREFIX):
                lines.put(line)
                continue
            lines.put(line)
            try:
                sys.stderr.write(line)
                sys.stderr.flush()
            except (AttributeError, OSError, ValueError):
                pass
    except (OSError, ValueError):
        pass
    finally:
        lines.put(_STUB_EOF)


def _wait_for_stub_ready(
    child: GuiChild,
    lines: queue.Queue[Any],
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> str:
    deadline = monotonic() + BACKEND_READY_TIMEOUT_SECONDS
    while True:
        if child.process.poll() is not None:
            item = _stub_item_after_exit(lines, sleep=sleep, monotonic=monotonic)
            if isinstance(item, str) and item.startswith(STUB_READY_PREFIX):
                return _parse_stub_ready(item)
            raise GuiError(
                "GUI_START_FAILED",
                "the placeholder backend exited before printing HYPRIAL_STUB_READY",
                {"exit_code": int(child.process.poll() or 0)},
            )
        try:
            item = lines.get(timeout=STUB_READY_POLL_SECONDS)
        except queue.Empty:
            if monotonic() >= deadline:
                raise GuiError(
                    "GUI_READY_TIMEOUT",
                    "the placeholder backend did not print HYPRIAL_STUB_READY "
                    f"within {BACKEND_READY_TIMEOUT_SECONDS:g}s",
                    {"timeout_seconds": BACKEND_READY_TIMEOUT_SECONDS},
                ) from None
            sleep(STUB_READY_POLL_SECONDS)
            continue
        if item is _STUB_EOF:
            raise GuiError(
                "GUI_START_FAILED",
                "the placeholder backend closed stdout before HYPRIAL_STUB_READY",
            )
        if isinstance(item, str) and item.startswith(STUB_READY_PREFIX):
            return _parse_stub_ready(item)
        if monotonic() >= deadline:
            raise GuiError(
                "GUI_READY_TIMEOUT",
                "the placeholder backend did not print HYPRIAL_STUB_READY "
                f"within {BACKEND_READY_TIMEOUT_SECONDS:g}s",
                {"timeout_seconds": BACKEND_READY_TIMEOUT_SECONDS},
            )


def _stub_item_after_exit(
    lines: queue.Queue[Any],
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> Any:
    """Let the reader thread flush after process exit before declaring failure.

    A process can exit immediately after writing its ready line; observing the
    exit before the reader thread queues that line must not discard a valid
    handshake.
    """
    deadline = monotonic() + STUB_EXIT_FLUSH_SECONDS
    while True:
        try:
            item = lines.get(timeout=STUB_READY_POLL_SECONDS)
        except queue.Empty:
            if monotonic() >= deadline:
                return None
            sleep(STUB_READY_POLL_SECONDS)
            continue
        if item is _STUB_EOF:
            return None
        if isinstance(item, str) and item.startswith(STUB_READY_PREFIX):
            return item
        if monotonic() >= deadline:
            return None


def _parse_stub_ready(line: str) -> str:
    raw = line[len(STUB_READY_PREFIX) :].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend readiness line is not valid JSON",
        ) from error
    if not isinstance(payload, dict):
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend readiness line must be a JSON object",
        )
    if "ok" in payload and payload.get("ok") is not True:
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend readiness line has an unexpected ok flag",
        )
    if payload.get("service") != "hyprial-gui-stub":
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend readiness line has an unexpected service",
        )
    host = payload.get("host")
    port = payload.get("port")
    endpoint = payload.get("endpoint")
    if not isinstance(host, str) or not _is_loopback(host):
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend must report a loopback host",
        )
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend must report a valid port",
        )
    if not isinstance(endpoint, str):
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend must report its endpoint",
        )
    try:
        normalized = _loopback_endpoint(endpoint)
    except GuiError as error:
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend endpoint must be a loopback base URL",
        ) from error
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.hostname != host or parsed.port != port:
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the placeholder backend endpoint does not match its host and port",
        )
    return normalized


def _with_backend_endpoint(environment: Mapping[str, str], endpoint: str) -> dict[str, str]:
    updated = dict(environment)
    updated["HYPRIAL_BACKEND_ENDPOINT"] = endpoint
    return updated


def _child_output(json_output: bool) -> tuple[Any, Any]:
    if not json_output:
        return None, None
    try:
        sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL, subprocess.DEVNULL
    return sys.stderr, sys.stderr


def _wait_for_http(
    url: str,
    process: Any,
    *,
    label: str,
    timeout: float,
    urlopen: UrlOpen,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    deadline = monotonic() + timeout
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            raise GuiError(
                "GUI_START_FAILED",
                f"the {label} exited before it became ready",
                {"url": url, "exit_code": int(exit_code or 0)},
            )
        try:
            with urlopen(url, timeout=1.0) as response:
                status = int(getattr(response, "status", 200))
            if 200 <= status < 400:
                return
        except urllib.error.HTTPError as error:
            if 200 <= int(error.code) < 400:
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if monotonic() >= deadline:
            raise GuiError(
                "GUI_READY_TIMEOUT",
                f"the {label} did not become ready at {url} within {timeout:g}s",
                {"url": url, "timeout_seconds": timeout},
            )
        sleep(HTTP_POLL_INTERVAL_SECONDS)


def _terminate_child(child: GuiChild | None) -> None:
    if child is None:
        return
    process = child.process
    if os.name == "nt":
        try:
            if process.poll() is not None:
                return
        except (AttributeError, OSError):
            return
        _terminate_windows(child)
        return
    if child.group is None:
        try:
            if process.poll() is not None:
                return
        except (AttributeError, OSError):
            return
        _terminate_process(process)
        return
    if not _group_alive(child.group):
        process.poll()
        return
    for signal_number, timeout in (
        (signal.SIGINT, PROCESS_STOP_TIMEOUT_SECONDS),
        (signal.SIGTERM, PROCESS_STOP_TIMEOUT_SECONDS),
        (signal.SIGKILL, PROCESS_KILL_TIMEOUT_SECONDS),
    ):
        if not _group_alive(child.group):
            return
        try:
            os.killpg(child.group, signal_number)
        except ProcessLookupError:
            return
        except OSError:
            _terminate_process(process)
            return
        deadline = time.monotonic() + timeout
        while _group_alive(child.group) and time.monotonic() < deadline:
            # poll() reaps the direct npm child without signalling anybody
            # else; the process group is checked separately so an npm wrapper
            # that exits first cannot strand a dev server it started.
            process.poll()
            time.sleep(0.05)
        if not _group_alive(child.group):
            return
    process.poll()


def _group_alive(group: int) -> bool:
    """Whether the already-recorded child process group still has members."""
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_windows(child: GuiChild) -> None:
    process = child.process
    try:
        # /T is required for npm.cmd -> node -> vite.  Doing this first also
        # covers the case where the npm wrapper would exit before its child.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROCESS_STOP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.terminate()
        process.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminate_process(process: Any) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        process.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
    except (OSError, AttributeError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
        except (OSError, AttributeError, subprocess.TimeoutExpired):
            pass


def _child_environment(
    source: Mapping[str, str],
    *,
    gui_home: Path,
    backend_endpoint: str | None,
) -> dict[str, str]:
    """Return a child-only environment; the parent process env is untouched.

    The UI contract reads ``HYPRIAL_GUI_HOME`` and the unprefixed endpoint via
    Vite's ``loadEnv(mode, root, '')``/``process.env``; no VITE_ duplicate is
    invented here because the browser only calls same-origin ``/api/gui/v1``.
    """
    environment = dict(source)
    environment["HYPRIAL_GUI_HOME"] = str(gui_home)
    if backend_endpoint is None:
        environment.pop("HYPRIAL_BACKEND_ENDPOINT", None)
    else:
        environment["HYPRIAL_BACKEND_ENDPOINT"] = backend_endpoint
    return environment


def _assert_port_free(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            # Match Node's POSIX bind semantics: closed connections in
            # TIME_WAIT must not prevent restarting the preview. Windows
            # SO_REUSEADDR can permit binding over a live listener; avoid it.
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise GuiError("GUI_PORT_IN_USE",
                f"GUI port {host}:{port} is already in use; choose another --port",
                {"host": host, "port": port},
            ) from error
        raise GuiError("GUI_PORT_UNAVAILABLE",
            f"cannot bind GUI port {host}:{port}: {error}",
            {"host": host, "port": port},
        ) from error


def _frontend_url(host: str, port: int) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in display_host:
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def _loopback_endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "HYPRIAL_BACKEND_ENDPOINT must be an http(s) loopback URL",
        )
    if parsed.username is not None or parsed.password is not None:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "HYPRIAL_BACKEND_ENDPOINT must not contain credentials",
        )
    if not _is_loopback(parsed.hostname):
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "HYPRIAL_BACKEND_ENDPOINT must point at a loopback placeholder service",
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "HYPRIAL_BACKEND_ENDPOINT must be a base URL without path, query, or fragment",
        )
    try:
        endpoint_port = parsed.port
    except ValueError as error:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "HYPRIAL_BACKEND_ENDPOINT has an invalid port",
        ) from error
    if endpoint_port is not None and not 1 <= endpoint_port <= 65535:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "HYPRIAL_BACKEND_ENDPOINT has an invalid port",
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"
