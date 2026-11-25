"""
GUI session state: one foreground GUI, its owned children and the stop proof.

The process primitives (spawn, readiness waits, termination) live in
``process``; this module owns the session dataclasses and start/stop decisions.
"""

from __future__ import annotations

import queue
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hyprial.kernel import process_birth_identity  # noqa: F401  (patch surface)

from ...errors import GuiError
from ..config import (
    BACKEND_READY_TIMEOUT_SECONDS,
    FRONTEND_READY_TIMEOUT_SECONDS,
    HTTP_POLL_INTERVAL_SECONDS,
    GuiLaunchPlan,
)
from ..handoff import open_browser
from .. import source as _source


from . import process as _process
from .process import GuiChild

PopenFactory = Callable[..., Any]
UrlOpen = Callable[..., Any]
Terminator = Callable[["GuiChild | None"], None]

def _drain_captured_output(child: GuiChild) -> None:
    """Wait, bounded, until the child's log reader has written its last line."""
    lines = child.lines
    if lines is None:
        return
    deadline = time.monotonic() + _process.PROCESS_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            item = lines.get(timeout=HTTP_POLL_INTERVAL_SECONDS)
        except queue.Empty:
            continue
        if item is _process._EOF:
            return


@dataclass
class GuiSession:
    """A running foreground GUI session."""

    plan: GuiLaunchPlan
    frontend: GuiChild
    backend: GuiChild | None
    assets: GuiChild | None = field(default=None, kw_only=True)
    stopped: bool = False
    handoff_path: Path | None = None
    runtime_lease: _source.RuntimeLease | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _terminate: Terminator | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    on_host_exit: Callable[[int, str], None] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def wait(self, *, sleep: Callable[[float], None] = time.sleep) -> int:
        """Wait for the frontend to exit, returning its exit status."""
        while True:
            frontend_code = self.frontend.process.poll()
            if frontend_code is not None:
                if int(frontend_code or 0) != 0:
                    self._report_host_exit(int(frontend_code))
                return int(frontend_code or 0)
            if self.assets is not None:
                assets_code = self.assets.process.poll()
                if assets_code is not None:
                    raise GuiError(
                        "GUI_ASSETS_EXITED",
                        "the GUI development server exited unexpectedly; stopping the GUI",
                        {
                            "assets_pid": self.assets.process.pid,
                            "assets_exit_code": int(assets_code or 0),
                        },
                    )
            if self.backend is not None and self.plan.backend_owned:
                backend_code = self.backend.process.poll()
                if backend_code is not None:
                    raise GuiError(
                        "GUI_BACKEND_EXITED",
                        "the GUI backend exited unexpectedly; stopping the GUI",
                        {
                            "backend_pid": self.backend.process.pid,
                            "backend_exit_code": int(backend_code or 0),
                        },
                    )
            sleep(HTTP_POLL_INTERVAL_SECONDS)

    def _report_host_exit(self, code: int) -> None:
        """Hand the redacted log tail of a host that ended on its own to the caller.

        The runtime files, host.log included, are removed right after the wait,
        so the tail is read here while they still exist.
        """
        if self.on_host_exit is None:
            return
        _drain_captured_output(self.frontend)
        tail = ""
        if self.runtime_lease is not None:
            tail = _process._log_tail(
                _source.host_log_path(self.runtime_lease), self.frontend.redactions
            )
        self.on_host_exit(code, tail)

    def stop(self) -> None:
        """Reclaim only the host, dev-server and backend process groups started here."""
        if self.stopped:
            return
        terminate = self._terminate or _process._terminate_child
        failure: BaseException | None = None
        for child in (
            self.frontend,
            self.assets,
            self.backend if self.plan.backend_owned else None,
        ):
            if child is not None:
                try:
                    terminate(child)
                except BaseException as exc:
                    failure = failure or exc
        self.cleanup_private_files()
        self.stopped = failure is None
        if failure is not None:
            raise failure

    def cleanup_private_files(self) -> None:
        _source.remove_handoff(self.runtime_lease)
        _process._cleanup_runtime_files(self.runtime_lease)
        self.handoff_path = None


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
    run_build: Callable[..., None] | None = None,
    spawn_child: Callable[..., GuiChild] | None = None,
    wait_for_http: Callable[..., None] | None = None,
    terminate_child: Terminator | None = None,
    session_factory: Callable[..., GuiSession] = GuiSession,
    on_backend_wait: Callable[[], None] | None = None,
    on_host_exit: Callable[[int, str], None] | None = None,
) -> GuiSession:
    """Build and start the admitted stack: the mock if selected, then the host.

    ``on_backend_wait`` is called at most once when the host is slower to print
    its readiness line than a normal start, so the caller can say so.
    ``on_host_exit(exit_code, log_tail)`` is called once by ``GuiSession.wait``
    when the host ends on its own with a non-zero status, before the runtime
    files are removed; the launcher still returns that status.
    """
    run_build = run_build or _process._run_build
    spawn_child = spawn_child or _process._spawn
    wait_for_http = wait_for_http or _process._wait_for_http
    terminate_child = terminate_child or _process._terminate_child
    spawn = popen or subprocess.Popen
    return _start_source_session(
        plan,
        json_output=json_output,
        popen=spawn,
        opener=opener,
        on_started=on_started,
        urlopen=urlopen,
        sleep=sleep,
        monotonic=monotonic,
        build_runner=build_runner,
        run_build=run_build,
        spawn_child=spawn_child,
        wait_for_http=wait_for_http,
        terminate_child=terminate_child,
        session_factory=session_factory,
        on_backend_wait=on_backend_wait,
        on_host_exit=on_host_exit,
    )


def _start_source_session(
    plan: GuiLaunchPlan,
    *,
    json_output: bool,
    popen: PopenFactory,
    opener: Callable[[str], Any] | None,
    on_started: Callable[[GuiSession], None] | None,
    urlopen: UrlOpen,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    build_runner: Callable[..., Any] | None,
    run_build: Callable[..., None],
    spawn_child: Callable[..., GuiChild],
    wait_for_http: Callable[..., None],
    terminate_child: Terminator,
    session_factory: Callable[..., GuiSession],
    on_backend_wait: Callable[[], None] | None = None,
    on_host_exit: Callable[[int, str], None] | None = None,
) -> GuiSession:
    """Start one stack whose mock (if selected), host and (dev) Vite child are all owned."""

    lease = _source.prepare_runtime(plan)
    backend: GuiChild | None = None
    frontend: GuiChild | None = None
    assets: GuiChild | None = None
    active_plan = plan
    started = monotonic()
    stages: dict[str, float] = {}

    def mark(stage: str) -> None:
        stages[stage] = round(monotonic() - started, 2)

    try:
        if plan.build_argv is not None:
            run_build(
                plan,
                json_output=json_output,
                runner=build_runner,
            )
            mark("build")
        if plan.assets_argv is not None:
            assets = _process._spawn_captured(
                plan,
                plan.assets_argv,
                name="assets",
                env=_source.assets_environment(plan),
                cwd=plan.project.home,
                log_path=_source.assets_log_path(lease),
                popen=popen,
            )
            mark("assets_spawned")
        authority: _source.BackendAuthority | None = None
        if plan.driver == "daemon":
            assert plan.admission is not None
        elif plan.driver == "contract-mock":
            argv, cwd, env = _source.mock_invocation(plan)
            backend = _process._spawn_captured(
                plan,
                argv,
                name="contract-mock",
                env=env,
                cwd=cwd,
                log_path=_source.mock_log_path(lease),
                popen=popen,
                mock_capture=True,
            )
            authority = _process._wait_for_mock(plan, backend, sleep=sleep, monotonic=monotonic)
        else:
            raise GuiError("GUI_BACKEND_DRIVER", "the GUI plan has no source driver")
        if authority is not None:
            assert backend is not None
            wait_for_http(
                authority.health_url,
                backend.process,
                label="GUI backend",
                timeout=BACKEND_READY_TIMEOUT_SECONDS,
                urlopen=_process._authorized_urlopen(urlopen, authority.token),
                sleep=sleep,
                monotonic=monotonic,
            )
            mark("backend_health")
        frontend_argv = active_plan.frontend_argv
        if authority is None:
            frontend_argv = (*frontend_argv, *_source.daemon_host_arguments(plan))
        if assets is not None:
            live_origin = _process._wait_for_host_ready(
                assets,
                _source.parse_assets_ready,
                label="GUI development server",
                sleep=sleep,
                monotonic=monotonic,
            )
            mark("assets_ready")
            frontend_argv = (*frontend_argv, "--live-upstream", live_origin)
        grant = _source.generate_launch_grant()
        handoff_path = None if active_plan.open_browser else lease.handoff_path
        host_env = _source.host_environment(
            plan,
            authority,
            grant,
            handoff_path=handoff_path,
            publication_path=lease.publication_path,
        )
        frontend = _process._spawn_captured(
            plan,
            frontend_argv,
            name="frontend",
            env=host_env,
            cwd=plan.project.home,
            log_path=_source.host_log_path(lease),
            popen=popen,
            redactions={grant} if authority is None else {authority.token, grant},
        )
        mark("host_spawned")
        host_ready = _process._wait_for_host_ready(
            frontend,
            lambda line: _source.parse_host_ready(line, active_plan),
            sleep=sleep,
            monotonic=monotonic,
            timeout=(
                BACKEND_READY_TIMEOUT_SECONDS
                if plan.driver == "daemon"
                else FRONTEND_READY_TIMEOUT_SECONDS
            ),
            on_slow=on_backend_wait,
        )
        active_plan = _source.bound_plan(active_plan, host_ready)
        mark("host_ready")
        wait_for_http(
            active_plan.frontend_health_url,
            frontend.process,
            label="GUI frontend",
            timeout=FRONTEND_READY_TIMEOUT_SECONDS,
            urlopen=urlopen,
            sleep=sleep,
            monotonic=monotonic,
        )
        _process._confirm_installed_assets(active_plan, urlopen=urlopen)
        active_plan = replace(
            active_plan,
            backend_endpoint=None if authority is None else authority.endpoint,
            api_docs=active_plan.dev_tools and host_ready.get("apiDocs") is True,
        )
        handoff = None
        browser_url = _source.launch_url(active_plan.url, grant)
        if not active_plan.open_browser:
            handoff = _source.write_handoff(lease, browser_url)
        _source.write_launch_publication(lease)
        session = session_factory(
            plan=active_plan,
            frontend=frontend,
            backend=backend,
            assets=assets,
            handoff_path=handoff,
            runtime_lease=lease,
        )
        session._terminate = terminate_child
        session.on_host_exit = on_host_exit
        if active_plan.open_browser:
            open_browser(browser_url, opener)
        if on_started is not None:
            on_started(session)
        return session
    except BaseException as error:
        mark("failed")
        _process.attach_failure_context(
            error,
            children=(backend, frontend, assets),
            logs={
                "frontend": _source.host_log_path(lease),
                "assets": _source.assets_log_path(lease),
                "contract-mock": _source.mock_log_path(lease),
            },
            stages=stages,
        )
        terminate_child(frontend)
        if assets is not None:
            terminate_child(assets)
        if backend is not None:
            terminate_child(backend)
        _source.remove_handoff(lease)
        _process._cleanup_runtime_files(lease)
        raise
