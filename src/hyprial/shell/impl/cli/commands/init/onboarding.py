"""The ``hyprial onboarding`` first-run planning and apply group."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from collections.abc import Callable
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
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
