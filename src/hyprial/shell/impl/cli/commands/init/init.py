# Decision record (Z-5 PR 2, owner decision 2026-11-18): ``hyprial init`` never logs in.
# - On a home with no account init creates the home, starts the bootstrap daemon
#   and reports ``phase: bootstrap``, ``owner: null`` and ``nextSteps:
#   ["hyprial login"]`` (exit 0).  The result carries no ``identity``, ``device``,
#   ``verificationUri`` or ``userCode``: those belong to ``hyprial login``, whose
#   first login is one daemon operation.  A daemon that is already running in
#   bootstrap gets the same next step.
# - Malformed settings stay loud: ``node_owner_or_none`` raises on them before any
#   daemon is started, exactly as it did when init decided whether to log in.
# - Sign-in is the daemon's: init and login share no identity code any more.
# - ``init`` stays a group so future initialization subcommands have a home,
#   but a bare ``hyprial init`` still runs the initialization below (a
#   deliberate exception to the help-on-bare-invocation rule).
"""The ``hyprial init`` home/daemon initialization command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import DaemonLaunchResult
from hyprial.kernel import ipc_errors
import os
import typer
from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS, JsonObject, _INIT_READY_TIMEOUT_ENV, _append_warning
from hyprial.shell.impl.cli.output import warn

init_app = group(
    help="Initialize home and daemon.",
    invoke_without_command=True,
    no_args_is_help=False,
)



def _zenoh_endpoint_warning(result: JsonObject) -> JsonObject | None:
    """Return a startup warning when the effective endpoints are empty.

    Used by `hyprial init` so the operator sees the isolation hazard in the very
    command that starts the daemon; the daemon itself stays up (single-node
    local development without endpoints is legitimate).
    """

    zenoh = result.get("zenoh")
    if not isinstance(zenoh, dict):
        return None
    listen = zenoh.get("listen")
    connect = zenoh.get("connect")
    if not isinstance(listen, list) or not isinstance(connect, list):
        return None
    if listen or connect:
        return None
    return {
        "code": "ZENOH_ENDPOINTS_UNSET",
        "message": (
            "daemon has no explicit Zenoh listen/connect endpoints and "
            "auto-discovery is disabled; this node is isolated from every other "
            "node until endpoints are configured"
        ),
    }


def _bootstrap_daemon_warning(result: JsonObject) -> JsonObject | None:
    """Say so when the daemon init found is a bootstrap daemon that cannot serve."""

    if result.get("phase") != "bootstrap":
        return None
    return {
        "code": "DAEMON_BOOTSTRAP_NO_ACCOUNT",
        "message": (
            "a daemon is already running in bootstrap with no account; it cannot "
            "serve this home until you run `hyprial login`"
        ),
    }


def _initialize_org_context() -> JsonObject | None:
    """Create the phase-1 local slots and report an unadopted node loudly."""
    services = get_services()

    from hyprial.daemon import OrgContextStore

    store = OrgContextStore(services._hyprial_home())
    store.ensure_layout()
    if store.load_accepted() is not None:
        return None
    return {"code": "ORG_CONTEXT_ABSENT", "message": "org-context absent"}


def _org_init_warning(warning: JsonObject | None) -> JsonObject | None:
    """Ask neighbors once for an empty slot and summarize staged candidates."""
    services = get_services()

    if warning is None:
        return None
    try:
        result = services.runtime.request(
            "org.fetch",
            {"requestMode": "neighbors", "timeoutSeconds": 2.0},
            restore_wait=0.0,
        )
    # PR #332 F4②: transient daemon-request failures are their own class
    # now; this best-effort branch keeps swallowing them alongside CliError.
    except (services.CliError, ipc_errors.TransientDaemonError):
        # The org slot remains correctly absent even if the daemon is still
        # starting or the best-effort mesh query itself is unavailable.
        return warning
    count = result.get("receivedCount") if isinstance(result, dict) else None
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return {
            "code": "ORG_CONTEXT_CANDIDATES_RECEIVED",
            "message": f"收到 {count} 个候选,hyprial org context status 查看",
            "candidateCount": count,
        }
    return warning


def _refuse_malformed_settings() -> None:
    """Raise on an unreadable settings.json: a daemon must not be started over it."""
    from hyprial.daemon import node_owner_or_none

    node_owner_or_none()


def _add_login_next_step(result: JsonObject) -> None:
    if result.get("phase") == "bootstrap":
        result["nextSteps"] = ["hyprial login"]


def _complete_initialization(
    *,
    ready_timeout: float,
    listen: str | None,
    connect: str | None,
    org_warning: JsonObject | None,
    autoinstall: bool = True,
) -> JsonObject:
    """Ensure the daemon after home creation, without command recursion.

    A home with no account gets the bootstrap daemon and the next step
    ``hyprial login``; init never signs in.  ``autoinstall`` is the launch
    primitive's switch: ``hyprial init`` leaves it on.
    """
    services = get_services()

    def already_running(status: Any) -> JsonObject:
        services = get_services()
        if not isinstance(status, dict):
            raise services.CliError("INVALID_RESPONSE", "daemon ps result must be an object")
        if listen is not None or connect is not None:
            raise services.CliError(
                "DAEMON_ALREADY_RUNNING",
                "stop the running daemon before changing zenoh endpoints",
            )
        existing: JsonObject = DaemonLaunchResult.existing(status).to_payload()
        _append_warning(existing, _bootstrap_daemon_warning(existing))
        _append_warning(existing, _org_init_warning(org_warning))
        _append_warning(existing, _zenoh_endpoint_warning(status))
        _add_login_next_step(existing)
        return existing

    try:
        status = services._daemon_probe(timeout=0.5)
    except ipc_errors.DaemonUnavailableError:
        pass
    else:
        return already_running(status)

    if not os.environ.get("HYPRIAL_OWNER", "").strip():
        _refuse_malformed_settings()

    launched = services._launch_daemon_process(
        ready_timeout=ready_timeout,
        listen=listen,
        connect=connect,
        autoinstall=autoinstall,
    )
    if launched.already_running:
        return already_running(launched.status)
    result = launched.to_payload()
    _append_warning(result, _org_init_warning(org_warning))
    _append_warning(result, _zenoh_endpoint_warning(result))
    _add_login_next_step(result)
    return result


@init_app.callback(invoke_without_command=True)
def init(
    ctx: typer.Context,
    listen: str | None = typer.Option(
        None,
        "--listen",
        help=(
            "Zenoh endpoint(s) to listen on, comma-separated (persisted in "
            "desired state; pass empty string to clear)."
        ),
    ),
    connect: str | None = typer.Option(
        None,
        "--connect",
        help=(
            "Zenoh endpoint(s) to connect to, comma-separated (persisted in "
            "desired state; pass empty string to clear)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    service: bool | None = typer.Option(
        None,
        "--service/--no-service",
        help=(
            "Install the per-user launchd/systemd service. Without either flag, "
            "interactive init offers once and non-interactive init leaves it unchanged."
        ),
    ),
    ready_timeout: float = typer.Option(
        # ① is a constant boundary -- socket bound, accept running, ping
        # answerable -- but reaching it crosses named startup phases.  This is
        # the allowed silence between phase observations; an advancing child
        # may use the separate hard cap. Restore remains outside this path.
        DAEMON_START_IDLE_BUDGET_SECONDS,
        "--ready-timeout",
        envvar=_INIT_READY_TIMEOUT_ENV,
        help=(
            "Seconds allowed without startup phase progress while the daemon "
            "reaches its serving boundary (socket bound, ping answering) "
            "before failing the start "
            "(env: HYPRIAL_INIT_READY_TIMEOUT). Restore runs off this path and "
            "reports through ping's phase. Continuously advancing startup is "
            "bounded by a separate two-minute hard cap."
        ),
    ),
) -> None:
    """Initialize the home and start the daemon; never sign in.

    A home with an account starts its serving daemon.  A home with no account
    starts the bootstrap daemon and the result names the next step,
    ``hyprial login``, which runs the first login through that daemon.
    """
    if ctx.invoked_subcommand is not None:
        return
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.shell.impl.cli.commands.daemon.service import (
            ServicePlatformUnsupported,
            _install_status,
            _run_service,
            _service_platform,
        )

        try:
            _service_platform()
        except ServicePlatformUnsupported as error:
            unsupported: ServicePlatformUnsupported | None = error
        else:
            unsupported = None
        if service is True and unsupported is not None:
            # Asked for explicitly: refuse before touching the home rather
            # than report a half-done init.
            raise services.CliError(
                "SERVICE_PLATFORM_UNSUPPORTED",
                f"{unsupported}; rerun without --service and start the daemon "
                "with `hyprial daemon start`",
                {"platform": unsupported.platform},
            )
        services.initialize_hyprial_home()
        org_warning = services._initialize_org_context()
        result = services._complete_initialization(
            ready_timeout=ready_timeout,
            listen=listen,
            connect=connect,
            org_warning=org_warning,
        )
        # Autostart is the default, not an opt-in.  A daemon that is installed
        # but not supervised comes back only if a human starts it, which is
        # exactly the failure this call removes -- and a fresh install has no
        # other way to learn it.  ``--no-service`` stays available for
        # containers and CI, where a supervisor is someone else's business.
        requested = True if service is None else service
        if requested and unsupported is not None:
            # Default registration on a platform without a supported per-user
            # supervisor (Windows in 0.5.5.3.4): a skip, not a failure.
            result["service"] = {
                "ok": True,
                "skipped": True,
                "reason": "service-platform-unsupported",
                "platform": unsupported.platform,
                "hint": (
                    "no autostart is registered on this platform: after a "
                    "reboot or logout start the daemon with `hyprial daemon start`"
                ),
            }
        elif requested:
            try:
                result["service"] = _run_service(_install_status)
            except services.CliError as error:
                # The daemon is already serving (that part of init succeeded),
                # so a supervisor we cannot install must not fail the whole
                # init: report it where the reader can act on it.
                result["service"] = {
                    "ok": False,
                    "reason": "service-install-failed",
                    "detail": str(error),
                }
        if not json_output:
            if result.get("nextSteps"):
                warn("hyprial: no account yet; next run `hyprial login`", json_output=False)
        return result

    services.runtime.execute(operation, json_output=json_output, allow_missing_home=True, ensure_daemon=False)
