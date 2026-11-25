"""Child-process primitives used by the GUI session (spawn, wait, stop).

Decision record (H1, 2026-11-05): readiness uses the remaining startup budget
for each response. A one-second cutoff abandons legitimate catalog validation
and repeated probes can queue behind request-http's refresh lock.

Decision record (GUI start diagnostics, 2026-11-16): a start that fails with
GUI_READY_TIMEOUT or any other GuiError carries ``stages_seconds`` (elapsed
seconds at each completed stage) and, per child, its pid, exit code and a
bounded redacted tail of its private log in the error ``data``. The runtime
files are deleted on failure, so without this a child that never printed its
readiness line leaves no evidence. The keys are additive; no contract pins the
``data`` of these codes.

Decision record (Z-7 A2, 2026-11-19): the launcher no longer replaces its
backend child; a principal change is an in-place rebind.  Decision (2822, B4):
with the daemon driver there is no backend child to wait for, so the wait for a
request-http capability file is gone.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from hyprial.kernel import spawn_kwargs, terminate_process_tree

from ...errors import GuiError
from ..config import (
    BACKEND_READY_TIMEOUT_SECONDS,
    BACKEND_SLOW_NOTICE_SECONDS,
    FRONTEND_READY_TIMEOUT_SECONDS,
    HTTP_POLL_INTERVAL_SECONDS,
    PROCESS_KILL_TIMEOUT_SECONDS,
    PROCESS_STOP_TIMEOUT_SECONDS,
    READY_POLL_INTERVAL_SECONDS,
    GuiLaunchPlan,
)
from ..handoff import child_output
from .. import source as _source

PopenFactory = Callable[..., Any]
UrlOpen = Callable[..., Any]
Terminator = Callable[["GuiChild | None"], None]
_EOF = object()
HEALTH_READ_TIMEOUT_SECONDS = 5.0
FAILURE_LOG_TAIL_BYTES = 4096


@dataclass
class GuiChild:
    """A child process whose process group belongs to this invocation."""

    name: str
    process: Any
    group: int | None
    lines: queue.Queue[Any] | None = None
    redactions: set[str] = field(default_factory=set, repr=False, compare=False)
    birth: str | None = field(default=None, repr=False, compare=False)


def _spawn_captured(
    plan: GuiLaunchPlan,
    argv: tuple[str, ...],
    *,
    name: str,
    env: Mapping[str, str],
    cwd: Path,
    log_path: Path,
    popen: PopenFactory,
    mock_capture: bool = False,
    redactions: set[str] | None = None,
    log_append: bool = False,
) -> GuiChild:
    try:
        descriptor = _source.open_private_append(log_path, exclusive=not log_append)
    except OSError as error:
        raise GuiError(
            "GUI_RUNTIME_UNAVAILABLE",
            f"cannot create the private {name} log: {error}",
        ) from error
    log = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": dict(env),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "text": True,
        "bufsize": 1,
    }
    kwargs.update(spawn_kwargs())
    try:
        process = popen(argv, **kwargs)
    except OSError as error:
        log.close()
        raise GuiError(
            "GUI_START_FAILED",
            f"cannot start the {name}: {error}",
            {"argv": list(argv), "gui_home": str(cwd)},
        ) from error
    try:
        group = os.getpgid(process.pid)
    except (AttributeError, OSError):
        group = None
    stream = getattr(process, "stdout", None)
    if stream is None:
        log.close()
        _terminate_child(GuiChild(name=name, process=process, group=group))
        raise GuiError(
            "GUI_BACKEND_CONTRACT", f"the {name} has no captured output stream"
        )
    child = GuiChild(
        name=name,
        process=process,
        group=group,
        lines=queue.Queue(),
        redactions=set(redactions or ()),
    )
    thread = threading.Thread(
        target=_read_captured,
        args=(stream, child.lines, log, mock_capture, child.redactions),
        name=f"hyprial-gui-{name}-log",
        daemon=True,
    )
    thread.start()
    return child


def _read_captured(
    stream: Any,
    lines: queue.Queue[Any],
    log: Any,
    mock_capture: bool,
    redactions: set[str],
) -> None:
    try:
        for line in iter(stream.readline, ""):
            if mock_capture:
                secret, redacted, kind = _source.mock_line(line)
                lines.put((kind, secret if secret is not None else line))
                safe = _redact(redacted, redactions)
                log.write(safe if safe.endswith("\n") else safe + "\n")
            else:
                lines.put(line)
                safe = _redact(line, redactions)
                log.write(safe if safe.endswith("\n") else safe + "\n")
            log.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            log.close()
        except OSError:
            pass
        lines.put(_EOF)


def _redact(text: str, secrets: set[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _log_tail(path: Path, redactions: set[str]) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 2 * FAILURE_LOG_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return ""
    return _redact(raw.decode("utf-8", errors="replace"), redactions)[-FAILURE_LOG_TAIL_BYTES:]


def attach_failure_context(
    error: BaseException,
    *,
    children: tuple[GuiChild | None, ...],
    logs: Mapping[str, Path],
    stages: Mapping[str, float],
) -> None:
    """Add per-stage elapsed seconds and each child's private-log tail to a GuiError.

    The runtime files are removed right after a failed start, so this is the
    only place a child's own output survives; the tail is redacted again and
    bounded.
    """
    if not isinstance(error, GuiError):
        return
    context: dict[str, Any] = {"stages_seconds": dict(stages)}
    tails: dict[str, Any] = {}
    for child in children:
        if child is None or child.name not in logs:
            continue
        exit_code = child.process.poll()
        tails[child.name] = {
            "pid": getattr(child.process, "pid", None),
            "exit_code": None if exit_code is None else int(exit_code),
            "log_tail": _log_tail(logs[child.name], child.redactions),
        }
    if tails:
        context["children"] = tails
    error.data = {**error.data, **context}


def _wait_for_mock(
    plan: GuiLaunchPlan,
    backend: GuiChild,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> _source.BackendAuthority:
    lines = backend.lines
    if lines is None:
        raise GuiError(
            "GUI_BACKEND_CONTRACT", "the Contract Mock has no captured readiness stream"
        )
    endpoint: str | None = None
    token: str | None = None
    deadline = monotonic() + BACKEND_READY_TIMEOUT_SECONDS
    while endpoint is None or token is None:
        code = backend.process.poll()
        if code is not None:
            raise GuiError(
                "GUI_START_FAILED",
                "the Contract Mock exited before publishing its private endpoint",
                {"exit_code": int(code or 0)},
            )
        try:
            item = lines.get(timeout=READY_POLL_INTERVAL_SECONDS)
        except queue.Empty:
            if monotonic() >= deadline:
                raise GuiError(
                    "GUI_READY_TIMEOUT", "the Contract Mock did not become ready"
                ) from None
            sleep(READY_POLL_INTERVAL_SECONDS)
            continue
        if item is _EOF:
            raise GuiError(
                "GUI_START_FAILED", "the Contract Mock closed its private output early"
            )
        kind, value = item
        if kind == "endpoint" and isinstance(value, str):
            match = _source.MOCK_ENDPOINT_RE.fullmatch(value.strip())
            if match is not None:
                endpoint = match.group(1)
        elif kind == "token" and isinstance(value, str):
            token = value
    return _source.mock_authority(endpoint, token, plan)


def _wait_for_host_ready(
    child: GuiChild,
    parse: Callable[[str], Any],
    *,
    label: str = "GUI host",
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout: float = FRONTEND_READY_TIMEOUT_SECONDS,
    on_slow: Callable[[], None] | None = None,
) -> Any:
    lines = child.lines
    if lines is None:
        raise GuiError(
            "GUI_BACKEND_CONTRACT", f"the {label} has no captured readiness stream"
        )
    started = monotonic()
    deadline = started + timeout
    notice_at = started + BACKEND_SLOW_NOTICE_SECONDS
    while True:
        code = child.process.poll()
        if code is not None:
            raise GuiError(
                "GUI_START_FAILED",
                f"the {label} exited before its readiness line",
                {"exit_code": int(code or 0)},
            )
        try:
            line = lines.get(timeout=READY_POLL_INTERVAL_SECONDS)
        except queue.Empty:
            now = monotonic()
            if now >= deadline:
                raise GuiError(
                    "GUI_READY_TIMEOUT", f"the {label} did not become ready"
                ) from None
            if on_slow is not None and now >= notice_at:
                on_slow()
                on_slow = None
            sleep(READY_POLL_INTERVAL_SECONDS)
            continue
        if line is _EOF:
            raise GuiError(
                "GUI_START_FAILED", f"the {label} closed its readiness stream early"
            )
        if isinstance(line, str) and line.startswith(_source.HOST_READY_PREFIX):
            return parse(line.strip())


def _confirm_installed_assets(
    plan: GuiLaunchPlan, *, urlopen: UrlOpen, timeout: float = HEALTH_READ_TIMEOUT_SECONDS
) -> None:
    if not plan.project.installed:
        return
    url = plan.frontend_health_url
    try:
        with urlopen(url, timeout=timeout) as response:
            health = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise GuiError(
            "GUI_ASSETS_INVALID",
            f"cannot confirm that the GUI host verified the installed assets: {error}",
            {"url": url},
        ) from error
    if (
        not isinstance(health, dict)
        or health.get("assets") != "installed"
        or health.get("assetsVerified") is not True
    ):
        raise GuiError(
            "GUI_ASSETS_INVALID",
            "the GUI host did not report verified installed assets; refusing to hand out a launch URL",
            {"url": url},
        )


def _authorized_urlopen(urlopen: UrlOpen, token: str) -> Callable[..., Any]:
    def open_authorized(url: str, *, timeout: float) -> Any:
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        return urlopen(request, timeout=timeout)

    return open_authorized


def _cleanup_runtime_files(lease: _source.RuntimeLease | None) -> None:
    if lease is None:
        return
    for name in (
        "request-http.json",
        "server.json",
        "host.log",
        "assets.log",
        "mock.log",
        "request-http.log",
        "launch.url",
        "launch.ready",
    ):
        try:
            (lease.directory / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    lease.close()


def _run_build(
    plan: GuiLaunchPlan,
    *,
    json_output: bool,
    runner: Callable[..., Any] | None,
) -> None:
    """Run the production build before serving a normal-mode session."""
    argv = plan.build_argv
    if argv is None:
        return
    kwargs: dict[str, Any] = {
        "cwd": str(plan.project.home),
        "env": _source.build_environment(plan.build_env or plan.env),
        "stdin": subprocess.DEVNULL,
    }
    if json_output:
        kwargs["stdout"] = sys.stderr
        kwargs["stderr"] = subprocess.STDOUT
    if runner is None:
        returncode = _run_owned_build(plan, argv, kwargs)
    else:
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


def _run_owned_build(
    plan: GuiLaunchPlan,
    argv: tuple[str, ...],
    kwargs: dict[str, Any],
) -> int:
    kwargs.update(spawn_kwargs())
    try:
        process = subprocess.Popen(argv, **kwargs)
    except OSError as error:
        raise GuiError(
            "GUI_BUILD_FAILED",
            f"cannot run the GUI production build: {error}",
            {"argv": list(argv), "gui_home": str(plan.project.home)},
        ) from error
    try:
        group = os.getpgid(process.pid)
    except (AttributeError, OSError):
        group = None
    child = GuiChild("build", process, group)
    try:
        return int(process.wait())
    except BaseException:
        _terminate_child(child)
        raise


def _spawn(
    plan: GuiLaunchPlan,
    argv: tuple[str, ...],
    *,
    name: str,
    json_output: bool,
    popen: PopenFactory,
) -> GuiChild:
    stdout, stderr = child_output(json_output)
    kwargs: dict[str, Any] = {
        "cwd": str(plan.project.home),
        "env": dict(plan.env),
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "close_fds": True,
    }
    kwargs.update(spawn_kwargs())
    try:
        process = popen(argv, **kwargs)
    except OSError as error:
        raise GuiError(
            "GUI_START_FAILED",
            f"cannot start the {name}: {error}",
            {"argv": list(argv), "gui_home": str(plan.project.home)},
        ) from error
    try:
        group = os.getpgid(process.pid)
    except (AttributeError, OSError):
        group = None
    return GuiChild(name=name, process=process, group=group)


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
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            with urlopen(url, timeout=remaining) as response:
                status = int(getattr(response, "status", 200))
            if 200 <= status < 400 and monotonic() < deadline:
                return
        except urllib.error.HTTPError as error:
            if 200 <= int(error.code) < 400 and monotonic() < deadline:
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(HTTP_POLL_INTERVAL_SECONDS, remaining))
    raise GuiError(
        "GUI_READY_TIMEOUT",
        f"the {label} did not become ready at {url} within {timeout:g}s",
        {"url": url, "timeout_seconds": timeout},
    )


def _terminate_child(child: GuiChild | None) -> None:
    """Stop one supervised child and its recorded process group."""

    if child is None:
        return
    terminate_process_tree(
        child.process,
        child.group,
        stop_timeout=PROCESS_STOP_TIMEOUT_SECONDS,
        kill_timeout=PROCESS_KILL_TIMEOUT_SECONDS,
    )


def _terminate_windows(child: GuiChild) -> None:
    """Retained name: stop a Windows child tree through the kernel seam."""

    terminate_process_tree(
        child.process,
        child.group,
        stop_timeout=PROCESS_STOP_TIMEOUT_SECONDS,
        kill_timeout=PROCESS_KILL_TIMEOUT_SECONDS,
    )


def _terminate_process(process: Any) -> None:
    """Retained name: stop one process with no recorded group."""

    terminate_process_tree(
        process,
        None,
        stop_timeout=PROCESS_STOP_TIMEOUT_SECONDS,
        kill_timeout=PROCESS_KILL_TIMEOUT_SECONDS,
    )



__all__ = [
    "GuiChild",
    "PopenFactory",
    "UrlOpen",
]
