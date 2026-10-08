"""Shared CLI runtime: error envelope, daemon IPC, home/socket selection."""

from __future__ import annotations

from hyprial.shell.impl.cli.output.channels import warn

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import emit_error, emit_value, json_failure, run_command, tables

from typing import Any, NoReturn
from collections.abc import Callable
from hyprial.kernel import RequestPortError
from pathlib import Path
from datetime import UTC, datetime
from hyprial.kernel import canonical_user_uri, parse_agent_uri
import errno
from hyprial.kernel import ipc_errors
import json
import math
import os
import socket
import subprocess
import sys

JsonObject = dict[str, Any]


_INIT_READY_TIMEOUT_ENV = "HYPRIAL_INIT_READY_TIMEOUT"


DAEMON_START_IDLE_BUDGET_SECONDS = 30.0


DAEMON_START_HARD_CAP_SECONDS = 120.0


class CliError(RequestPortError):
    """A stable, user-facing CLI error.

    Canonical base is kernel's ``RequestPortError`` (code, message, data=None)
    so daemon/PAC code raises the shared pure error and the shell translates
    presentation - no daemon/biz import of this shell class.  Constructor,
    ``str()``, ``args``, ``code`` and ``data`` semantics are unchanged.
    """

    def __init__(self, code: str, message: str, data: Any | None = None) -> None:
        super().__init__(code, message, data)


def _hyprial_home() -> Path:
    services = get_services()
    return services.configured_hyprial_home()[0]


def _agent_workspace(actor: str) -> Path:
    """Return one validated local actor's default private workspace path."""
    services = get_services()

    from hyprial.identity import AgentRegistry

    name = AgentRegistry.normalize_actor(actor)
    return services._hyprial_home() / "agents" / name / "workspace"


def _resolved_agent_cwd(actor: str, cwd: Path | None) -> Path:
    return cwd.expanduser().resolve() if cwd is not None else _agent_workspace(actor)


def _stdin_isatty() -> bool:
    """Whether a CLI confirmation can be answered (a narrow test seam)."""

    return sys.stdin.isatty()


def _state_dir() -> Path:
    """State root: ``HARNESS_STATE_DIR`` when set, else ``<HYPRIAL_HOME>/state``.

    Both variables are isolation boundaries and the repo's own isolated
    layouts set them as siblings (``root/home`` + ``root/state``: the unit
    conftest's autouse fixture, 47 test files, the E2E ``IsolatedDaemon``),
    so "state dir outside the home" is a first-class shape, not a conflict.
    Do not rule precedence by value: the two values alone cannot tell that
    layout from a child that named its own ``HYPRIAL_HOME`` while inheriting a
    parent's ``HARNESS_STATE_DIR`` (card 85dd41e2; a "home wins when they
    disagree" rule turned 45 tests red on 2026-09-05).  That leak is closed on
    the exporting side instead: ``child_state_environment`` hands a child
    ``HARNESS_STATE_DIR`` only when the state really lives outside
    ``<home>/state``, so an inherited environment usually carries no state
    dir for an explicit ``HYPRIAL_HOME`` to lose against.
    """
    services = get_services()
    configured = os.environ.get("HARNESS_STATE_DIR")
    return (
        Path(configured).expanduser().resolve() if configured else services._hyprial_home() / "state"
    )

def _endpoint_args(raw: str) -> tuple[str, ...]:
    """Parse a CLI endpoint flag value; an empty string clears the side."""

    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _socket_path() -> Path:
    # An explicit state/home root is an isolation boundary.  Agent runtimes may
    # inject HARNESS_SOCKET_PATH for their own production daemon; allowing it
    # to override a test-owned state root would silently escape isolation.
    services = get_services()
    if "HARNESS_STATE_DIR" in os.environ or "HYPRIAL_HOME" in os.environ:
        return services._state_dir() / "daemon.sock"
    configured = os.environ.get("HARNESS_SOCKET_PATH")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else services._state_dir() / "daemon.sock"
    )


def _json_failure(error: Exception) -> JsonObject:
    return json_failure(error)


def _emit(value: Any, *, json_output: bool, json_indent: int | None = None) -> None:
    emit_value(value, json_output=json_output, json_indent=json_indent)


def _fail(error: Exception, *, json_output: bool) -> NoReturn:
    emit_error(error, json_output=json_output)


def _execute(
    operation: Callable[[], Any],
    *,
    json_output: bool,
    json_indent: int | None = None,
    allow_missing_home: bool = False,
) -> None:
    services = get_services()
    run_command(
        operation,
        json_output=json_output,
        json_indent=json_indent,
        require_home=None if allow_missing_home else services.require_initialized_hyprial_home,
    )


_PEER_GONE_ERRNOS = frozenset(
    {
        # EPIPE: the peer is gone at write time (macOS).  ECONNRESET: the
        # peer is gone at write OR read time (Linux).  Same fact, different
        # platforms -- discriminate by errno, not by exception class, so the
        # next platform cannot leak a third spelling.
        errno.EPIPE,
        errno.ECONNRESET,
    }
)


_CONNECT_BACKLOG_ERRNOS = frozenset(
    {
        # Linux answers connect() to a full AF_UNIX accept queue with an
        # immediate EAGAIN -- even a timeout socket never waits for a slot
        # there (CPython surfaces that EAGAIN as BlockingIOError after its
        # one select() round), so a patient caller must retry within its
        # budget or a healthy request looks starved whenever the dispatcher
        # is briefly slower than its clients.  EWOULDBLOCK is the same value
        # on POSIX but is listed so the intent survives a platform where the
        # two diverge.
        errno.EAGAIN,
        errno.EWOULDBLOCK,
    }
)


_CONNECT_BACKLOG_RETRY_INTERVAL = 0.01


def _connect_daemon_socket(socket_path: Path, timeout: float) -> socket.socket:
    """Connect to the daemon, waiting out a momentarily full listen backlog.

    The retry budget is the request's own ``timeout``: the kernel only parks
    a connect() against a full AF_UNIX accept queue for fully blocking
    sockets, and this caller always runs with a timeout, so patience has to
    be explicit.  Only the full-backlog spelling (EAGAIN/EWOULDBLOCK) is
    retried -- ECONNREFUSED still fails fast because it also describes a
    stale socket file whose daemon is gone, which must not take the whole
    budget to report.
    """
    services = get_services()

    if os.name == "nt":
        from hyprial.kernel import connect_named_pipe as connect

        return connect(socket_path, timeout)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    deadline = services.time.monotonic() + timeout
    try:
        while True:
            try:
                client.connect(str(socket_path))
            except OSError as error:
                if (
                    error.errno in _CONNECT_BACKLOG_ERRNOS
                    and services.time.monotonic() < deadline
                ):
                    services.time.sleep(_CONNECT_BACKLOG_RETRY_INTERVAL)
                    continue
                raise
            return client
    except BaseException:
        client.close()
        raise


def _peer_gone(
    socket_path: Path, error: OSError | None = None
) -> ipc_errors.DaemonDisconnectedError:
    detail = f" ({error})" if error is not None else ""
    # PR #332 F4②: transport mints use the registered transient classes so
    # every daemon-request failure is catchable as TransientDaemonError
    # without a second string-comparison system.
    return ipc_errors.DaemonDisconnectedError(
        f"connection closed by the Harness daemon at {socket_path} "
        f"before any answer{detail}"
    )


_RESTORE_WAIT_POLL_SECONDS = 0.25


_RESTORE_WAIT_DEFAULT_SECONDS = 120.0


_RESTORE_WAIT_MAX_SECONDS = 900.0


_DAEMON_IPC_ROUNDTRIP_SECONDS = 15.0


_RESTORE_FOLLOWUP_POLL_INTERVAL_SECONDS = 2.0


def _restore_wait_budget() -> float:
    """Bounded seconds a user command waits out a DAEMON_RESTORING refusal.

    PR #332 F4①: while the daemon's restore gate is closed, heavy methods
    answer DAEMON_RESTORING at dispatch -- BEFORE any side effect -- so a
    refused mutation never executed and waiting the refusal out is safe.
    The default covers a derived restore round (F1:
    (ceil(targets/width)+1) x per-start timeout) for typical fleets;
    HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS=0 restores the fail-fast behaviour for
    scripts that prefer it.
    """

    raw = os.environ.get("HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS")
    try:
        value = _RESTORE_WAIT_DEFAULT_SECONDS if raw is None else float(raw)
    except ValueError:
        return _RESTORE_WAIT_DEFAULT_SECONDS
    if not math.isfinite(value) or not 0.0 <= value <= _RESTORE_WAIT_MAX_SECONDS:
        return _RESTORE_WAIT_DEFAULT_SECONDS
    return value


def _daemon_request(
    method: str,
    params: JsonObject | None = None,
    *,
    timeout: float = _DAEMON_IPC_ROUNDTRIP_SECONDS,
    restore_wait: float | None = None,
) -> Any:
    """Call the daemon's version-1 newline-delimited JSON IPC protocol.

    ``restore_wait`` bounds how long a DAEMON_RESTORING refusal is waited
    out before it surfaces as a CliError (None: the
    HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS budget, 0: fail fast).  Callers that
    answer the refusal with their own readiness projection (``ps``) or run
    in best-effort branches pass ``restore_wait=0.0`` so they never sit
    out the budget just to swallow or re-render the same error.
    """
    services = get_services()

    budget = _restore_wait_budget() if restore_wait is None else restore_wait
    deadline = services.time.monotonic() + budget
    while True:
        try:
            return _daemon_request_once(method, params, timeout=timeout)
        # PR #332 F4②: the restore-wait judgment is the registered class,
        # not a code comparison.  The other transient classes (transport
        # blips) propagate untouched, exactly as the old ``code !=
        # DAEMON_RESTORING`` raise did.
        except ipc_errors.DaemonRestoringError:
            if services.time.monotonic() >= deadline:
                raise
            services.time.sleep(_RESTORE_WAIT_POLL_SECONDS)


def _daemon_request_once(
    method: str,
    params: JsonObject | None = None,
    *,
    timeout: float = _DAEMON_IPC_ROUNDTRIP_SECONDS,
) -> Any:
    """Call the daemon's version-1 newline-delimited JSON IPC protocol."""
    services = get_services()

    request_id = str(services.uuid4())
    frame: JsonObject = {"version": 1, "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    client: socket.socket | None = None
    try:
        try:
            client = _connect_daemon_socket(services._socket_path(), timeout)
        except OSError as error:
            # Report only what happened: which socket, and why the connect
            # failed.  The cause (never started / mid-restart / hijacked
            # HYPRIAL_HOME / permissions) cannot be told apart from here, so no
            # remedy is embedded -- guidance belongs to skills and docs,
            # which can be updated; a string constant cannot.
            raise ipc_errors.DaemonUnavailableError(
                f"cannot connect to the Harness daemon socket "
                f"{services._socket_path()}: {error}"
            ) from error
        try:
            client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        except OSError as error:
            if error.errno in _PEER_GONE_ERRNOS:
                raise _peer_gone(services._socket_path(), error) from error
            raise
        buffer = bytearray()
        while len(buffer) <= 8 * 1024 * 1024:
            try:
                chunk = client.recv(64 * 1024)
            except OSError as error:
                if error.errno in _PEER_GONE_ERRNOS:
                    raise _peer_gone(services._socket_path(), error) from error
                raise
            if not chunk:
                raise _peer_gone(services._socket_path())
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not line.strip():
                    continue
                try:
                    response = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise services.CliError(
                        "INVALID_JSON", f"invalid daemon IPC response: {error}"
                    ) from error
                if not isinstance(response, dict) or response.get("version") != 1:
                    raise services.CliError(
                        ipc_errors.VERSION_MISMATCH, "unsupported daemon IPC version"
                    )
                if response.get("id") != request_id:
                    # Event frames and responses for other request IDs are not
                    # the result of this one-shot request.
                    continue
                failure = response.get("error")
                if isinstance(failure, dict):
                    code = str(failure.get("code", ipc_errors.DAEMON_ERROR))
                    message = str(failure.get("message", "daemon request failed"))
                    # PR #332 F4②: transient envelope codes deserialise
                    # through the shared registry into the SAME class the
                    # daemon minted; every other code stays a CliError (the
                    # CLI's user-facing surface).
                    transient = ipc_errors.transient_error_from_code(
                        code, message, failure.get("data")
                    )
                    if transient is not None:
                        raise transient
                    raise services.CliError(
                        code,
                        message,
                        failure.get("data"),
                    )
                if "result" not in response:
                    raise services.CliError(
                        "INVALID_RESPONSE", "daemon response is missing result"
                    )
                return response["result"]
        raise services.CliError("IPC_RESPONSE_TOO_LARGE", "daemon IPC response exceeded 8 MiB")
    except TimeoutError as error:
        raise ipc_errors.IpcTimeoutError(
            f"timed out after {timeout:g}s waiting for the Harness daemon at "
            f"{services._socket_path()} to answer method {method!r}",
        ) from error
    finally:
        if client is not None:
            client.close()


def _wait_for_daemon(
    *,
    timeout: float,
    process: subprocess.Popen[bytes] | None = None,
    startup_progress: Callable[[], JsonObject] | None = None,
    hard_cap: float = DAEMON_START_HARD_CAP_SECONDS,
) -> JsonObject:
    """Wait for phase ① with a progress-reset idle budget and hard cap.

    daemon.json is written when the daemon can serve -- socket bound, accept
    running, ping answerable -- not after restore, so this returns as soon as
    the daemon is answering instead of after every connector has come back.
    Restore progress is carried by ping's ``phase``/``restorePending`` fields
    for whoever needs it.  ``timeout`` is the maximum silence between startup
    phase observations; ``hard_cap`` is the absolute ceiling even while phases
    continue to advance. An explicit idle value can widen the silence window,
    but it never widens that absolute ceiling.
    """
    services = get_services()

    started = services.time.monotonic()
    idle_deadline = started + timeout
    effective_hard_cap = hard_cap
    hard_deadline = started + effective_hard_cap
    last_progress: tuple[int, str | None] = (0, None)
    progress_evidence: JsonObject = {
        "lastStartupPhase": None,
        "phasesSeen": 0,
    }
    last_error: Exception | None = None
    limit = "idle"
    while True:
        now = services.time.monotonic()
        if startup_progress is not None:
            observed = startup_progress()
            phases_seen = observed.get("phasesSeen")
            phase = observed.get("lastStartupPhase")
            current_progress = (
                phases_seen
                if isinstance(phases_seen, int) and not isinstance(phases_seen, bool)
                else 0,
                phase if isinstance(phase, str) else None,
            )
            if (
                current_progress[0] > last_progress[0]
                or current_progress[1] != last_progress[1]
            ):
                idle_deadline = now + timeout
                last_progress = current_progress
            progress_evidence = {
                "lastStartupPhase": current_progress[1],
                "phasesSeen": current_progress[0],
            }
        if now >= hard_deadline:
            limit = "hard"
            break
        if now >= idle_deadline:
            break
        if process is not None and process.poll() is not None:
            raise services.CliError(
                ipc_errors.DAEMON_START_FAILED,
                f"daemon exited during startup with status {process.returncode}",
            )
        if process is not None and not services._daemon_ready_for_process(process.pid):
            # The pid check is still the stale-client fence: an old
            # generation's socket must not pass this generation's wait.
            services.time.sleep(min(0.05, idle_deadline - now, hard_deadline - now))
            continue
        try:
            # One probe path (E2E-010 on intg, "unknown daemon method ping"):
            # `_daemon_probe` answers for every daemon generation -- ping, or
            # ps for a peer older than the ping contract -- and this wait must
            # not be the one place that forgets the fallback.
            result = services._daemon_probe(timeout=0.5)
        # PR #332 F4②: the boot-wait retry set is spelled as the registered
        # classes -- the same three transport codes the old literal set held;
        # DAEMON_RESTORING deliberately stays out (ping is in the restore
        # gate's light set, and the restore budget lives in
        # ``_daemon_request``).
        except (
            ipc_errors.DaemonUnavailableError,
            ipc_errors.DaemonDisconnectedError,
            ipc_errors.IpcTimeoutError,
        ) as error:
            last_error = error
            remaining = min(idle_deadline, hard_deadline) - services.time.monotonic()
            if remaining > 0:
                services.time.sleep(min(0.05, remaining))
            continue
        except services.CliError as error:
            last_error = error
            raise
        if _probe_reports_running(result):
            return result
        last_error = services.CliError("INVALID_RESPONSE", "daemon did not report ready")
        remaining = min(idle_deadline, hard_deadline) - services.time.monotonic()
        if remaining > 0:
            services.time.sleep(min(0.05, remaining))
    phase = progress_evidence["lastStartupPhase"]
    phase_detail = f"; last startup phase was {phase!r}" if phase is not None else ""
    limit_detail = (
        f"the hard cap of {effective_hard_cap:g}s"
        if limit == "hard"
        else f"{timeout:g}s without startup progress"
    )
    raise services.CliError(
        ipc_errors.DAEMON_START_TIMEOUT,
        f"daemon did not become ready within {limit_detail}{phase_detail}"
        + (f" ({last_error})" if last_error is not None else ""),
    )


def _daemon_ready_for_process(pid: int) -> bool:
    services = get_services()
    try:
        marker = json.loads((services._state_dir() / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and marker.get("pid") == pid


def _daemon_probe(timeout: float = 0.5) -> JsonObject:
    """One readiness probe that works against any daemon generation.

    ``ping`` is the light method every daemon from this build on answers,
    including mid-restore.  A daemon older than that contract answers
    METHOD_NOT_FOUND, and for exactly that peer we fall back to ``ps`` --
    heavy, but the only readiness answer it has.  The fallback keeps `hyprial
    init`/`service` truthful during a rolling upgrade instead of failing
    against a daemon that is merely old.
    """
    services = get_services()

    try:
        result = services._daemon_request("ping", timeout=timeout)
    except services.CliError as error:
        if error.code != ipc_errors.METHOD_NOT_FOUND:
            raise
        result = services._daemon_request("ps", timeout=timeout)
    if not isinstance(result, dict):
        raise services.CliError("INVALID_RESPONSE", "daemon probe result must be an object")
    return result


def _probe_reports_running(probe: JsonObject) -> bool:
    """Normalise the two probe shapes: ping is flat, the legacy ps nests."""

    if probe.get("running") is True:
        return True
    daemon = probe.get("daemon")
    return isinstance(daemon, dict) and daemon.get("running") is True


_SQUIRE_SETUP_PARTS = (
    ("profile", "no squire profile yet"),
    ("channel", "no Feishu channel bound to your squire"),
    ("ownerOpenId", "your Feishu account is not bound to your squire"),
)


_DEVICE_SIGN_IN_HINT = (
    "on the sign-in page choose \"sign in with an existing account\" "
    "(使用已有账户登录); signing in with Feishu (使用飞书登录) currently does not "
    "approve this device, and this command would wait until the code expires"
)


def _squire_setup_warning() -> JsonObject | None:
    """Guide the owner to set up their squire when the daemon (re)starts.

    Allen 2026-09-26: on daemon start/restart/init, check that the owner's
    squire and user-proxy are configured, and guide setup in that order --
    squire first, then squire guides the user-proxy setup.  Without a squire
    nothing reaches this person through ``user:<owner>``; delivery fails with
    TARGET_SQUIRE_UNCONFIGURED only when someone first tries.

    ⚠️ Local files only (settings.json for the owner, users.json for the
    profile): no daemon call, no network, never a write.  It runs inside the
    commands that start the daemon, whose budgets are tight.  "Configured" is
    the daemon's own test for a user target (a profile with a channel and an
    owner open_id, daemon/application.py `_user_targets`).

    Silent when there is no owner yet (login's own next steps cover that) and
    for an isolated daemon, which cannot reach Feishu at all.
    """
    services = get_services()

    if os.environ.get("HYPRIAL_NETWORK_ISOLATED", "").strip() not in ("", "0"):
        return None
    from hyprial.daemon import node_owner_or_none
    from hyprial.identity import UserProfileError, UserProfileStore

    owner = node_owner_or_none()
    if owner is None:
        return None
    try:
        profile = UserProfileStore(services._state_dir() / "users.json").resolve(owner)
    except (UserProfileError, OSError) as error:
        return {
            "code": "SQUIRE_PROFILE_UNREADABLE",
            "message": (
                f"cannot read the squire profiles ({error}); messages to user:{owner} "
                "will fail until it is fixed. Check state/users.json, then run: hyprial squire setup"
            ),
            "data": {"owner": owner, "nextStep": "hyprial squire setup"},
        }
    if profile is None:
        missing = [name for name, _ in _SQUIRE_SETUP_PARTS]
    else:
        missing = [
            name
            for name, present in (
                ("channel", profile.squire_adapter is not None),
                ("ownerOpenId", profile.owner_open_id is not None),
            )
            if not present
        ]
    if not missing:
        return _user_proxy_setup_warning(owner, profile)
    first = dict(_SQUIRE_SETUP_PARTS)[missing[0]]
    return {
        "code": "SQUIRE_NOT_CONFIGURED",
        "message": (
            f"your squire is not set up ({first}), so nobody can reach you through "
            f"{canonical_user_uri(owner)} yet. Set it up with: hyprial squire setup"
        ),
        "data": {"owner": owner, "missing": missing, "nextStep": "hyprial squire setup"},
    }


def _user_proxy_setup_warning(owner: str, profile: Any) -> JsonObject | None:
    """The second step of the startup guidance, once the squire is complete.

    Without a user-proxy (users.json ``deliveryAgent``), messages to
    ``user:<owner>`` arrive through the squire's chat, mixed in with the
    squire's own traffic.  Setup needs a Feishu app of the person's own (the
    squire's adapter is refused), and it must run on the preferred-receiver
    node, so the message says both before the command.
    """

    if profile.delivery_agent is not None:
        return None
    machine = profile.preferred_receiver.machine
    next_step = "hyprial user-proxy setup --adapter <your-adapter> --route <route>"
    return {
        "code": "SQUIRE_USER_PROXY_NOT_CONFIGURED",
        "message": (
            f"your squire is set up, but messages to user:{owner} still arrive in the "
            "squire's chat, mixed with its own. For a separate channel, add a Feishu "
            f"adapter of your own (not the squire's), then on {machine} run: {next_step}"
        ),
        "data": {"owner": owner, "machine": machine, "nextStep": next_step},
    }


def _announce_setup_guidance(result: JsonObject) -> None:
    """One stderr line per setup warning, for a person reading the terminal.

    The human output of init/restart is the whole result dict, where a
    warning sits among forty fields.  Same shape as the plugin-skip lines.
    """
    for warning in result.get("warnings") or []:
        if isinstance(warning, dict) and str(warning.get("code", "")).startswith("SQUIRE_"):
            warn(f"hyprial: {warning['message']}", json_output=False)


def _overview_duration(milliseconds: int) -> str:
    return tables.duration(milliseconds)


def _overview_age(milliseconds: object, *, now_ms: int) -> str:
    return tables.age(milliseconds, now_ms=now_ms)


def _overview_next(milliseconds: object, *, now_ms: int) -> str:
    return tables.relative(milliseconds, now_ms=now_ms)


def _overview_sender(value: object) -> str:
    # hyprial.uri owns identity parsing (tests/test_address_parsing_guard.py):
    # the actor segment of an agent/channel URI; user:<x> and the rest verbatim.
    from hyprial.kernel import short_actor_name

    return short_actor_name(str(value or "-"))


def _overview_last_segment(value: object) -> str:
    return _overview_sender(value)


def _overview_table(
    title: str,
    headers: list[str],
    cells: list[list[str]],
    *,
    truncate_column: int | None = None,
) -> str:
    return tables.render_table(title, headers, cells, truncate_column=truncate_column)


def _message_claim_params(
    source: str, *, on_behalf_of: str | None = None
) -> JsonObject:
    """Attach a managed worker's opaque fence only to its own actor claim."""

    params: JsonObject = {"from": source}
    worker_actor = os.environ.get("HYPRIAL_WORKER_ACTOR")
    worker_session = os.environ.get("HYPRIAL_WORKER_SESSION_REF")
    parsed = parse_agent_uri(worker_actor) if worker_actor else None
    same_actor = bool(
        worker_actor
        and worker_session
        and (source == worker_actor or (parsed is not None and source == parsed[2]))
    )
    if same_actor:
        params["sessionRef"] = worker_session
    if on_behalf_of is not None:
        params["onBehalfOf"] = on_behalf_of
    return params


def _local_operator_identity() -> str:
    services = get_services()
    from hyprial.daemon import resolve_node_owner

    home, _source = services.configured_hyprial_home()
    return canonical_user_uri(resolve_node_owner(hyprial_home=home))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()

def _append_warning(result: JsonObject, warning: JsonObject | None) -> None:
    services = get_services()
    if warning is None:
        return
    warnings = result.setdefault("warnings", [])
    if not isinstance(warnings, list):
        raise services.CliError("INVALID_RESPONSE", "warnings must be an array")
    warnings.append(warning)



def _user_proxy_launch_params(
    name: str, route_uri: str, *, cwd: Path | None = None
) -> JsonObject:
    resolved_cwd = _resolved_agent_cwd(name, cwd)
    return {
        "provider": "user-proxy",
        "name": name,
        "headless": True,
        "args": [],
        "cwd": str(resolved_cwd),
        "command": [
            os.path.abspath(sys.executable),
            "-m",
            "hyprial.daemon.impl.harnesses.python_worker._user_proxy_worker",
            "--kind",
            "user-proxy",
            "--route",
            route_uri,
        ],
    }



def _render_workflow_list(
    rows: list[JsonObject], *, all_callers: bool, now_ms: int | None = None
) -> str:
    services = get_services()
    observed_at = services.time.time_ns() // 1_000_000 if now_ms is None else now_ms
    cells = []
    for row in rows:
        current = row.get("currentNode")
        current_text = (
            f"{current.get('nodeId', '?')}:{current.get('state', '?')}"
            if isinstance(current, dict)
            else "-"
        )
        name = str(row.get("name", "?"))
        if row.get("parentGraphId"):
            name = f"↳ {name} (parent {str(row['parentGraphId'])[:12]})"
        cells.append(
            [
                str(row.get("graphId", "?"))[:12],
                name,
                str(row.get("state", "?")),
                _overview_sender(row.get("sender")),
                _overview_age(row.get("createdAtMs"), now_ms=observed_at),
                _overview_age(row.get("lastProgressAtMs"), now_ms=observed_at),
                current_text,
            ]
        )
    suffix = " (all callers)" if all_callers else ""
    return _overview_table(
        f"hyprial workflow list   {len(rows)} runs{suffix}",
        ["GRAPH", "NAME", "STATE", "SENDER", "AGE", "PROGRESS", "CURRENT"],
        cells,
        truncate_column=1,
    )


def _render_routine_list(
    rows: list[JsonObject], *, all_callers: bool, now_ms: int | None = None
) -> str:
    services = get_services()
    observed_at = services.time.time_ns() // 1_000_000 if now_ms is None else now_ms
    cells = [
        [
            str(row.get("name", "?")),
            _overview_last_segment(row.get("owner")),
            str(row.get("mode", "?")),
            "yes" if row.get("enabled") is True else "no",
            _overview_age(row.get("lastDispatchAtMs"), now_ms=observed_at),
            str(row.get("lastOutcome") or "-"),
            _overview_next(row.get("nextDueMs"), now_ms=observed_at)
            if row.get("enabled") is True
            else "paused",
            str(len(row.get("inFlight", [])))
            if isinstance(row.get("inFlight"), list)
            else "0",
        ]
        for row in rows
    ]
    suffix = " (all callers)" if all_callers else ""
    return _overview_table(
        f"hyprial routine list   {len(rows)} routines{suffix}",
        ["NAME", "OWNER", "MODE", "ON", "LAST RUN", "LAST RESULT", "NEXT", "IN-FLIGHT"],
        cells,
        truncate_column=0,
    )
