"""The ``hyprial init`` home/identity/daemon initialization command."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # deferred annotations only
    from hyprial.identity import IdentityTransactionLock

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Callable
from hyprial.kernel import DaemonLaunchResult
from hyprial.kernel import ipc_errors
import os
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.output import confirm
from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS, JsonObject, _INIT_READY_TIMEOUT_ENV, _announce_setup_guidance, _append_warning, _squire_setup_warning


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
        result = services._daemon_request(
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


def _complete_initialization(
    *,
    ready_timeout: float,
    listen: str | None,
    connect: str | None,
    org_warning: JsonObject | None,
    login_when_missing: Callable[[], JsonObject] | None,
    held_identity_transaction: list[IdentityTransactionLock] | None = None,
) -> JsonObject:
    """Ensure owner and daemon after home creation, without command recursion."""
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
        _append_warning(existing, _org_init_warning(org_warning))
        _append_warning(existing, _zenoh_endpoint_warning(status))
        _append_warning(existing, _squire_setup_warning())
        return existing

    try:
        status = services._daemon_probe(timeout=0.5)
    except ipc_errors.DaemonUnavailableError:
        pass
    else:
        return already_running(status)

    login_result: JsonObject | None = None
    owner_override = os.environ.get("HYPRIAL_OWNER", "").strip()
    if not owner_override:
        from hyprial.daemon import node_owner_or_none, resolve_node_owner

        if node_owner_or_none() is None and login_when_missing is not None:
            try:
                login_result = login_when_missing()
            except KeyboardInterrupt as error:
                raise services.CliError(
                    "INTERRUPTED",
                    "login was interrupted; run `hyprial login` or rerun `hyprial init`",
                    data={"nextSteps": ["hyprial login", "hyprial init"]},
                ) from error
            except services.CliError as error:
                data = dict(error.data) if isinstance(error.data, dict) else {}
                data["nextSteps"] = ["hyprial login", "hyprial init"]
                raise services.CliError(
                    error.code,
                    f"{error}; run `hyprial login` or rerun `hyprial init`",
                    data=data,
                ) from error

        # The resolver is the daemon's own authority. For a login entry this
        # verifies the identity stage committed before the init half can launch;
        # for init it also keeps malformed settings loud rather than treating
        # them as an absent owner. An environment override stays on init's
        # legacy fast path and is validated by the daemon itself.
        resolve_node_owner()

    launched = services._launch_daemon_process(
        ready_timeout=ready_timeout,
        listen=listen,
        connect=connect,
        identity_transaction=(
            held_identity_transaction[0] if held_identity_transaction else None
        ),
    )
    if launched.already_running:
        # The launcher's own optimistic probe won the race; re-wrap its
        # answer through the same already-running branch as the probe
        # above, warnings included.
        return already_running(launched.status)
    result = launched.to_payload()
    _append_warning(result, _org_init_warning(org_warning))
    _append_warning(result, _zenoh_endpoint_warning(result))
    _append_warning(result, _squire_setup_warning())
    if login_result is not None:
        for key in ("identity", "device", "verificationUri", "userCode"):
            if key in login_result:
                result[key] = login_result[key]
        device = login_result.get("device")
        if isinstance(device, dict) and device.get("ready") is False:
            _append_warning(
                result,
                {
                    "code": "DEVICE_KEY_NOT_READY",
                    "message": (
                        "login identity was kept and the daemon started; "
                        "install the hyprial-tailcat sidecar and rerun "
                        "`hyprial login` to provision the device key"
                    ),
                    "data": device,
                },
            )
    return result


@app.command()
def init(
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
    """Initialize home and daemon, establishing identity first when owner is absent.

    Home creation, owner establishment, and daemon startup are one in-process
    onboarding flow.  Since the tailnet cutover the only identity source is
    the Hyprial service (the profile issuer): a home with no owner runs the
    existing login path unchanged — no identity-source choice, no self-hosted
    tailnet branch.  An existing owner still takes the pre-onboarding path
    unchanged; no login implementation is copied or launched through a shell.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        services.initialize_hyprial_home()
        org_warning = services._initialize_org_context()
        held_identity_transaction: list[IdentityTransactionLock] = []

        def login_when_missing() -> JsonObject:
            services = get_services()
            # No identity-source choice since the tailnet cutover: an
            # ownerless home goes straight to the service login
            # (the profile issuer).
            return services._run_login_cli_flow(
                no_open=False,
                json_output=json_output,
                switch_account=False,
                no_daemon=True,
                first_time_setup=True,
                held_identity_transaction=held_identity_transaction,
            )

        try:
            result = services._complete_initialization(
                ready_timeout=ready_timeout,
                listen=listen,
                connect=connect,
                org_warning=org_warning,
                login_when_missing=login_when_missing,
                held_identity_transaction=held_identity_transaction,
            )
        finally:
            for transaction in held_identity_transaction:
                transaction.close()
        requested = service
        if requested is None and not json_output and services._stdin_isatty():
            requested = confirm("Install the Hyprial daemon as a per-user service?")
        if requested:
            from hyprial.shell.impl.cli.commands.daemon.service import (
                _install_status,
                _run_service,
            )

            result["service"] = _run_service(_install_status)
        if not json_output:
            _announce_setup_guidance(result)
        return result

    services._execute(operation, json_output=json_output, allow_missing_home=True)
