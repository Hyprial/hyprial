"""Shared CLI runtime: error envelope, daemon IPC, home/socket selection.

The implicit daemon start and the login next step live in ``common/ensure``.
"""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.ensure import JSON_OUTPUT, OFF, ON, SCOPE, ensure_listening, with_next_step
from hyprial.shell.impl.cli.output import emit_error, emit_value, json_failure, run_command, tables

from typing import Any, NoReturn
from collections.abc import Callable
from hyprial.kernel import RequestPortError, daemon_request, daemon_socket_absent, daemon_socket_path
from pathlib import Path
from datetime import UTC, datetime
from hyprial.kernel import canonical_user_uri, parse_agent_uri
from hyprial.error import codes
from hyprial.kernel import ipc_errors
import json
import os
import subprocess
import sys

JsonObject = dict[str, Any]


def request_upgrade_steps(services: Any, *, apply: bool = False) -> JsonObject:
    """Use the daemon port; transport absence never executes a local migration."""
    method = "upgrade.steps.run" if apply else "upgrade.steps.status"
    try:
        return services.runtime.request(method, {"apply": True} if apply else {}, timeout=120.0)
    except (RequestPortError, ipc_errors.TransientDaemonError) as error:
        return {"steps": [], "pending": ["daemon-unavailable"], "unavailable": True,
                "code": error.code, "error": "daemon upgrade steps unavailable; no local migration was run"}


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


def _resolved_interactive_resume_cwd(actor: str, cwd: Path | None) -> Path:
    """Resolve an existing cwd without consulting native session history."""

    services = get_services()
    if cwd is None:
        result = services.runtime.request("agent.get", {"name": actor})
        raw_agent = result.get("agent")
        registered = raw_agent.get("cwd") if isinstance(raw_agent, dict) else None
        if not isinstance(registered, str) or not registered:
            raise services.CliError(
                "INVALID_CWD",
                "interactive resume without --cwd requires an existing agent "
                "with a registered cwd",
            )
        resolved = Path(registered).expanduser().resolve()
    else:
        resolved = cwd.expanduser().resolve()
    if not resolved.is_dir():
        raise services.CliError(
            "INVALID_CWD",
            f"interactive resume cwd is not an existing directory: {resolved}",
        )
    return resolved


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
    # Endpoint selection (including the "explicit socket never evaluates the
    # state root" rule) lives in the kernel client.
    services = get_services()
    return daemon_socket_path(services._state_dir, env=os.environ)


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
    ensure_daemon: bool = True,
) -> None:
    services = get_services()
    scope, output = SCOPE.set(ON if ensure_daemon else OFF), JSON_OUTPUT.set(json_output)
    try:
        run_command(
            operation,
            json_output=json_output,
            json_indent=json_indent,
            require_home=None if allow_missing_home else services.require_initialized_hyprial_home,
        )
    finally:
        JSON_OUTPUT.reset(output)
        SCOPE.reset(scope)


_RESTORE_FOLLOWUP_POLL_INTERVAL_SECONDS = 2.0


def _daemon_request(
    method: str,
    params: JsonObject | None = None,
    *,
    timeout: float | None = None,
    timeout_allowance: float = 0.0,
    restore_wait: float | None = None,
) -> Any:
    """Call the kernel client through the CLI's request port.

    Framing, connect/backlog patience, timeout, and error classification live
    only in :mod:`hyprial.kernel.impl.ipc.client`; this function supplies the
    CLI's endpoint/clock/id bindings and translates a non-transient port
    refusal into the CLI's user-facing ``CliError``.

    ``restore_wait`` bounds how long a DAEMON_RESTORING refusal is waited
    out before it surfaces as a CliError (None: the
    HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS budget, 0: fail fast).  Callers that
    answer the refusal with their own readiness projection (``ps``) or run
    in best-effort branches pass ``restore_wait=0.0`` so they never sit
    out the budget just to swallow or re-render the same error.
    """
    services = get_services()
    ensure_listening(services, method, daemon_socket_absent)
    try:
        # ``socket_path`` and ``clock`` are passed as CALLABLES: the kernel
        # client re-resolves both on every restore-wait retry, so a daemon
        # that moved its socket (or a test that swapped the service
        # bindings) between attempts is followed rather than frozen.  The
        # endpoint resolver reads the SAME composition-root binding daemon
        # commands use (``CliServices._socket_path``), never a second copy.
        return daemon_request(
            method,
            params,
            socket_path=_service_socket_path,
            timeout=timeout,
            restore_wait=restore_wait,
            clock=_service_clock,
            uuid4=_service_uuid4,
            **({"timeout_allowance": timeout_allowance} if timeout_allowance else {}),
        )
    except RequestPortError as error:
        with_step = with_next_step(services, error)
        if with_step is not None:
            raise with_step
        # The cause shape is per code, because the base client raised the two
        # paths differently: a malformed frame chained the decode error
        # (``from error``), every other refusal was raised bare. A single
        # "clean" mapping would change one of them.
        if error.code == "INVALID_JSON" and isinstance(error.__cause__, BaseException):
            raise services.CliError(error.code, str(error), error.data) from error.__cause__
        raise services.CliError(error.code, str(error), error.data)


def _service_clock() -> object:
    """Live clock binding: re-read the composition root on every attempt."""

    return get_services().time


def _service_socket_path() -> Path:
    """Live endpoint binding: the same one daemon commands consume.

    Reading ``get_services()._socket_path`` (rather than calling this
    module's ``_socket_path`` directly) keeps the request and the other
    daemon-backed commands on one injected endpoint: when the composition
    root rebinds ``_socket_path``, both follow it -- and the kernel client
    re-resolves it on every restore retry.
    """

    return get_services()._socket_path()


def _service_uuid4() -> Any:
    """Live request-id factory: re-read the composition root on every attempt."""

    return get_services().uuid4()


def is_start_verdict(error: BaseException) -> bool:
    """Whether ``_wait_for_daemon`` ended by its own idle/hard-cap/exit verdict."""

    return getattr(error, "code", None) in (
        codes.DAEMON_START_TIMEOUT,
        codes.DAEMON_START_FAILED,
    )


def _wait_for_daemon(
    *,
    timeout: float,
    process: subprocess.Popen[bytes] | None = None,
    startup_progress: Callable[[], JsonObject] | None = None,
    hard_cap: float = DAEMON_START_HARD_CAP_SECONDS,
    launch_id: str | None = None,
    job_exited: Callable[[], bool] | None = None,
) -> JsonObject:
    """Wait for phase ① with a progress-reset idle budget and hard cap.

    daemon.json is written when the daemon can serve -- socket bound, accept
    running, ping answerable -- not after restore, so this returns as soon as
    the daemon is answering instead of after every connector has come back.
    Restore progress is carried by ping's ``phase``/``restorePending`` fields
    for whoever needs it.  ``timeout`` is the maximum silence between startup
    phase observations; ``hard_cap`` is the absolute ceiling even while phases
    continue to advance. An explicit idle value can widen the silence window,
    but it never widens that absolute ceiling.  ``job_exited`` lets a
    supervisor-launched caller (service install, N2) report that its job ran
    and is gone; the budgets stay defined here and only here.
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
        if job_exited is not None and job_exited():
            raise services.CliError(
                codes.DAEMON_START_FAILED,
                "supervised daemon exited during startup",
            )
        if process is not None and process.poll() is not None:
            raise services.CliError(
                codes.DAEMON_START_FAILED,
                f"daemon exited during startup with status {process.returncode}",
            )
        if process is not None and not (
            services._daemon_ready_for_process(process.pid, launch_id=launch_id)
            if launch_id is not None
            else services._daemon_ready_for_process(process.pid)
        ):
            # The stale-client fence: an old generation's socket must not
            # pass this generation's wait.  The CLI launcher fences on its
            # per-launch nonce (daemon.json ``launchId``) because the pid it
            # spawned need not be the daemon's own (a Windows venv redirector).
            services.time.sleep(min(0.05, idle_deadline - now, hard_deadline - now))
            continue
        try:
            # One probe path (E2E-010 on intg, "unknown daemon method ping"):
            # `_daemon_probe` answers for every daemon generation -- ping, or
            # ps for a peer older than the ping contract -- and this wait must
            # not be the one place that forgets the fallback. Bound the probe
            # itself, not only the poll count, by the remaining wall time.
            remaining = (
                min(idle_deadline, hard_deadline) - services.time.monotonic()
            )
            if remaining <= 0:
                continue
            result = services._daemon_probe(timeout=min(0.5, remaining))
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
            health = result.get("storageHealth")
            if isinstance(health, dict) and health.get("healthy") is False:
                raise services.CliError(
                    codes.DAEMON_START_FAILED,
                    "daemon is reachable but its database health check failed",
                    {"storageHealth": health},
                )
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
        codes.DAEMON_START_TIMEOUT,
        f"daemon did not become ready within {limit_detail}{phase_detail}"
        + (f" ({last_error})" if last_error is not None else ""),
    )


def _daemon_ready_for_process(pid: int, *, launch_id: str | None = None) -> bool:
    """daemon.json names this launch: by its nonce when given, else by pid."""

    services = get_services()
    try:
        marker = json.loads((services._state_dir() / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(marker, dict):
        return False
    if launch_id is not None:
        return marker.get("launchId") == launch_id and type(marker.get("pid")) is int
    return marker.get("pid") == pid


def _daemon_probe(timeout: float = 0.5) -> JsonObject:
    """One readiness probe that works against any daemon generation.

    ``ping`` is the light method every daemon from this build on answers,
    including mid-restore.  A daemon older than that contract answers
    METHOD_NOT_FOUND, and for exactly that peer we fall back to ``ps`` --
    heavy, but the only readiness answer it has.  The fallback keeps `hyprial
    init`/`service` truthful during a rolling upgrade instead of failing
    against a daemon that is merely old.

    ``timeout`` is one wall-clock budget for the complete probe, not a budget
    per request.  In particular, a pre-ping peer cannot consume it once for
    ``ping`` and then again for the legacy ``ps`` fallback.
    """
    services = get_services()
    budget = max(0.0, timeout)
    deadline = services.time.monotonic() + budget

    try:
        result = services.runtime.request("ping", timeout=budget)
    except services.CliError as error:
        if error.code != codes.METHOD_NOT_FOUND:
            raise
        remaining = deadline - services.time.monotonic()
        if remaining <= 0:
            raise ipc_errors.IpcTimeoutError(
                f"daemon probe exhausted its {budget:g}s timeout before "
                "the legacy ps fallback"
            ) from error
        result = services.runtime.request("ps", timeout=remaining)
    if not isinstance(result, dict):
        raise services.CliError("INVALID_RESPONSE", "daemon probe result must be an object")
    return result


def _probe_reports_running(probe: JsonObject) -> bool:
    """Normalise the two probe shapes: ping is flat, the legacy ps nests."""

    if probe.get("running") is True:
        return True
    daemon = probe.get("daemon")
    return isinstance(daemon, dict) and daemon.get("running") is True


def device_sign_in_hint(issuer: str) -> str:
    """The device-flow hint, including where a person without an account goes.

    The device flow's sign-in page has no sign-up entry of its own, so a new
    user has nowhere to go and the device code simply expires.  Shown by
    ``login`` next to the device hint (infra-ops, 2026-10-20).  The signup
    page shares the login issuer's origin; the caller passes the issuer it
    already resolved, so the auth domain stays declared exactly once (T9).
    """

    signup_url = f"{issuer}/signup/app-hyprial"
    return (
        "on the sign-in page choose \"sign in with Feishu\" "
        "(使用飞书登录); that sign-in now approves this device. "
        f"No account yet? Create one first: {signup_url}"
    )


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


_UNRESOLVED_BINDING = object()


def _message_claim_params(
    source: str,
    *,
    on_behalf_of: str | None = None,
    binding: object = _UNRESOLVED_BINDING,
) -> JsonObject:
    """Attach an agent session's opaque fence only to its own actor claim.

    Callers that already resolved the one per-operation binding pass it in;
    other callers retain the lazy read.  No process-wide cache is used.
    """

    if binding is _UNRESOLVED_BINDING:
        binding = get_services().runtime.worker_session_binding()
    worker_actor = binding.actor if binding is not None else None
    worker_session = binding.session_ref if binding is not None else None
    params: JsonObject = {"from": source}
    parsed = parse_agent_uri(worker_actor) if worker_actor else None
    same_actor = bool(
        worker_actor
        and worker_session
        and (source == worker_actor or (parsed is not None and source == parsed[2]))
    )
    if binding is not None and not same_actor:
        from hyprial.identity import PAC_PRINCIPAL_UNVERIFIED, PacError

        message = (
            f"--from {source!r} disagrees with the verified acting principal "
            f"{worker_actor!r}"
        )
        raise PacError(
            PAC_PRINCIPAL_UNVERIFIED,
            message,
            {"claimed": source, "verified": worker_actor},
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
