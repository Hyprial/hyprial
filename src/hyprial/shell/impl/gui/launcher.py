"""Thin public facade for the shared ``gui/`` launcher.

Decision: keep the CLI-facing module free of the ``.backend`` package so
command registration and the post-install primer can load the launcher,
install, project, worktree, and termination surface without the daemon
admission graph. Backend plan, handoff, and lifecycle modules are resolved
when a launch operation actually runs; the wrappers still resolve launcher
globals at call time so existing private injection points keep working.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import GuiError
from .install import GuiAssets
from .install.report import render_startup, startup_payload  # noqa: F401 - re-export
from .project import GuiProject, _npm_prefix, inspect_project, resolve_gui_home  # noqa: F401
from .worktree import AutoGuiWorktree, discover_source_repo  # noqa: F401

if TYPE_CHECKING:
    from .backend.config import (
        BACKEND_HOST as BACKEND_HOST,
        BACKEND_READY_TIMEOUT_SECONDS as BACKEND_READY_TIMEOUT_SECONDS,
        BUILD_SCRIPT as BUILD_SCRIPT,
        FRONTEND_HEALTH_PATH as FRONTEND_HEALTH_PATH,
        FRONTEND_READY_TIMEOUT_SECONDS as FRONTEND_READY_TIMEOUT_SECONDS,
        GuiLaunchPlan as GuiLaunchPlan,
        HTTP_POLL_INTERVAL_SECONDS as HTTP_POLL_INTERVAL_SECONDS,
        PROCESS_KILL_TIMEOUT_SECONDS as PROCESS_KILL_TIMEOUT_SECONDS,
        PROCESS_STOP_TIMEOUT_SECONDS as PROCESS_STOP_TIMEOUT_SECONDS,
        READY_POLL_INTERVAL_SECONDS as READY_POLL_INTERVAL_SECONDS,
    )
    from .backend.lifecycle import GuiChild as GuiChild
    from .backend.lifecycle import GuiSession as GuiSession
    from .backend.lifecycle import PopenFactory as PopenFactory
    from .backend.lifecycle import UrlOpen as UrlOpen

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4173

_BACKEND_EXPORTS = frozenset(
    {
        "BACKEND_HOST",
        "BACKEND_READY_TIMEOUT_SECONDS",
        "BUILD_SCRIPT",
        "FRONTEND_HEALTH_PATH",
        "FRONTEND_READY_TIMEOUT_SECONDS",
        "GuiChild",
        "GuiLaunchPlan",
        "HTTP_POLL_INTERVAL_SECONDS",
        "PopenFactory",
        "PROCESS_KILL_TIMEOUT_SECONDS",
        "PROCESS_STOP_TIMEOUT_SECONDS",
        "READY_POLL_INTERVAL_SECONDS",
        "UrlOpen",
    }
)

__all__ = [
    "AutoGuiWorktree",
    "BACKEND_HOST",
    "BACKEND_READY_TIMEOUT_SECONDS",
    "BUILD_SCRIPT",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "FRONTEND_HEALTH_PATH",
    "FRONTEND_READY_TIMEOUT_SECONDS",
    "GuiAssets",
    "GuiChild",
    "GuiError",
    "GuiLaunchPlan",
    "GuiProject",
    "GuiSession",
    "HTTP_POLL_INTERVAL_SECONDS",
    "PROCESS_KILL_TIMEOUT_SECONDS",
    "PROCESS_STOP_TIMEOUT_SECONDS",
    "PopenFactory",
    "READY_POLL_INTERVAL_SECONDS",
    "UrlOpen",
    "build_plan",
    "discover_source_repo",
    "inspect_project",
    "os",
    "parse_action",
    "parse_host",
    "parse_port",
    "render_startup",
    "resolve_gui_home",
    "start_session",
    "startup_payload",
]

_GuiSession: type[Any] | None = None


def _gui_session_class() -> type[Any]:
    global _GuiSession
    if _GuiSession is None:
        from .backend.lifecycle import GuiSession as _BackendGuiSession

        class GuiSession(_BackendGuiSession):
            """Compatibility session resolving the launcher terminator at stop time."""

            def stop(self) -> None:
                if self.stopped:
                    return
                self.stopped = True
                _terminate_child(self.frontend)
                if self.assets is not None:
                    _terminate_child(self.assets)
                if self.backend is not None and self.plan.backend_owned:
                    _terminate_child(self.backend)
                self.cleanup_private_files()

        GuiSession.__module__ = __name__
        GuiSession.__qualname__ = "GuiSession"
        _GuiSession = GuiSession
        globals()["GuiSession"] = GuiSession
    return _GuiSession


def __getattr__(name: str) -> Any:
    if name == "GuiSession":
        return _gui_session_class()
    if name in _BACKEND_EXPORTS:
        from . import backend

        value = getattr(backend, name)
        globals()[name] = value
        return value
    if name in {"_config", "_handoff", "_lifecycle"}:
        from .backend import config, handoff, lifecycle

        value = {
            "_config": config,
            "_handoff": handoff,
            "_lifecycle": lifecycle,
        }[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    admit: Callable[..., Any] | None = None,
    ensure_daemon: Callable[[], Any] | None = None,
    with_dev_tools: bool = False,
    live_source: bool = False,
) -> Any:
    from .backend import config as _config

    return _config.build_plan(
        action,
        ip=ip,
        port=port,
        gui_home=gui_home,
        no_open=no_open,
        environ=environ,
        cwd=cwd,
        which=which,
        run=run,
        inspect=inspect,
        assert_port_free=_assert_port_free,
        discover_source=discover_source_repo,
        admit=admit,
        ensure_daemon=ensure_daemon,
        with_dev_tools=with_dev_tools,
        live_source=live_source,
    )


def start_session(
    plan: Any,
    *,
    json_output: bool = False,
    popen: Any = None,
    opener: Callable[[str], Any] | None = None,
    on_started: Callable[[Any], None] | None = None,
    urlopen: Any = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    build_runner: Callable[..., Any] | None = None,
    on_backend_wait: Callable[[], None] | None = None,
    on_host_exit: Callable[[int, str], None] | None = None,
) -> Any:
    from .backend import lifecycle as _lifecycle

    return _lifecycle.start_session(
        plan,
        json_output=json_output,
        popen=popen,
        opener=opener,
        on_started=on_started,
        urlopen=urlopen,
        sleep=sleep,
        monotonic=monotonic,
        build_runner=build_runner,
        run_build=_run_build,
        spawn_child=_spawn,
        wait_for_http=_wait_for_http,
        terminate_child=_terminate_child,
        session_factory=_gui_session_class(),
        on_backend_wait=on_backend_wait,
        on_host_exit=on_host_exit,
    )


def _run_build(
    plan: Any,
    *,
    json_output: bool,
    runner: Callable[..., Any] | None,
) -> None:
    from .backend import lifecycle as _lifecycle

    return _lifecycle._run_build(plan, json_output=json_output, runner=runner)


def _spawn(
    plan: Any,
    argv: tuple[str, ...],
    *,
    name: str,
    json_output: bool,
    popen: Any,
) -> Any:
    from .backend import lifecycle as _lifecycle

    return _lifecycle._spawn(
        plan,
        argv,
        name=name,
        json_output=json_output,
        popen=popen,
    )


def _wait_for_http(
    url: str,
    process: Any,
    *,
    label: str,
    timeout: float,
    urlopen: Any,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    from .backend import lifecycle as _lifecycle

    return _lifecycle._wait_for_http(
        url,
        process,
        label=label,
        timeout=timeout,
        urlopen=urlopen,
        sleep=sleep,
        monotonic=monotonic,
    )


def _terminate_child(child: Any | None) -> None:
    from .backend import lifecycle as _lifecycle

    return _lifecycle._terminate_child(child)


def _terminate_windows(child: Any) -> None:
    from .backend import lifecycle as _lifecycle

    return _lifecycle._terminate_windows(child)


def _terminate_process(process: Any) -> None:
    from .backend import lifecycle as _lifecycle

    return _lifecycle._terminate_process(process)


def _child_output(json_output: bool) -> tuple[Any, Any]:
    from .backend import handoff as _handoff

    return _handoff.child_output(json_output)


def _child_environment(
    source: Mapping[str, str],
    *,
    gui_home: Path,
) -> dict[str, str]:
    from .backend import handoff as _handoff

    return _handoff.child_environment(
        source,
        gui_home=gui_home,
    )


def _assert_port_free(host: str, port: int) -> None:
    from .backend import handoff as _handoff

    return _handoff.assert_port_free(host, port)


def _frontend_url(host: str, port: int) -> str:
    from .backend import handoff as _handoff

    return _handoff.frontend_url(host, port)


def _join_url(base: str, path: str) -> str:
    from .backend import handoff as _handoff

    return _handoff.join_url(base, path)


def parse_action(value: str | None) -> str:
    from .backend.config import parse_action as _parse_action

    return _parse_action(value)


def parse_host(value: str) -> str:
    from .backend.config import parse_host as _parse_host

    return _parse_host(value)


def parse_port(value: int) -> int:
    from .backend.config import parse_port as _parse_port

    return _parse_port(value)
