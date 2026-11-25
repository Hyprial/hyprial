"""Private runtime, child configuration and handoff for source GUI sessions.

Decision record (M1 source entry, 2026-11-05)
- Backend admission completes before this module creates the runtime directory.
  A per-runtime lock (supervisor.lock) refuses a concurrent supervisor with
  GUI_RUNTIME_BUSY and is the only lock the runtime holds.
- Decision gui-runtime-portable-20261121 (W1C, owner 2026-11-21): the supervisor
  lock is the kernel's ProcessFileLock (flock on POSIX, LockFileEx on Windows),
  taken with one non-blocking attempt and released by the OS when the process
  exits, so no platform is refused for lacking fcntl or os.getuid.  The runtime
  directory is checked by where it genuinely lives.  Only when admission names a
  verified home directory and both state_dir and runtime_dir stay inside that
  home boundary (with no symlink or reparse point escaping along the way) does
  the runtime rely on the home's own root fence and check shape only (a real
  directory, never a symlink or reparse point).  Every runtime outside a verified
  home -- an explicit external HARNESS_STATE_DIR, a Contract Mock state_dir
  (home is None), or admission's temporary-directory fallback hyprial-gui-<uid>
  when state_dir is None -- keeps the full hard gate: each newly created root
  and runtime subdirectory is hardened with harden_private_path, and both the
  external state/fallback root and every runtime subdirectory down to runtime_dir
  must pass shape validation plus private_tree_violation (owner and no
  group/other bits on POSIX, the account-private DACL on Windows).
- The launcher, not the browser, chooses the one driver. The Contract Mock
  bearer is captured from its private stderr, redacted before logging, and
  never passed in argv. The Contract Mock is the
  JavaScript module gui/contract-mock run by the project's own Node (1e-2,
  2026-11-15); it keeps the stderr protocol of the Python mock it replaced, so
  MOCK_ENDPOINT_RE and MOCK_TOKEN_PREFIX are unchanged, and it reads the
  contract from the same source worktree.
- Launch grants live only in the host environment, the browser fragment, or an
  exclusive 0600 no-open handoff file. Public startup data contains paths and
  origins but never a grant, upstream bearer or credential-bearing URL.
- Decision host-serves-daemon-in-process-20261119 (2822, B3): with the daemon
  driver the host is the only child.  It serves request-http's dispatch in its own
  process, so the launcher starts no request-http child, reads no ``server.json``,
  publishes no ``frontend-authority.json`` and hands the host no bearer: the host
  gets the non-secret coordinates ``--home --state-dir --socket --node`` and an
  environment with the driver, the node, the home, the state directory and the
  grant (HYPRIAL_NODE_ID and HYPRIAL_HOME are what the old child's environment
  carried; GuiRuntimeHost and the kernel read them).  The startup record then has
  ``backend_pid`` null and an empty ``backend_endpoint``.
- Decision delete-request-http-child-20261119 (2822, B4): the request-http child
  helpers, the transport binding check and the runtime's server.lock probe are
  gone.  The names request-http.json, request-http.log, server.json, server.lock
  and frontend-authority.json stay only in stale-file lists (this module's
  _remove_stale_files and the session-end cleanup of lifecycle/process.py, which
  lists the first three) so a runtime left by an older build is cleared; nothing
  writes them any more.
- The frontend child is "the host": the Python session host for the installed
  wheel and for a source checkout alike (PR 3, 2026-11-16). Its environment,
  readiness line and log are named for the host (host_environment,
  parse_host_ready, host.log).
- ``hyprial gui dev`` owns one more child, the Vite dev server ("assets"). It
  gets the base environment with every HYPRIAL_GUI_* variable removed, so it
  holds no driver, endpoint, bearer, grant or principal, and its own readiness
  line (the dev serve script's HYPRIAL_GUI_READY with no driver) names the
  loopback port the host is told to relay to. A readiness line that names a
  driver, a non-development mode or a non-loopback host is refused.
- Decision external-runtime-ancestry-20261121 (W1C r2): external runtime
  fencing rejects every reparse point and every symlink on the target root or
  below it. A strict ancestor is accepted only for the POSIX system links
  /tmp, /var and /etc, preserving macOS's system layout without allowing
  user-controlled or Windows root-level links.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import urllib.parse
from collections.abc import Mapping
from typing import Any


from hyprial.kernel import (
    FileLockBusy,
    PrivateAccessUnsupported,
    ProcessFileLock,
    harden_private_path,
    path_is_reparse,
    private_tree_violation,
)

from ..errors import GuiError
from .admission import SourceAdmission
from .config import GuiLaunchPlan
from .handoff import frontend_url

MOCK_ENDPOINT_RE = re.compile(
    r"^endpoint: (http://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]+)/daemon/request/<method>$"
)
MOCK_TOKEN_PREFIX = "token: Bearer "
HOST_READY_PREFIX = "HYPRIAL_GUI_READY "
RUNTIME_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_HANDOFF_NAME = "launch.url"
_PUBLICATION_NAME = "launch.ready"
_HOST_LOG_NAME = "host.log"
_ASSETS_LOG_NAME = "assets.log"
_MOCK_LOG_NAME = "mock.log"


@dataclass(frozen=True, slots=True)
class BackendAuthority:
    driver: str
    endpoint: str
    token: str
    principal: str | None
    owner: str | None
    node: str

    @property
    def health_url(self) -> str:
        return f"{self.endpoint}/healthz"


@dataclass(slots=True)
class RuntimeLease:
    directory: Path
    _lock: ProcessFileLock | None

    @property
    def handoff_path(self) -> Path:
        return self.directory / _HANDOFF_NAME

    @property
    def publication_path(self) -> Path:
        return self.directory / _PUBLICATION_NAME

    def close(self) -> None:
        if self._lock is None:
            return
        lock, self._lock = self._lock, None
        lock.release()


def _is_real_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(info.st_mode)
        and not path_is_reparse(info)
        and stat.S_ISDIR(info.st_mode)
    )


def _steps_under(
    root: Path, target: Path, *, resolve_existing_root: bool = False
) -> tuple[Path, ...] | None:
    base = Path(os.path.abspath(root))
    target_abs = Path(os.path.abspath(target))
    if not target_abs.is_relative_to(base):
        if not resolve_existing_root:
            return None
        try:
            resolved = root.resolve()
        except OSError:
            return None
        if not target_abs.is_relative_to(resolved):
            return None
        base = resolved
    relative = target_abs.relative_to(base)
    steps: list[Path] = []
    current = base
    for part in relative.parts:
        current = current / part
        steps.append(current)
    return tuple(steps)


def _stays_inside_home(home: Path, home_resolved: Path, target: Path) -> bool:
    steps = _steps_under(home, target, resolve_existing_root=True)
    if steps is None:
        return False
    for step in steps:
        try:
            info = step.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or path_is_reparse(info):
            return False
    try:
        resolved = target.resolve()
    except OSError:
        return False
    return resolved == home_resolved or home_resolved in resolved.parents


def _runtime_inside_home(plan: GuiLaunchPlan) -> bool:
    directory = getattr(plan, "runtime_dir", None)
    admission = getattr(plan, "admission", None)
    if directory is None or admission is None:
        return False
    home = getattr(admission, "home", None)
    state_dir = getattr(admission, "state_dir", None)
    if home is None or state_dir is None or not _is_real_directory(home):
        return False
    try:
        home_resolved = home.resolve()
    except OSError:
        return False
    dir_steps = _steps_under(home, directory, resolve_existing_root=True)
    if not dir_steps:
        return False
    return _stays_inside_home(home, home_resolved, state_dir) and _stays_inside_home(
        home, home_resolved, directory
    )


def _make_directory(path: Path, *, harden: bool) -> None:
    try:
        path.mkdir(mode=RUNTIME_DIR_MODE)
    except FileExistsError:
        return
    if harden:
        harden_private_path(path, mode=RUNTIME_DIR_MODE)


def _require_directory_shape(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or path_is_reparse(info)
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise GuiError("GUI_RUNTIME_UNAVAILABLE", "the GUI runtime must be a real directory")
    return info


def _ensure_unlinked_parent(root: Path) -> None:
    _fence_external_tree(root)


def _fence_external_tree(target: Path) -> tuple[Path, ...]:
    target_abs = Path(os.path.abspath(target))
    chain: list[Path] = []
    current = Path(target_abs.anchor)
    for part in target_abs.parts[1:]:
        current /= part
        chain.append(current)
    for step in chain:
        try:
            info = step.lstat()
        except FileNotFoundError:
            _make_directory(step, harden=True)
            info = step.lstat()
        except OSError as error:
            raise GuiError(
                "GUI_RUNTIME_UNAVAILABLE", "the GUI runtime must be a real directory"
            ) from error
        if path_is_reparse(info):
            raise GuiError(
                "GUI_RUNTIME_UNAVAILABLE", "the GUI runtime must be a real directory"
            )
        if stat.S_ISLNK(info.st_mode):
            is_allowed_system_link = (
                step != target_abs
                and step.anchor == "/"
                and step.parts in {("/", "tmp"), ("/", "var"), ("/", "etc")}
            )
            if not is_allowed_system_link:
                raise GuiError(
                    "GUI_RUNTIME_UNAVAILABLE", "the GUI runtime must be a real directory"
                )
            try:
                if not step.is_dir():
                    raise GuiError(
                        "GUI_RUNTIME_UNAVAILABLE", "the GUI runtime must be a real directory"
                    )
            except OSError as error:
                raise GuiError(
                    "GUI_RUNTIME_UNAVAILABLE", "the GUI runtime must be a real directory"
                ) from error
        elif not stat.S_ISDIR(info.st_mode):
            raise GuiError(
                "GUI_RUNTIME_UNAVAILABLE", "the GUI runtime must be a real directory"
            )
    return tuple(chain)


def _external_fenced_paths(plan: GuiLaunchPlan, directory: Path) -> tuple[Path, ...]:
    admission = getattr(plan, "admission", None)
    state_dir = getattr(admission, "state_dir", None) if admission is not None else None
    if state_dir is None:
        _fence_external_tree(directory)
        return (directory.parent, directory)
    substeps = _steps_under(state_dir, directory)
    if substeps is not None:
        _fence_external_tree(directory)
        return tuple(dict.fromkeys((state_dir, *substeps)))
    _fence_external_tree(state_dir)
    _fence_external_tree(directory)
    return tuple(dict.fromkeys((state_dir, directory.parent, directory)))


def _prepare_runtime_directory(
    plan: GuiLaunchPlan, directory: Path, *, inside_home: bool
) -> None:
    """Create the runtime directory; fence every root and step outside a verified home."""

    try:
        if inside_home:
            admission = getattr(plan, "admission", None)
            home = getattr(admission, "home", None)
            state_dir = getattr(admission, "state_dir", None)
            assert home is not None and state_dir is not None
            state_steps = _steps_under(home, state_dir, resolve_existing_root=True) or ()
            dir_steps = _steps_under(home, directory, resolve_existing_root=True) or (
                directory,
            )
            for path in dict.fromkeys((*state_steps, *dir_steps)):
                _make_directory(path, harden=False)
                _require_directory_shape(path)
            return
        for path in _external_fenced_paths(plan, directory):
            _make_directory(path, harden=True)
            info = _require_directory_shape(path)
            _require_private(path, info)
    except PrivateAccessUnsupported as error:
        raise GuiError(
            "GUI_RUNTIME_UNAVAILABLE",
            f"cannot make the GUI runtime private: {error}",
        ) from error
    except OSError as error:
        raise GuiError(
            "GUI_RUNTIME_UNAVAILABLE",
            f"cannot prepare the private GUI runtime: {error}",
        ) from error


def _require_private(path: Path, info: os.stat_result) -> None:
    violation = private_tree_violation(path, info)
    if violation is not None:
        raise GuiError(
            "GUI_RUNTIME_UNAVAILABLE",
            "the GUI runtime outside the home must be private to the current user",
            {"path": str(path), "reason": violation},
        )


def prepare_runtime(plan: GuiLaunchPlan) -> RuntimeLease:
    """Create or validate the private runtime and take its supervisor lock."""

    directory = plan.runtime_dir
    if directory is None:
        raise GuiError("GUI_RUNTIME_UNAVAILABLE", "the source GUI has no private runtime path")
    _prepare_runtime_directory(plan, directory, inside_home=_runtime_inside_home(plan))

    lock = ProcessFileLock(directory / "supervisor.lock", mode=PRIVATE_FILE_MODE)
    try:
        lock.acquire(0.0)
    except FileLockBusy as error:
        raise GuiError(
            "GUI_RUNTIME_BUSY",
            "another foreground GUI already owns this runtime",
            {"runtime": str(directory)},
        ) from error
    except OSError as error:
        raise GuiError(
            "GUI_RUNTIME_UNAVAILABLE",
            f"cannot acquire the GUI supervisor lock: {error}",
        ) from error
    lease = RuntimeLease(directory=directory, _lock=lock)
    try:
        _remove_stale_files(directory)
    except BaseException:
        lease.close()
        raise
    return lease


def _remove_stale_files(directory: Path) -> None:
    for name in (
        _HANDOFF_NAME,
        _PUBLICATION_NAME,
        "request-http.json",
        _HOST_LOG_NAME,
        _ASSETS_LOG_NAME,
        _MOCK_LOG_NAME,
        "request-http.log",
        "server.json",
        "server.lock",
        "frontend-authority.json",
        "logout-custody.json",
    ):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise GuiError(
                "GUI_RUNTIME_UNAVAILABLE",
                f"cannot clear stale private runtime file {name}: {error}",
            ) from error


def mock_invocation(plan: GuiLaunchPlan) -> tuple[tuple[str, ...], Path, dict[str, str]]:
    admission = _mock_admission(plan)
    entry, cwd = _mock_entry(plan)
    environment = _base_environment(plan.env)
    for name in tuple(environment):
        if name.startswith(("HYPRIAL_", "HARNESS_")):
            environment.pop(name, None)
    argv = (
        plan.project.node,
        str(entry),
        "--scenario",
        admission.scenario,
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        *(
            item
            for origin in _allowed_origins(plan)
            for item in ("--allow-origin", origin)
        ),
    )
    return argv, cwd, environment


def mock_log_path(lease: RuntimeLease) -> Path:
    return lease.directory / _MOCK_LOG_NAME


def host_log_path(lease: RuntimeLease) -> Path:
    return lease.directory / _HOST_LOG_NAME


def assets_log_path(lease: RuntimeLease) -> Path:
    return lease.directory / _ASSETS_LOG_NAME


def assets_environment(plan: GuiLaunchPlan) -> dict[str, str]:
    """The Vite dev server's environment: no HYPRIAL_GUI_* variable of any kind."""
    return build_environment(plan.build_env or plan.env)


def _mock_entry(plan: GuiLaunchPlan) -> tuple[Path, Path]:
    source = (
        plan.auto_worktree.source
        if plan.auto_worktree is not None
        else plan.project.worktree
    )
    cwd = source / "gui"
    entry = cwd / "contract-mock" / "serve.ts"
    openapi = source / "contract" / "daemon-request" / "openapi.json"
    if not (entry.is_file() and openapi.is_file()):
        raise GuiError(
            "GUI_MOCK_UNAVAILABLE",
            "the selected source worktree does not contain gui/contract-mock "
            "and contract/daemon-request",
            {"worktree": str(source)},
        )
    return entry, cwd


def mock_line(line: str) -> tuple[str | None, str, str]:
    """Return ``(secret, redacted_line, kind)`` for one mock stdout line."""

    endpoint_match = MOCK_ENDPOINT_RE.fullmatch(line.strip())
    if endpoint_match is not None:
        return None, line, "endpoint"
    if line.startswith(MOCK_TOKEN_PREFIX):
        token = line[len(MOCK_TOKEN_PREFIX) :].strip()
        if not token:
            raise GuiError("GUI_BACKEND_CONTRACT", "the Contract Mock published an empty bearer")
        return token, "token: Bearer <redacted>", "token"
    return None, line, "other"


def mock_authority(endpoint: str, token: str, plan: GuiLaunchPlan) -> BackendAuthority:
    admission = _mock_admission(plan)
    if not isinstance(endpoint, str) or not isinstance(token, str) or not token:
        raise GuiError("GUI_BACKEND_CONTRACT", "the Contract Mock did not publish a complete authority")
    return BackendAuthority(
        driver="contract-mock",
        endpoint=_loopback_origin(endpoint),
        token=token,
        principal=admission.principal,
        owner=admission.owner,
        node=admission.node,
    )


def daemon_host_arguments(plan: GuiLaunchPlan) -> tuple[str, ...]:
    """The non-secret coordinates of the daemon the host serves in its own process."""

    admission = _daemon_admission(plan)
    assert admission.home is not None
    assert admission.state_dir is not None
    assert admission.socket_path is not None
    return (
        "--home",
        str(admission.home),
        "--state-dir",
        str(admission.state_dir),
        "--socket",
        str(admission.socket_path),
        "--node",
        admission.node,
    )


def host_environment(
    plan: GuiLaunchPlan,
    authority: BackendAuthority | None,
    grant: str,
    *,
    handoff_path: Path | None,
    publication_path: Path | None = None,
) -> dict[str, str]:
    environment = _base_environment(plan.env)
    environment.pop("HARNESS_SOCKET_PATH", None)
    environment.pop("HYPRIAL_GUI_HOME", None)
    # An ownerless bootstrap authority has no principal.  The inherited
    # supervisor value must be removed, not merely overwritten: an empty or
    # stale principal would hand the shell a fabricated identity.
    environment.pop("HYPRIAL_GUI_PRINCIPAL", None)
    if authority is None:
        admission = _daemon_admission(plan)
        assert admission.home is not None
        assert admission.state_dir is not None
        environment.update(
            {
                "HYPRIAL_GUI_BACKEND_DRIVER": "daemon",
                "HYPRIAL_NODE_ID": admission.node,
                "HYPRIAL_HOME": str(admission.home),
                "HARNESS_STATE_DIR": str(admission.state_dir),
                "HYPRIAL_GUI_LAUNCH_GRANT": grant,
            }
        )
    else:
        environment.update(
            {
                "HYPRIAL_GUI_BACKEND_DRIVER": authority.driver,
                "HYPRIAL_BACKEND_ENDPOINT": authority.endpoint,
                "HYPRIAL_NODE_ID": authority.node,
                "HYPRIAL_GUI_UPSTREAM_TOKEN": authority.token,
                "HYPRIAL_GUI_LAUNCH_GRANT": grant,
            }
        )
        if authority.principal is not None:
            environment["HYPRIAL_GUI_PRINCIPAL"] = authority.principal
    if handoff_path is not None:
        environment["HYPRIAL_GUI_HANDOFF_PATH"] = str(handoff_path)
    if publication_path is not None:
        environment["HYPRIAL_GUI_PUBLICATION_PATH"] = str(publication_path)
    return environment


def build_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = _base_environment(source)
    _drop_runtime_controls(environment)
    for name in (
        "HYPRIAL_GUI_LAUNCH_TTL_MS",
        "HYPRIAL_GUI_SESSION_TTL_MS",
        "HYPRIAL_GUI_MAX_SESSIONS",
        "HYPRIAL_GUI_CONTRACT_MOCK_SCENARIO",
    ):
        environment.pop(name, None)
    return environment


def generate_launch_grant() -> str:
    return secrets.token_urlsafe(48)


def launch_url(public_url: str, grant: str) -> str:
    return f"{public_url}#grant={urllib.parse.quote(grant, safe='')}"


def write_handoff(lease: RuntimeLease, url: str) -> Path:
    path = lease.handoff_path
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            PRIVATE_FILE_MODE,
        )
    except FileExistsError as error:
        raise GuiError("GUI_HANDOFF_CONFLICT", "the no-open handoff already exists") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(url)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def write_launch_publication(lease: RuntimeLease) -> Path:
    path = lease.publication_path
    try:
        write_private_json(
            path,
            {
                "schemaVersion": 1,
                "service": "hyprial-gui",
                "event": "launch-published",
            },
        )
    except FileExistsError as error:
        raise GuiError(
            "GUI_PUBLICATION_CONFLICT",
            "the launch publication marker already exists",
        ) from error
    except OSError as error:
        raise GuiError(
            "GUI_RUNTIME_UNAVAILABLE",
            f"cannot publish the GUI launch grant: {error}",
        ) from error
    return path


def remove_handoff(lease: RuntimeLease | None) -> None:
    if lease is None:
        return
    try:
        lease.handoff_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def parse_host_ready(line: str, plan: GuiLaunchPlan) -> dict[str, Any]:
    if not line.startswith(HOST_READY_PREFIX):
        raise GuiError("GUI_START_FAILED", "the source GUI emitted an unexpected readiness line")
    try:
        payload = json.loads(line[len(HOST_READY_PREFIX) :])
    except json.JSONDecodeError as error:
        raise GuiError("GUI_BACKEND_CONTRACT", "the source GUI readiness line is not JSON") from error
    if not isinstance(payload, dict):
        raise GuiError("GUI_BACKEND_CONTRACT", "the source GUI readiness line must be an object")
    expected_mode = "development" if plan.live_source else "production"
    driver = plan.driver
    port = payload.get("port")
    if plan.port == 0:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise GuiError(
                "GUI_BACKEND_CONTRACT",
                "the GUI host did not announce the loopback port it bound",
            )
        expected_url = frontend_url(plan.host, port).rstrip("/")
    else:
        port = plan.port
        expected_url = plan.url.rstrip("/")
    if (
        payload.get("service") != "hyprial-gui"
        or payload.get("mode") != expected_mode
        or payload.get("dataMode") != "hosted"
        or payload.get("driver") != driver
        or payload.get("backendConfigured") is not True
        or payload.get("session") != "launch-required"
        or payload.get("devTools", False) is not plan.dev_tools
        or payload.get("port") != port
        or payload.get("url") != expected_url
        or not _is_loopback(payload.get("host"))
    ):
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the source GUI did not confirm the selected driver and loopback origin",
        )
    return payload


def bound_plan(plan: GuiLaunchPlan, ready: Mapping[str, Any]) -> GuiLaunchPlan:
    """The plan with the port the host announced; unchanged for a fixed port."""
    if plan.port != 0:
        return plan
    port = int(ready["port"])
    return replace(plan, port=port, url=frontend_url(plan.host, port))


def parse_assets_ready(line: str) -> str:
    """Validate the dev server's readiness line and return its loopback origin."""
    if not line.startswith(HOST_READY_PREFIX):
        raise GuiError("GUI_START_FAILED", "the GUI development server emitted an unexpected readiness line")
    try:
        payload = json.loads(line[len(HOST_READY_PREFIX) :])
    except json.JSONDecodeError as error:
        raise GuiError("GUI_BACKEND_CONTRACT", "the GUI development server readiness line is not JSON") from error
    port = payload.get("port") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("service") != "hyprial-gui"
        or payload.get("mode") != "development"
        or payload.get("driver") is not None
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or payload.get("host") not in {"127.0.0.1", "::1"}
    ):
        raise GuiError(
            "GUI_BACKEND_CONTRACT",
            "the GUI development server did not report a driverless development server on a loopback port",
        )
    host = payload["host"]
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def open_private_append(path: Path, *, exclusive: bool = True) -> int:
    flags = os.O_CREAT | os.O_WRONLY
    flags |= os.O_EXCL if exclusive else os.O_APPEND
    return os.open(path, flags, PRIVATE_FILE_MODE)


def write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        PRIVATE_FILE_MODE,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _base_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = dict(source)
    for name in (
        "HYPRIAL_OWNER",
        "HYPRIAL_BACKEND_ENDPOINT",
        "HYPRIAL_GUI_UPSTREAM_TOKEN",
        "HYPRIAL_GUI_LAUNCH_GRANT",
        "HYPRIAL_GUI_HANDOFF_PATH",
        "HYPRIAL_GUI_PUBLICATION_PATH",
        "HYPRIAL_GUI_AUTH",
        "HYPRIAL_GUI_SETUP",
        "HYPRIAL_GUI_SETUP_CLI",
    ):
        environment.pop(name, None)
    return environment


def _drop_runtime_controls(environment: dict[str, str]) -> None:
    for name in tuple(environment):
        if name == "HARNESS_SOCKET_PATH" or name.startswith("HYPRIAL_GUI_"):
            environment.pop(name, None)


def _allowed_origins(plan: GuiLaunchPlan) -> list[str]:
    return [] if plan.port == 0 else [_origin(plan.url)]


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise GuiError("GUI_START_FAILED", "the GUI origin is invalid")
    return f"{parsed.scheme}://{parsed.netloc}"


def _loopback_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not _is_loopback(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise GuiError("GUI_BACKEND_CONTRACT", "the backend endpoint is not a loopback origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise GuiError("GUI_BACKEND_CONTRACT", "the backend endpoint has an invalid port") from error
    if port is None:
        raise GuiError("GUI_BACKEND_CONTRACT", "the backend endpoint has no port")
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_loopback(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.lower() in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        return True
    try:
        return ipaddress.ip_address(value.strip("[]")).is_loopback
    except ValueError:
        return False


def _daemon_admission(plan: GuiLaunchPlan) -> SourceAdmission:
    admission = plan.admission
    if admission is None or admission.driver != "daemon":
        raise GuiError("GUI_BACKEND_DRIVER", "the GUI plan did not select a daemon")
    return admission


def _mock_admission(plan: GuiLaunchPlan) -> SourceAdmission:
    admission = plan.admission
    if admission is None or admission.driver != "contract-mock":
        raise GuiError("GUI_BACKEND_DRIVER", "the GUI plan did not select a Contract Mock")
    return admission


__all__ = [
    "BackendAuthority",
    "MOCK_ENDPOINT_RE",
    "MOCK_TOKEN_PREFIX",
    "HOST_READY_PREFIX",
    "RuntimeLease",
    "build_environment",
    "daemon_host_arguments",
    "generate_launch_grant",
    "launch_url",
    "mock_authority",
    "mock_invocation",
    "mock_line",
    "mock_log_path",
    "assets_environment",
    "assets_log_path",
    "host_environment",
    "host_log_path",
    "open_private_append",
    "parse_assets_ready",
    "bound_plan",
    "parse_host_ready",
    "prepare_runtime",
    "remove_handoff",
    "write_handoff",
    "write_launch_publication",
    "write_private_json",
]
