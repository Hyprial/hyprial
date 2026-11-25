"""Trusted admission for the source GUI's explicitly selected backend.

Decision (DL-1, 2026-11-15): a home whose daemon is not running is started
by the caller's ``ensure_daemon`` hook, never by this module.  The hook runs
inside the daemon branch, after the driver, identity, principal and socket
checks, so no invocation the launcher refuses on cheap validation spawns a
daemon.  The launch primitive's readiness means "socket bound and ping
answered", not "restore finished", so after the hook verification polls a
restoring daemon for ``RESTORE_WAIT_SECONDS`` before the unchanged
``GUI_DAEMON_NOT_READY`` surfaces.

Decision: daemon-first-z6a-launcher-ensure-20261117.  The hook runs in every
state of the home (Z-6a): an ownerless home gets its bootstrap daemon here too,
and the daemon's own ping phase decides the admission whenever a daemon
answers (a bootstrap daemon is admitted as ``bootstrap`` even when the settings
already hold an owner, because a login can commit before initialize).  A bootstrap
daemon is checked for identity (node, socket, version) and a usable catalog but
not for HTTP readiness: its wire catalog reports ``not-ready`` routes by design.
An ownerless home whose daemon does not answer is ``GUI_DAEMON_UNAVAILABLE``.

Decision: daemon-first-z6b-no-hosted-bootstrap-20261117.  The ownerless probe
runs with or without a hook: the child no longer hosts a bootstrap of its own,
so an ownerless home is admitted only by a bootstrap answer, and the refusal
belongs to the launcher rather than to a child that would exit at once.

Decision: w1e2-home-and-external-state-gate-20261121.  Daemon admission inspects
the un-resolved lexical home and any explicit external HARNESS_STATE_DIR
(state_dir != home / "state") before identity reads, daemon launch, or IPC:
symlinks, Windows reparse points (leaf or non-system ancestors), non-directories,
wrong owners, and non-private permissions all fail closed with GUI_HOME_UNSAFE.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from hyprial import __version__
from hyprial.daemon import daemon_catalog, node_owner_or_none
from hyprial.kernel import (
    PrivateAccessUnsupported,
    RequestPortError,
    canonical_user_uri,
    configured_harness_state_dir,
    configured_hyprial_home,
    daemon_socket_path,
    daemon_request_once,
    ipc_errors,
    manysys,
    resolve_node_id,
    secure_private_directory,
)

from ..errors import GuiError
from ...request_http.catalog import CatalogProblem, build_view

SOURCE_DRIVERS = frozenset({"daemon", "contract-mock"})
MOCK_OWNER = "contract-mock"
MOCK_NODE = "local"
MOCK_SCENARIOS = frozenset({"success", "empty", "unconfigured", "error"})
_CONFLICT_KEYS = (
    "HYPRIAL_BACKEND_ENDPOINT",
    "HYPRIAL_GUI_AUTH",
    "HYPRIAL_GUI_SETUP",
    "HYPRIAL_GUI_SETUP_CLI",
    "HYPRIAL_GUI_UPSTREAM_TOKEN",
    "HYPRIAL_GUI_LAUNCH_GRANT",
    "HYPRIAL_GUI_HANDOFF_PATH",
    "HYPRIAL_GUI_PUBLICATION_PATH",
    "HYPRIAL_GUI_REFERENCE_CONFIG",
    "HYPRIAL_GUI_DEV_PREVIEW",
)

RequestOnce = Callable[..., Any]
EnsureDaemon = Callable[[], Any]

DAEMON_NOT_READY = "GUI_DAEMON_NOT_READY"
RESTORE_WAIT_SECONDS = 60.0
RESTORE_POLL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class SourceAdmission:
    """Verified source-backend coordinates, with no credentials.

    ``phase`` names the assembly this admission selects.  A ``serving`` home
    has a committed owner and the canonical user principal; a
    ``bootstrap`` home is provably ownerless, so both fields are None and the
    supervisor must never synthesize either one.
    """

    driver: str
    owner: str | None
    node: str
    principal: str | None
    home: Path | None
    state_dir: Path | None
    socket_path: Path | None
    scenario: str = "success"
    phase: Literal["bootstrap", "serving"] = "serving"

    def __post_init__(self) -> None:
        if self.phase not in {"bootstrap", "serving"}:
            raise ValueError("source admission phase must be bootstrap or serving")
        if self.phase == "bootstrap":
            if self.owner is not None or self.principal is not None:
                raise ValueError(
                    "a bootstrap admission selects an ownerless home: "
                    "owner and principal must both be None"
                )
        elif self.owner is None or self.principal is None:
            raise ValueError(
                "a serving admission requires an owner and a principal"
            )

    @property
    def runtime_root(self) -> Path:
        if self.state_dir is not None:
            return self.state_dir / "gui"
        suffix = (
            manysys.current_uid() if manysys.posix_metadata_enforced() else "user"
        )
        return Path(os.path.abspath(tempfile.gettempdir())) / f"hyprial-gui-{suffix}"


def admit_source(
    environ: Mapping[str, str] | None = None,
    *,
    request_once: RequestOnce = daemon_request_once,
    timeout: float = 3.0,
    ensure_daemon: EnsureDaemon | None = None,
    restore_wait: float = RESTORE_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SourceAdmission:
    """Validate the one explicit driver.

    Without ``ensure_daemon`` nothing is spawned or written.  With it, the
    hook runs only for a validated ``daemon`` driver on a home that has an
    owner, after every cheap check and before the daemon is verified, and the
    verification then waits (bounded by ``restore_wait``) for a daemon that
    is still restoring.
    """

    env = dict(os.environ if environ is None else environ)
    driver = env.get("HYPRIAL_GUI_BACKEND_DRIVER", "").strip()
    if driver not in SOURCE_DRIVERS:
        raise GuiError(
            "GUI_BACKEND_DRIVER",
            "HYPRIAL_GUI_BACKEND_DRIVER must be exactly `daemon` or `contract-mock`",
            {"accepted": sorted(SOURCE_DRIVERS)},
        )
    if "HYPRIAL_OWNER" in env:
        raise GuiError(
            "GUI_OWNER_OVERRIDE",
            "HYPRIAL_OWNER is forbidden for the GUI; commit the owner in the isolated settings instead",
        )
    conflicts = [name for name in _CONFLICT_KEYS if name in env]
    if conflicts:
        raise GuiError(
            "GUI_CONFIG_CONFLICT",
            "the source GUI owns backend endpoints, credentials and identity; "
            "remove the conflicting inherited configuration",
            {"variables": sorted(conflicts)},
        )
    if driver == "contract-mock":
        return _admit_mock(env)
    return _admit_daemon(
        env,
        request_once=request_once,
        timeout=timeout,
        ensure_daemon=ensure_daemon,
        restore_wait=restore_wait,
        sleep=sleep,
        monotonic=monotonic,
    )


def _admit_mock(env: Mapping[str, str]) -> SourceAdmission:
    scenario = env.get("HYPRIAL_GUI_CONTRACT_MOCK_SCENARIO", "success").strip()
    if scenario not in MOCK_SCENARIOS:
        raise GuiError(
            "GUI_MOCK_SCENARIO",
            "HYPRIAL_GUI_CONTRACT_MOCK_SCENARIO is not a declared Contract Mock scenario",
            {"accepted": sorted(MOCK_SCENARIOS)},
        )
    principal = canonical_user_uri(MOCK_OWNER)
    explicit = env.get("HYPRIAL_GUI_PRINCIPAL")
    if explicit is not None and explicit != principal:
        raise GuiError(
            "GUI_PRINCIPAL_MISMATCH",
            "HYPRIAL_GUI_PRINCIPAL does not match the synthetic Contract Mock identity",
        )
    configured_state = env.get("HARNESS_STATE_DIR", "").strip()
    state_dir = (
        Path(os.path.abspath(Path(configured_state).expanduser())) if configured_state else None
    )
    return SourceAdmission(
        driver="contract-mock",
        owner=MOCK_OWNER,
        node=MOCK_NODE,
        principal=principal,
        home=None,
        state_dir=state_dir,
        socket_path=None,
        scenario=scenario,
    )


def _is_allowed_system_symlink(step: Path, target: Path) -> bool:
    return (
        step != target
        and step.anchor == "/"
        and step.parts in {("/", "tmp"), ("/", "var"), ("/", "etc")}
    )


def _validate_existing_ancestry(target: Path) -> None:
    lexical = Path(os.path.abspath(os.fspath(target)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if manysys.path_is_reparse(info):
            raise PrivateAccessUnsupported("unsafe-ancestry")
        if stat.S_ISLNK(info.st_mode):
            if not _is_allowed_system_symlink(current, lexical) or not current.is_dir():
                raise PrivateAccessUnsupported("unsafe-ancestry")
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise PrivateAccessUnsupported("unsafe-ancestry")


def _unsafe_home_error(home: Path) -> GuiError:
    return GuiError(
        "GUI_HOME_UNSAFE",
        "HYPRIAL home must be owned by the current user and private "
        "(mode 0700 on POSIX). Verify the selected directory and correct "
        "its permissions before starting the GUI; no permissions were changed.",
        {"home": str(home)},
    )


def _unsafe_state_error(home: Path, state_dir: Path) -> GuiError:
    return GuiError(
        "GUI_HOME_UNSAFE",
        "HARNESS_STATE_DIR must be owned by the current user and private "
        "(mode 0700 on POSIX). Verify the selected directory and correct "
        "its permissions before starting the GUI; no permissions were changed.",
        {"home": str(home), "state_dir": str(state_dir)},
    )


def _validate_home_directory(home: Path) -> None:
    try:
        _validate_existing_ancestry(home)
    except (OSError, PrivateAccessUnsupported):
        raise _unsafe_home_error(home) from None
    try:
        observed = home.lstat()
    except FileNotFoundError:
        raise GuiError(
            "GUI_HOME_NOT_INITIALIZED",
            f"HYPRIAL home does not exist or is not a directory: {home}",
            {"home": str(home)},
        ) from None
    except OSError:
        raise _unsafe_home_error(home) from None
    if stat.S_ISLNK(observed.st_mode) or manysys.path_is_reparse(observed):
        raise _unsafe_home_error(home)
    if not stat.S_ISDIR(observed.st_mode):
        raise GuiError(
            "GUI_HOME_NOT_INITIALIZED",
            f"HYPRIAL home does not exist or is not a directory: {home}",
            {"home": str(home)},
        )
    try:
        secure_private_directory(home, mode=0o700)
    except (OSError, PrivateAccessUnsupported):
        raise _unsafe_home_error(home) from None


def _validate_external_state_directory(home: Path, state_dir: Path) -> None:
    try:
        _validate_existing_ancestry(state_dir)
        try:
            observed = state_dir.lstat()
        except FileNotFoundError:
            observed = None
        if observed is not None:
            if (
                stat.S_ISLNK(observed.st_mode)
                or manysys.path_is_reparse(observed)
                or not stat.S_ISDIR(observed.st_mode)
            ):
                raise PrivateAccessUnsupported("unsafe-directory")
            secure_private_directory(state_dir, create=False, mode=0o700)
    except (OSError, PrivateAccessUnsupported):
        raise _unsafe_state_error(home, state_dir) from None


def _admit_daemon(
    env: Mapping[str, str],
    *,
    request_once: RequestOnce,
    timeout: float,
    ensure_daemon: EnsureDaemon | None = None,
    restore_wait: float = RESTORE_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SourceAdmission:
    home, _source = configured_hyprial_home(env)
    _validate_home_directory(home)
    state_dir = configured_harness_state_dir(env, home_selector=lambda: home)
    if state_dir != home / "state":
        _validate_external_state_directory(home, state_dir)
    try:
        owner = node_owner_or_none(env, hyprial_home=home)
    except ValueError as error:
        raise GuiError("GUI_IDENTITY_INVALID", str(error)) from error
    try:
        node = resolve_node_id(env)
    except ValueError as error:
        raise GuiError("GUI_IDENTITY_INVALID", str(error)) from error
    explicit = env.get("HYPRIAL_GUI_PRINCIPAL")
    principal: str | None = None
    if owner is None:
        if explicit is not None:
            raise GuiError(
                "GUI_PRINCIPAL_MISMATCH",
                "an ownerless bootstrap home has no principal to match",
                {"provided": explicit},
            )
    else:
        principal = _principal_for(owner, explicit)

    socket_path = daemon_socket_path(lambda: state_dir, env=env)
    selected_socket = state_dir / "daemon.sock"
    if socket_path != selected_socket:
        raise GuiError(
            "GUI_SOCKET_MISMATCH",
            "HARNESS_SOCKET_PATH cannot replace the daemon socket selected by HYPRIAL_HOME/HARNESS_STATE_DIR",
            {"expected": str(selected_socket)},
        )
    if "HARNESS_SOCKET_PATH" in env:
        configured = env.get("HARNESS_SOCKET_PATH", "")
        if not configured.strip() or not _same_path(Path(configured), socket_path):
            raise GuiError(
                "GUI_SOCKET_MISMATCH",
                "HARNESS_SOCKET_PATH differs from the selected home/state daemon socket",
                {"expected": str(socket_path)},
            )

    phase: Literal["bootstrap", "serving"] = "serving"
    if ensure_daemon is not None:
        ensure_daemon()
    if owner is None:
        ping = _probe_ping(request_once, socket_path=socket_path, timeout=timeout)
        if ping is None:
            raise GuiError(
                "GUI_DAEMON_UNAVAILABLE",
                "the selected daemon is not reachable and ready",
            )
        if ping.get("phase") == "bootstrap":
            phase = "bootstrap"
            verify_bootstrap_daemon(
                node=node,
                socket_path=socket_path,
                request_once=request_once,
                timeout=timeout,
            )
        else:
            owner = _reread_owner(env, home)
            principal = _principal_for(owner, explicit)
    if phase == "serving":
        try:
            _verify_serving(
                owner=owner,
                node=node,
                socket_path=socket_path,
                request_once=request_once,
                timeout=timeout,
                ensure_daemon=ensure_daemon,
                restore_wait=restore_wait,
                sleep=sleep,
                monotonic=monotonic,
            )
        except GuiError as error:
            if not _owner_mismatch(error):
                raise
            phase = "bootstrap"
            verify_bootstrap_daemon(
                node=node,
                socket_path=socket_path,
                request_once=request_once,
                timeout=timeout,
            )
    if phase == "bootstrap":
        owner = None
        principal = None
    return SourceAdmission(
        driver="daemon",
        owner=owner,
        node=node,
        principal=principal,
        home=home,
        state_dir=state_dir,
        socket_path=socket_path,
        phase=phase,
    )


def _principal_for(owner: str | None, explicit: str | None) -> str | None:
    if owner is None:
        return None
    principal = canonical_user_uri(owner)
    if explicit is not None and explicit != principal:
        raise GuiError(
            "GUI_PRINCIPAL_MISMATCH",
            "HYPRIAL_GUI_PRINCIPAL must equal the canonical user identity for the verified owner",
            {"expected": principal},
        )
    return principal


def _reread_owner(env: Mapping[str, str], home: Path) -> str:
    try:
        owner = node_owner_or_none(env, hyprial_home=home)
    except ValueError as error:
        raise GuiError("GUI_IDENTITY_INVALID", str(error)) from error
    if owner is None:
        raise GuiError(
            "GUI_DAEMON_IDENTITY",
            "the selected daemon has an account but the home settings have no owner",
            {"mismatches": ["owner"]},
        )
    return owner


def _owner_mismatch(error: GuiError) -> bool:
    mismatches = error.data.get("mismatches")
    return error.code == "GUI_DAEMON_IDENTITY" and isinstance(mismatches, list) and "owner" in mismatches


def _verify_serving(
    *,
    owner: str,
    node: str,
    socket_path: Path,
    request_once: RequestOnce,
    timeout: float,
    ensure_daemon: EnsureDaemon | None,
    restore_wait: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    if ensure_daemon is None:
        verify_ready_daemon(
            owner=owner,
            node=node,
            socket_path=socket_path,
            request_once=request_once,
            timeout=timeout,
        )
        return
    verify_ready_daemon_waiting(
        owner=owner,
        node=node,
        socket_path=socket_path,
        request_once=request_once,
        timeout=timeout,
        restore_wait=restore_wait,
        sleep=sleep,
        monotonic=monotonic,
    )


def _probe_ping(
    request_once: RequestOnce, *, socket_path: Path, timeout: float
) -> dict[str, Any] | None:
    try:
        ping = request_once("ping", {}, socket_path=socket_path, timeout=timeout)
    except (
        RequestPortError,
        ipc_errors.DaemonRequestError,
        ipc_errors.DaemonUnavailableError,
        ipc_errors.DaemonRestoringError,
        ipc_errors.DaemonDisconnectedError,
        ipc_errors.IpcTimeoutError,
        OSError,
        TimeoutError,
    ):
        return None
    return ping if isinstance(ping, dict) else None


@dataclass(frozen=True, slots=True)
class DaemonReadiness:
    owner: str | None
    node: str
    epoch: str
    version: str


def verify_ready_daemon_waiting(
    *,
    owner: str,
    node: str,
    socket_path: Path,
    request_once: RequestOnce = daemon_request_once,
    timeout: float = 3.0,
    restore_wait: float = RESTORE_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DaemonReadiness:
    """Verify readiness, waiting a bounded time for a restoring daemon.

    Only ``GUI_DAEMON_NOT_READY`` is retried; every identity, catalog or
    transport refusal surfaces at once.  When the budget runs out the last
    not-ready error is raised unchanged.
    """

    deadline = monotonic() + max(restore_wait, 0.0)
    while True:
        try:
            return verify_ready_daemon(
                owner=owner,
                node=node,
                socket_path=socket_path,
                request_once=request_once,
                timeout=timeout,
            )
        except GuiError as error:
            remaining = deadline - monotonic()
            if error.code != DAEMON_NOT_READY or remaining <= 0:
                raise
            sleep(min(RESTORE_POLL_SECONDS, remaining))


def verify_ready_daemon(
    *,
    owner: str,
    node: str,
    socket_path: Path,
    request_once: RequestOnce = daemon_request_once,
    timeout: float = 3.0,
) -> DaemonReadiness:
    """Verify one complete serving snapshot for a verified owner and node."""

    ping, catalog = _snapshot(request_once, socket_path=socket_path, timeout=timeout)
    _verify_identity(
        ping,
        owner=owner,
        node=node,
        socket_path=socket_path,
        expected_version=__version__,
    )
    if ping.get("phase") not in {"serving", "reconciled"} or ping.get("restorePending") is True:
        raise GuiError(
            "GUI_DAEMON_NOT_READY",
            "the selected daemon is restoring and cannot serve the GUI yet",
            {"phase": ping.get("phase")},
        )
    try:
        view = build_view(
            ping,
            catalog,
            socket_path=str(socket_path),
            local_entries=daemon_catalog()["methods"],
        )
    except CatalogProblem as error:
        raise GuiError(
            "GUI_DAEMON_CATALOG",
            f"the selected daemon catalog is not usable: {error.message}",
            {"reason": error.reason},
        ) from error
    if view.backend.owner != owner or view.backend.node_id != node:
        raise GuiError(
            "GUI_DAEMON_IDENTITY",
            "the selected daemon identity differs from the verified settings identity",
        )
    version = catalog.get("packageVersion")
    if version != __version__ or view.backend.version != __version__:
        raise GuiError(
            "GUI_DAEMON_CATALOG",
            "the selected daemon package version differs from this CLI",
            {"expected": __version__, "observed": version},
        )
    if catalog.get("epoch") != ping.get("epoch"):
        raise GuiError(
            "GUI_DAEMON_CATALOG",
            "daemon ping and catalog do not share one epoch",
        )
    not_ready = sorted(
        name for name, route in view.routes.items() if route.state != "available"
    )
    if not view.routes or not_ready:
        raise GuiError(
            "GUI_DAEMON_NOT_READY",
            "the selected daemon has no complete HTTP-ready method catalog",
            {"notReady": not_ready},
        )
    epoch = ping.get("epoch")
    if not isinstance(epoch, str) or not epoch:
        raise GuiError(
            "GUI_DAEMON_CATALOG",
            "the selected daemon readiness snapshot has no epoch",
        )
    return DaemonReadiness(owner=owner, node=node, epoch=epoch, version=__version__)


def verify_bootstrap_daemon(
    *,
    node: str,
    socket_path: Path,
    request_once: RequestOnce = daemon_request_once,
    timeout: float = 3.0,
) -> DaemonReadiness:
    """Verify one complete bootstrap snapshot: ownerless, this node, this socket.

    The wire catalog reports ``not-ready`` routes for methods that need an
    account, so no route readiness is required here.
    """

    ping, catalog = _snapshot(request_once, socket_path=socket_path, timeout=timeout)
    _verify_identity(
        ping,
        owner=None,
        node=node,
        socket_path=socket_path,
        expected_version=__version__,
    )
    if ping.get("phase") != "bootstrap":
        raise GuiError(
            "GUI_DAEMON_IDENTITY",
            "the selected daemon is not in the bootstrap phase",
            {"mismatches": ["phase"]},
        )
    try:
        view = build_view(
            ping,
            catalog,
            socket_path=str(socket_path),
            local_entries=daemon_catalog()["methods"],
        )
    except CatalogProblem as error:
        raise GuiError(
            "GUI_DAEMON_CATALOG",
            f"the selected daemon catalog is not usable: {error.message}",
            {"reason": error.reason},
        ) from error
    if view.backend.node_id != node or view.backend.owner is not None:
        raise GuiError(
            "GUI_DAEMON_IDENTITY",
            "the selected daemon identity differs from the verified settings identity",
        )
    if catalog.get("packageVersion") != __version__ or view.backend.version != __version__:
        raise GuiError(
            "GUI_DAEMON_CATALOG",
            "the selected daemon package version differs from this CLI",
            {"expected": __version__, "observed": catalog.get("packageVersion")},
        )
    epoch = ping.get("epoch")
    if not isinstance(epoch, str) or not epoch:
        raise GuiError(
            "GUI_DAEMON_CATALOG",
            "the selected daemon readiness snapshot has no epoch",
        )
    return DaemonReadiness(owner=None, node=node, epoch=epoch, version=__version__)


def _snapshot(
    request_once: RequestOnce,
    *,
    socket_path: Path,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    last_epoch: tuple[Any, Any] | None = None
    for _attempt in range(3):
        try:
            ping = request_once(
                "ping", {}, socket_path=socket_path, timeout=timeout
            )
            catalog = request_once(
                "daemon.catalog", {}, socket_path=socket_path, timeout=timeout
            )
        except (
            RequestPortError,
            ipc_errors.DaemonRequestError,
            ipc_errors.DaemonUnavailableError,
            ipc_errors.DaemonRestoringError,
            ipc_errors.DaemonDisconnectedError,
            ipc_errors.IpcTimeoutError,
            OSError,
            TimeoutError,
        ) as error:
            raise GuiError(
                "GUI_DAEMON_UNAVAILABLE",
                "the selected daemon is not reachable and ready",
            ) from error
        if not isinstance(ping, dict) or not isinstance(catalog, dict):
            raise GuiError(
                "GUI_DAEMON_CATALOG",
                "the selected daemon returned a non-object readiness snapshot",
            )
        last_epoch = (ping.get("epoch"), catalog.get("epoch"))
        if last_epoch[0] == last_epoch[1]:
            return ping, catalog
    raise GuiError(
        "GUI_DAEMON_CATALOG",
        "daemon ping and catalog did not converge on one epoch",
        {"epochs": [last_epoch[0] if last_epoch else None, last_epoch[1] if last_epoch else None]},
    )


def _verify_identity(
    ping: Mapping[str, Any],
    *,
    owner: str | None,
    node: str,
    socket_path: Path,
    expected_version: str,
) -> None:
    mismatches: list[str] = []
    if ping.get("owner") != owner:
        mismatches.append("owner")
    if ping.get("nodeId") != node:
        mismatches.append("node")
    observed_socket = ping.get("socket")
    if not isinstance(observed_socket, str) or not _same_path(
        Path(observed_socket), socket_path
    ):
        mismatches.append("socket")
    if ping.get("version") != expected_version:
        mismatches.append("version")
    if ping.get("running") is not True:
        mismatches.append("running")
    if mismatches:
        raise GuiError(
            "GUI_DAEMON_IDENTITY",
            "the selected daemon does not match the configured home, owner, node and socket",
            {"mismatches": mismatches},
        )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.expanduser().resolve())) == os.path.normcase(
        str(right.expanduser().resolve())
    )


__all__ = [
    "DaemonReadiness",
    "MOCK_NODE",
    "MOCK_OWNER",
    "MOCK_SCENARIOS",
    "SOURCE_DRIVERS",
    "SourceAdmission",
    "admit_source",
    "verify_bootstrap_daemon",
    "verify_ready_daemon",
    "verify_ready_daemon_waiting",
]
