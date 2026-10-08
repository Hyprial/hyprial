"""``hyprial init`` and ``hyprial onboarding`` first-run setup."""

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
from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS, JsonObject, _INIT_READY_TIMEOUT_ENV, _announce_setup_guidance, _append_warning, _squire_setup_warning
onboarding_app = typer.Typer(
    help="Plan first-run setup from the machine's current read-only state."
)


FIRST_RUN_ROUTE_NAME = "squire"


def _onboarding_scope_authorized(name: str) -> bool:
    """Read the same tenant-scope evidence as ``adapter doctor --json``."""
    services = get_services()

    from hyprial.daemon import configured_scope_client, diagnose_scopes

    app_id, client = configured_scope_client(services._hyprial_home(), services._state_dir(), name)
    result = diagnose_scopes(adapter=name, app_id=app_id, response=client.list_scopes())
    return result.get("ok") is True


def _onboarding_snapshot() -> dict[str, object]:
    """Collect machine state through existing read-only CLI surfaces."""
    services = get_services()

    from hyprial.shell.impl.onboarding.plan import OnboardingStateReader

    return OnboardingStateReader(
        hyprial_home=services._hyprial_home(),
        state_dir=services._state_dir(),
        daemon_request=services._daemon_request,
        scope_authorized=_onboarding_scope_authorized,
    ).read()


@onboarding_app.command("plan")
def onboarding_plan(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Report the remaining ordered first-run steps without changing state."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.shell.impl.onboarding.plan import plan_first_run

        return {"ok": True, **plan_first_run(services._onboarding_snapshot())}

    services._execute(operation, json_output=json_output, allow_missing_home=True)


def _onboarding_apply_actions() -> dict[str, Callable[[], object]]:
    """The steps ``onboarding apply`` can run without a person, and their primitives.

    Each entry is the same primitive the operator would reach for by hand, so a
    manual run and the automatic path cannot drift into two behaviours.  Steps
    without an entry here are refused by name (contract section 11) instead of
    being silently skipped.
    """
    services = get_services()

    def create_agent(name: str) -> Callable[[], object]:
        # The actor names are part of the contract: onboarding's reader recognizes
        # exactly these to answer "which role already exists" (see _agent_role in
        # hyprial.onboarding).
        services = get_services()
        return lambda: services._daemon_request("agent.create", {"name": name})

    def squire() -> object:
        # The command's own body, with the derived defaults: one implementation,
        # two callers.
        return services._squire_setup_operation()

    actions: dict[str, Callable[[], object]] = {
        "default-agent": create_agent("default"),
        "worker-agent": create_agent("worker"),
        "squire": squire,
    }
    # A pin is one adapter bound to one agent, and an agent can carry at most one
    # pin, so the step only has one answer while the machine has exactly one
    # adapter configured.  With several, binding an arbitrary channel would be the
    # machine picking a message route for the user: leave the action out, which is
    # what makes `--auto` skip the step and a single-step apply report
    # MANUAL_ACTION_REQUIRED instead of guessing.
    names = _configured_adapter_names()
    if len(names) == 1:
        adapter = names[0]
        actions["lark-pin"] = lambda: services._daemon_request(
            "adapter.pin", {"name": adapter, "actor": "squire"}
        )
        # The outbound route points at the chat the user already talked to this
        # bot in, so the machine reads that id from its own inbound observations
        # instead of asking the user to copy one.  A single observed chat is a
        # single answer; none means nobody has messaged the bot yet, and several
        # mean picking the conversation would be the machine's decision rather
        # than the user's.  Those two leave the action out, which is what makes
        # `--auto` skip the step and a single-step apply report
        # MANUAL_ACTION_REQUIRED (contract sections 10/11).
        candidates = services._route_candidates(adapter)
        if len(candidates) == 1:
            chat_id = str(candidates[0]["chatId"])

            def bind_first_route(
                adapter_name: str = adapter, native_id: str = chat_id
            ) -> object:
                services = get_services()
                from hyprial.daemon import (
                    RouteInput,
                    add_gateway_route,
                )

                return services._route_operation(
                    lambda: add_gateway_route(
                        hyprial_home=services._hyprial_home(),
                        name=adapter_name,
                        route=RouteInput(
                            name=FIRST_RUN_ROUTE_NAME, native_id=native_id
                        ),
                        make_default=True,
                    )
                )

            actions["lark-route"] = bind_first_route
    return actions


def _configured_adapter_names() -> list[str]:
    """Configured adapters, read through the same surface the plan uses.

    An unreadable ``channels.json`` is absence, not failure: onboarding must still
    be able to finish the steps that have nothing to do with messaging.
    """
    services = get_services()

    from hyprial.daemon import list_gateway_routes

    try:
        payload = list_gateway_routes(hyprial_home=services._hyprial_home())
        adapters = payload.get("adapters")
    except Exception:  # noqa: BLE001 - a malformed local config is empty state
        return []
    if not isinstance(adapters, list):
        return []
    names: list[str] = []
    for adapter in adapters:
        if isinstance(adapter, dict):
            name = adapter.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return sorted(names)


@onboarding_app.command("apply")
def onboarding_apply(
    step_id: str | None = typer.Argument(
        None, help="Onboarding step to apply; omit when using --auto."
    ),
    auto: bool = typer.Option(
        False, "--auto", help="Apply every ready, non-interactive step, in plan order."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Run the first-run steps that need no person (desktop contract section 11).

    Writes go through the same primitives the operator would run by hand; success
    is re-read from the machine rather than recorded locally, so re-running is
    always safe.  ``--auto`` is gated by ``settings.json`` ``autoOnboarding``
    (default on); a single named step is an explicit action and ignores the
    switch.  Interactive steps refuse here rather than half-running: the desktop
    shell carries the human part.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.shell.impl.onboarding.apply import (
            OnboardingApplier,
            OnboardingApplyError,
            auto_onboarding_enabled,
        )

        if auto and step_id:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "pass either <step-id> or --auto, not both",
            )
        if not auto and not step_id:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "onboarding apply needs a <step-id> or --auto",
            )
        applier = OnboardingApplier(
            snapshot=services._onboarding_snapshot,
            actions=services._onboarding_apply_actions(),
            auto_enabled=lambda: auto_onboarding_enabled(services._hyprial_home()),
        )
        try:
            if auto:
                return applier.apply_auto()
            return applier.apply_step(step_id or "")
        except OnboardingApplyError as error:
            raise services.CliError(error.code, str(error), error.details or None) from error

    services._execute(operation, json_output=json_output, allow_missing_home=True)


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
            "message": f"收到 {count} 个候选,hyprial org status 查看",
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
        if not json_output:
            _announce_setup_guidance(result)
        return result

    services._execute(operation, json_output=json_output, allow_missing_home=True)
