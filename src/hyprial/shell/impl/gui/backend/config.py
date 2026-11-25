"""Launch configuration and plan construction for the shared GUI.

Decision record (GUI cold-start budget, 2026-11-15): the request-http child
spends its start almost entirely on CPU, not on the daemon: interpreter and
module imports (about 55 percent), the local daemon catalog build and schema
validation (about 33 percent), and one ping plus one catalog request that
takes about 0.1 s.  It does not wait on a restoring daemon and publishes the
same capability whether the daemon was just started or has been up for hours.
Measured from spawn to ``server.json`` on a 4-core machine: 3.9-5.3 s
unconstrained, 13.8 s with one core shared against two competing sessions,
24.4 s against four and 37.1 s against six (about 8 times slower than idle).
The former 15 s budget therefore only covered an idle machine and refused a
legitimate start on a busy CI container or a small VM.  The rule is the worst
time observed under the harshest constraint reproduced, times a margin of
1.6, rounded to the 60 s that ``RESTORE_WAIT_SECONDS`` already uses for the
daemon: long enough for a real slow start, bounded so a child that never
publishes still ends the command.  A child that exits is noticed at once and
is not subject to the budget.  ``BACKEND_SLOW_NOTICE_SECONDS`` is when the
caller is told the wait is longer than usual, so a long start is not mistaken
for a hang.

Decision record (host serves the daemon driver, 2026-11-19, 2822 B3): the work
the paragraph above measured (module imports, daemon attach, catalog build and
schema validation) now runs inside the session host, so the host's readiness
line is the signal that used to be the request-http child's ``server.json``.
For the daemon driver the host ready wait therefore uses the same
``BACKEND_READY_TIMEOUT_SECONDS`` (60 s); the contract-mock driver, whose host
only relays, keeps ``FRONTEND_READY_TIMEOUT_SECONDS`` (30 s).  Measured from
spawn to the ready line: 3.1-3.3 s warm and 6.5-7.3 s cold on an idle machine;
cold with one core shared against 2, 4 and 6 competing sessions, 20.9 s, 35.3 s
and 50.5 s, the last two beyond 30 s.  The total budget does not grow: before
it was 60 s for the child plus 30 s for the host.  The slow notice, after
``BACKEND_SLOW_NOTICE_SECONDS``, is now announced from the host wait for both
drivers, once.  A host that ends on its own with a non-zero status after its
ready line ends ``GuiSession.wait`` with that status (the published "exits with
the frontend's exit status"); no error code or stdout line is added.  Before the
runtime files are removed ``GuiSession.wait`` hands the status and the redacted
log tail to the optional ``on_host_exit`` callback, which the CLI writes once to
stderr (``gui-host-exited`` under ``--json``).

Decision record (one launch path, 2026-11-16): every GUI the launcher starts is
served by the Python session host, installed or source.  A source checkout runs
``npm run build`` and the host serves ``<gui>/dist``; ``hyprial gui dev`` adds
one more owned child, the Vite dev server on an ephemeral loopback port that
holds no backend variable, and the host relays assets and HMR to it with
``--live-upstream``.  The browser only ever sees the host's origin.  Because the
Vite port is known only after Vite is ready, the plan carries the host argv
without ``--live-upstream`` and the session appends it.  The Node host and the
``start`` script are gone, so a source ``package.json`` needs ``build`` for the
normal GUI and ``dev`` for ``hyprial gui dev``.  An unset
``HYPRIAL_GUI_BACKEND_DRIVER`` means ``daemon`` for the source checkout as it
already did for the wheel (owner decision OD-2, the plan default); the launcher
names the driver, the browser never does, and ``contract-mock`` stays an explicit
choice.

Decision record (``--port 0``, 2026-11-16): port 0 asks the session host to bind
an ephemeral loopback port.  The launcher cannot know the origin before the
host's ready line, so the plan keeps ``port == 0`` and a placeholder ``url``
until the session rebinds it from the announced port (``source.bound_plan``).
Port 0 is never probed with ``assert_port_free``: the probe would only reserve a
number the host then binds itself.  In dev mode only the host's port is public;
the Vite child already uses its own ephemeral port.
Origin allowlists while the port is unbound: the daemon driver's request-http
config gets ``allowedOrigins: []`` (only the host's origin is ever used by the
browser).  The contract-mock driver is passed no ``--allow-origin`` at all, and
``gui/contract-mock/serve.ts`` then falls back to its ``DEFAULT_ORIGINS``
(localhost and 127.0.0.1 on ports 5173 and 4173); that is the mock's default, not
an empty allowlist.  It is harmless (the bearer is still required and the data is
synthetic).  Follow-up, not in this change: let ``serve.ts`` express an explicit
empty allowlist.

Decision record (``backend_owned``, 2026-11-20, owner-approved): the plan owns a
backend child only when it starts one.  The daemon driver starts none (the host
serves the request-http dispatch itself and the daemon outlives the GUI), so
``backend_owned`` is false for it; the contract-mock driver launches its own
loopback child and keeps true.  Every reader also tests ``backend is not None``,
so stop never signals the daemon either way.

Decision record (``--with-dev-tools``, PR 4b-C, 2026-11-17): the developer tool
layer is switched by one launch argument.  ``dev_tools`` on the plan is true for
``--with-dev-tools`` and for ``dev`` (which implies it), and the host argv ends
with ``--dev-tools`` exactly then; no environment variable is read for it, and a
release wheel accepts the flag like a checkout (owner decision Q-3).  ``api_docs``
is not decided here: the session copies it from the host's ready line, because
only the host knows whether it found the docs files and which driver it serves.

Decision record (``--live-source``, PR 5, 2026-11-17): live source is the second
switch, independent of the tools.  ``--live-source`` alone starts the Vite child
without the tool layer, ``dev`` is an alias for both flags and stays idempotent
with either, and ``mode`` is ``dev`` exactly when both are on (it is
informational).  The plan does not store the switch: ``live_source`` is the
presence of the Vite child argv, so a plan cannot say one thing and start
another, and ``hot_reload`` equals it.  Target selection, the required npm
script and the host argv key on live source, not on ``mode``.  With live source
there is no asset tree, so with the tools the host is given the Swagger UI
directory by ``--docs-assets``: ``<gui>/node_modules/swagger-ui-dist`` of the
project, or ``dev-tools/docs`` of the installed tree for a wheel's managed
worktree.  The argument is passed only when both files exist; the host refuses
an incomplete explicit directory, and a stale ``node_modules`` must not stop a
development launch (the session then reports ``api_docs`` false).
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.kernel import ipc_errors

from ..errors import GuiError
from ..install import GuiAssets
from ..install.launch import select_live_source_target, select_normal_target
from ..install.resources import installed_asset_root
from ..project import GuiProject, _npm_prefix, inspect_project
from ..worktree import AutoGuiWorktree, discover_source_repo as _discover_source_repo
from .admission import SourceAdmission, admit_source
from .handoff import (
    assert_port_free as _assert_port_free,
    child_environment,
    frontend_url,
    join_url,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4173
BACKEND_HOST = "127.0.0.1"
DEFAULT_DRIVER = "daemon"
BUILD_SCRIPT = "build"
HOST_MODULE = "hyprial.shell.impl.gui.host"
FRONTEND_HEALTH_PATH = "/__gui/healthz"
FRONTEND_READY_TIMEOUT_SECONDS = 30.0
BACKEND_READY_TIMEOUT_SECONDS = 60.0
BACKEND_SLOW_NOTICE_SECONDS = 5.0
HTTP_POLL_INTERVAL_SECONDS = 0.2
READY_POLL_INTERVAL_SECONDS = 0.1
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
PROCESS_KILL_TIMEOUT_SECONDS = 2.0


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
    frontend_argv: tuple[str, ...]
    env: Mapping[str, str]
    open_browser: bool
    build_argv: tuple[str, ...] | None = None
    auto_worktree: AutoGuiWorktree | None = None
    installed_assets: GuiAssets | None = None
    dev_prompt: str = ""
    driver: str | None = None
    admission: SourceAdmission | None = None
    runtime_dir: Path | None = None
    build_env: Mapping[str, str] | None = None
    assets_argv: tuple[str, ...] | None = None
    dev_tools: bool = False
    api_docs: bool = False

    @property
    def frontend_health_url(self) -> str:
        return join_url(self.url, FRONTEND_HEALTH_PATH)

    @property
    def live_source(self) -> bool:
        """Whether the Vite dev server serves the page; it is the plan's Vite child."""
        return self.assets_argv is not None

    @property
    def hot_reload(self) -> bool:
        return self.live_source


def parse_action(value: str | None) -> str:
    """Map the public leaf grammar to a launch mode."""
    normalized = (value or "").strip().lower()
    if normalized == "":
        return "normal"
    if normalized == "dev":
        return "dev"
    raise GuiError(
        ipc_errors.INVALID_ARGUMENT,
        "Use `hyprial gui` for the built GUI, with --with-dev-tools and "
        "--live-source as needed; `hyprial gui dev` is both flags. "
        "start/status/stop/upgrade are retired.",
    )


DOCS_FILES = ("swagger-ui-bundle.js", "swagger-ui.css")


def docs_assets(gui_home: Path, *, managed: bool, installed_root: Path) -> Path | None:
    """The Swagger UI directory for a live-source host, or ``None`` unless both files exist."""
    directory = (
        installed_root / "dev-tools" / "docs" if managed else gui_home / "node_modules" / "swagger-ui-dist"
    )
    return directory if all((directory / name).is_file() for name in DOCS_FILES) else None


LOOPBACK_IPS = frozenset({"127.0.0.1", "::1"})


def host_argv(
    host: str, port: int, *, root: Path | None = None, dev_tools: bool = False, docs: Path | None = None
) -> tuple[str, ...]:
    """The Python session host command; ``--root`` for built assets, none for a live dev server.

    ``--dev-tools`` follows the address and is present only when the plan asked for the tools;
    ``--docs-assets`` only with it and with a Swagger UI directory.
    """
    root_args = () if root is None else ("--root", str(root))
    docs_args = () if docs is None or not dev_tools else ("--docs-assets", str(docs))
    return (
        sys.executable,
        "-P",
        "-m",
        HOST_MODULE,
        "serve",
        *root_args,
        "--host",
        host,
        "--port",
        str(port),
        *(("--dev-tools",) if dev_tools else ()),
        *docs_args,
    )


def parse_host(value: str) -> str:
    """Validate ``--ip``: only the loopback addresses the session host binds."""
    normalized = (value or DEFAULT_HOST).strip()
    if normalized.lower() == "localhost":
        return DEFAULT_HOST
    try:
        address = str(ipaddress.ip_address(normalized))
    except ValueError as error:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            f"--ip must be an IP address or localhost, not {value!r}",
        ) from error
    if address not in LOOPBACK_IPS:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            f"--ip must be a loopback address (127.0.0.1, ::1 or localhost), not {value!r}",
        )
    return address


def parse_port(value: int) -> int:
    """Validate ``--port``; 0 means the host picks a free loopback port."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 65535:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "--port must be an integer from 0 to 65535 (0 picks a free port)",
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
    assert_port_free: Callable[[str, int], None] = _assert_port_free,
    discover_source: Callable[..., Path] = _discover_source_repo,
    admit: Callable[..., SourceAdmission] | None = None,
    ensure_daemon: Callable[[], Any] | None = None,
    with_dev_tools: bool = False,
    live_source: bool = False,
) -> GuiLaunchPlan:
    """Build a launch plan and fail before starting any child on bad input."""
    alias = parse_action(action) == "dev"
    dev_tools = with_dev_tools is True or alias
    live = live_source is True or alias
    mode = "dev" if dev_tools and live else "normal"
    host = parse_host(ip)
    frontend_port = parse_port(port)
    source = dict(os.environ if environ is None else environ)
    _reject_removed_configuration(source)
    admission: SourceAdmission | None = None

    def admit_with_ensure(environment: Mapping[str, str]) -> SourceAdmission:
        options = {} if ensure_daemon is None else {"ensure_daemon": ensure_daemon}
        named = {"HYPRIAL_GUI_BACKEND_DRIVER": DEFAULT_DRIVER, **environment}
        return (admit or admit_source)(environ=named, **options)

    def preflight() -> None:
        nonlocal admission
        admission = admit_with_ensure(source)

    installed_admission: SourceAdmission | None = None

    def installed_preflight() -> SourceAdmission:
        if source.get("HYPRIAL_GUI_BACKEND_DRIVER", "").strip() == "contract-mock":
            raise GuiError(
                "GUI_MOCK_UNAVAILABLE",
                "the installed GUI serves the real daemon only; the Contract Mock "
                "needs a source checkout and Node",
            )
        return admit_with_ensure(source)

    target = (
        select_normal_target(
            gui_home,
            environ=source,
            cwd=cwd,
            inspect=inspect,
            which=which,
            run=run,
            preflight=preflight,
        )
        if not live
        else select_live_source_target(
            gui_home,
            environ=source,
            cwd=cwd,
            inspect=inspect,
            which=which,
            run=run,
            discover=discover_source,
            preflight=preflight,
        )
    )
    project = target.project
    auto = target.auto

    if target.static_root is not None:
        installed_admission = installed_preflight()
    if frontend_port != 0:
        assert_port_free(host, frontend_port)
    url = frontend_url(host, frontend_port)
    if target.static_root is not None:
        frontend_argv = host_argv(host, frontend_port, root=target.static_root, dev_tools=dev_tools)
        return GuiLaunchPlan(
            mode="normal",
            project=project,
            host=host,
            port=frontend_port,
            url=url,
            backend_endpoint=None,
            backend_owned=installed_admission.driver != "daemon",
            frontend_argv=frontend_argv,
            env=child_environment(
                source,
                gui_home=target.static_root,
            ),
            open_browser=not no_open,
            installed_assets=target.assets,
            driver=installed_admission.driver,
            admission=installed_admission,
            runtime_dir=installed_admission.runtime_root / "daemon",
            build_env=source,
            dev_tools=dev_tools,
        )

    if admission is None:
        admission = admit_with_ensure(source)
    required_script = "dev" if live else BUILD_SCRIPT
    if required_script not in project.scripts:
        raise GuiError(
            "GUI_SCRIPT_MISSING",
            f"{project.home / 'package.json'} has no `{required_script}` script",
            {"gui_home": str(project.home), "script": required_script},
        )

    child_env = child_environment(
        source,
        gui_home=project.home,
    )
    if not live:
        frontend_argv = host_argv(host, frontend_port, root=project.home / "dist", dev_tools=dev_tools)
        assets_argv = None
        build_argv = (*_npm_prefix(project.npm), "run", BUILD_SCRIPT)
    else:
        docs = docs_assets(
            project.home,
            managed=auto is not None and auto.managed,
            installed_root=installed_asset_root(),
        )
        frontend_argv = host_argv(host, frontend_port, dev_tools=dev_tools, docs=docs)
        assets_argv = (
            *_npm_prefix(project.npm),
            "run",
            "dev",
            "--",
            "--host",
            BACKEND_HOST,
            "--port",
            "0",
            "--strict-port",
        )
        build_argv = None
    runtime_name = "daemon" if admission.driver == "daemon" else "contract-mock"
    return GuiLaunchPlan(
        mode=mode,
        project=project,
        host=host,
        port=frontend_port,
        url=url,
        backend_endpoint=None,
        backend_owned=admission.driver != "daemon",
        frontend_argv=frontend_argv,
        env=child_env,
        open_browser=not no_open,
        build_argv=build_argv,
        auto_worktree=auto,
        installed_assets=target.assets,
        dev_prompt=target.dev_prompt,
        driver=admission.driver,
        admission=admission,
        runtime_dir=admission.runtime_root / runtime_name,
        build_env=source,
        assets_argv=assets_argv,
        dev_tools=dev_tools,
    )


def _reject_removed_configuration(environment: Mapping[str, str]) -> None:
    removed = [
        name
        for name in (
            "HYPRIAL_BACKEND_ENDPOINT",
            "HYPRIAL_GUI_AUTH",
            "HYPRIAL_GUI_SETUP",
            "HYPRIAL_GUI_SETUP_CLI",
            "HYPRIAL_GUI_REFERENCE_CONFIG",
            "HYPRIAL_GUI_DEV_PREVIEW",
        )
        if name in environment
    ]
    if removed:
        raise GuiError(
            "GUI_CONFIG_CONFLICT",
            "the source GUI owns its backend endpoint and no longer supports legacy auth/setup bridges",
            {"variables": removed},
        )
__all__ = [
    "BACKEND_HOST",
    "BACKEND_READY_TIMEOUT_SECONDS",
    "BACKEND_SLOW_NOTICE_SECONDS",
    "BUILD_SCRIPT",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "FRONTEND_HEALTH_PATH",
    "FRONTEND_READY_TIMEOUT_SECONDS",
    "GuiLaunchPlan",
    "HOST_MODULE",
    "HTTP_POLL_INTERVAL_SECONDS",
    "PROCESS_KILL_TIMEOUT_SECONDS",
    "PROCESS_STOP_TIMEOUT_SECONDS",
    "READY_POLL_INTERVAL_SECONDS",
    "build_plan",
    "docs_assets",
    "host_argv",
    "parse_action",
    "parse_host",
    "parse_port",
]
