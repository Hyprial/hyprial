"""The ``hyprial user-proxy`` personal delivery-channel commands."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import LIFECYCLE_IPC_MARGIN_SECONDS, LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS
from hyprial.kernel import ipc_errors
import json
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject, _resolved_agent_cwd, _user_proxy_launch_params
from hyprial.shell.impl.cli.commands.agent.runtime.tiers import _delivery_agent_live
user_proxy_app = typer.Typer(
    help="Configure the personal user-proxy delivery channel."
)


def _stored_user_proxy_route(name: str) -> str | None:
    services = get_services()
    from hyprial.daemon import DesiredStateStore

    state = DesiredStateStore(services._state_dir() / "desired-state.json").load()
    spec = next(
        (
            candidate
            for candidate in state.harnesses
            if candidate.harness == "user-proxy" and candidate.name == name
        ),
        None,
    )
    if spec is None:
        return None
    try:
        index = spec.command.index("--route")
    except ValueError:
        return None
    return spec.command[index + 1] if index + 1 < len(spec.command) else None


@user_proxy_app.command("status")
def user_proxy_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read-only: is this person's user-proxy configured and live (no model calls)."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import resolve_node_owner
        from hyprial.identity import UserProfileError, UserProfileStore

        store = UserProfileStore(services._state_dir() / "users.json")
        try:
            profile = store.resolve(resolve_node_owner())
        except UserProfileError as error:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        agent = profile.delivery_agent if profile is not None else None
        return {
            "ok": True,
            "configured": agent is not None,
            "deliveryAgent": agent,
            # Only a configured agent costs one daemon ps round trip.
            "live": _delivery_agent_live(agent) if agent is not None else False,
        }

    services._execute(operation, json_output=json_output)


@user_proxy_app.command("setup")
def user_proxy_setup(
    adapter: str = typer.Option(..., "--adapter", help="Dedicated Lark adapter name."),
    route: str = typer.Option(..., "--route", help="Route name on that adapter."),
    name: str | None = typer.Option(
        None, "--name", help="Local agent name; default: <owner-key>-proxy."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Idempotently pin and run this person's dedicated user-proxy."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.identity import AgentError, AgentRegistry
        from hyprial.daemon import resolve_node_owner
        from hyprial.kernel import (
            ChannelConfiguration,
            PersistentConfigError,
        )
        from hyprial.identity import UserProfileError, UserProfileStore
        from hyprial.kernel import agent_uri_actor, parse_channel_uri, parse_route_uri

        store = UserProfileStore(services._state_dir() / "users.json")
        try:
            profile = store.resolve(resolve_node_owner())
        except UserProfileError as error:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        if profile is None:
            raise services.CliError(
                "NOT_FOUND",
                "no local user profile; run hyprial squire setup first",
            )

        try:
            proxy_name = AgentRegistry.normalize_actor(
                name if name is not None else f"{profile.owner_key}-proxy"
            )
        except AgentError as error:
            raise services.CliError(error.code, str(error)) from error
        route_uri = f"route:{adapter}:{route}"
        if parse_route_uri(route_uri) is None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--adapter and --route must form route:<adapter>:<route>",
            )

        channels_path = services._hyprial_home() / "channels.json"
        try:
            channels = ChannelConfiguration.from_json(
                json.loads(channels_path.read_text(encoding="utf-8"))
            )
        except FileNotFoundError as error:
            raise services.CliError(
                ipc_errors.ADAPTER_NOT_FOUND,
                f"adapter is not configured: {adapter}; {channels_path} is missing",
            ) from error
        except (OSError, UnicodeError, json.JSONDecodeError, PersistentConfigError) as error:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        gateway = next(
            (candidate for candidate in channels.gateways if candidate.name == adapter),
            None,
        )
        if gateway is None:
            available = ", ".join(item.name for item in channels.gateways) or "none"
            raise services.CliError(
                ipc_errors.ADAPTER_NOT_FOUND,
                f"adapter is not configured: {adapter}; available adapters: {available}",
            )
        if route not in {candidate.name for candidate in gateway.routes}:
            available = ", ".join(candidate.name for candidate in gateway.routes) or "none"
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"route {route!r} is not configured on adapter {adapter!r}; "
                f"available routes: {available}",
            )

        squire_adapter = profile.squire_adapter
        parsed_squire = (
            parse_channel_uri(squire_adapter)
            if squire_adapter is not None
            else None
        )
        squire_name = (
            parsed_squire[2]
            if parsed_squire is not None
            else squire_adapter
        )
        if squire_name == adapter:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"adapter {adapter!r} is the profile's Squire adapter; "
                "user-proxy requires its own dedicated Lark adapter",
            )

        status = services._daemon_request("ps", {})
        daemon = status.get("daemon")
        node_id = daemon.get("nodeId") if isinstance(daemon, dict) else None
        if node_id != profile.preferred_receiver.machine:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "user-proxy setup must run on the profile's preferred-receiver "
                f"node {profile.preferred_receiver.machine!r}, not {node_id!r}",
            )
        pins_result = services._daemon_request("adapter.pins", {})
        raw_pins = pins_result.get("pins")
        pins = raw_pins if isinstance(raw_pins, dict) else {}
        pinned = pins.get(adapter)
        if isinstance(pinned, str) and agent_uri_actor(pinned) != proxy_name:
            raise services.CliError(
                "PIN_CONFLICT",
                f"adapter {adapter!r} is already pinned to a different agent: "
                f"{pinned}; run 'hyprial adapter unpin {adapter}' first",
            )

        raw_agents = status.get("agents")
        agents = raw_agents if isinstance(raw_agents, list) else []
        agent_row = next(
            (
                candidate
                for candidate in agents
                if isinstance(candidate, dict)
                and (
                    candidate.get("actor") == proxy_name
                    or (
                        isinstance(candidate.get("uri"), str)
                        and agent_uri_actor(candidate["uri"]) == proxy_name
                    )
                )
            ),
            None,
        )
        raw_connectors = status.get("connectors")
        connectors = raw_connectors if isinstance(raw_connectors, list) else []
        live_connector = next(
            (
                candidate
                for candidate in connectors
                if isinstance(candidate, dict)
                and candidate.get("name") == proxy_name
                and candidate.get("running") is True
            ),
            None,
        )
        if live_connector is not None and live_connector.get("runtime") != "user-proxy":
            raise services.CliError(
                "AGENT_ALREADY_RUNNING",
                f"agent {proxy_name!r} is already running on "
                f"{live_connector.get('runtime')!r}; choose another --name",
            )
        proxy_live = (
            live_connector is not None
            and live_connector.get("runtime") == "user-proxy"
        )
        if proxy_live:
            stored_route = services._stored_user_proxy_route(proxy_name)
            if stored_route != route_uri:
                raise services.CliError(
                    "AGENT_ALREADY_RUNNING",
                    f"user-proxy {proxy_name!r} is running with route "
                    f"{stored_route!r}, not {route_uri!r}; run "
                    f"'hyprial down user-proxy {proxy_name}' before changing it",
                )

        changed: list[str] = []
        worker: JsonObject = {"started": False, "live": proxy_live}
        target_uri = (
            agent_row.get("uri")
            if isinstance(agent_row, dict) and isinstance(agent_row.get("uri"), str)
            else None
        )
        if not proxy_live:
            cwd = _resolved_agent_cwd(proxy_name, None)
            created = services._create_agent_for_start(
                name=proxy_name,
                harness="user-proxy",
                runtime="headless",
                cwd=cwd,
            )
            raw_agent = created.get("agent")
            if not isinstance(raw_agent, dict) or not isinstance(
                raw_agent.get("uri"), str
            ):
                raise services.CliError(
                    ipc_errors.INVALID_RESPONSE,
                    "agent.create did not return the user-proxy URI",
                )
            target_uri = raw_agent["uri"]
            if created.get("created") is True:
                changed.append("agent")
        if not isinstance(target_uri, str):
            raise services.CliError(
                ipc_errors.INVALID_RESPONSE,
                "live user-proxy has no registered agent URI",
            )
        if pinned != target_uri:
            pin_result = services._daemon_request(
                "adapter.pin", {"name": adapter, "actor": target_uri}
            )
            if pin_result.get("changed") is True:
                changed.append("adapter.pin")
        if not proxy_live:
            worker = services._daemon_request(
                "lifecycle.start",
                _user_proxy_launch_params(proxy_name, route_uri),
                timeout=(
                    LIFECYCLE_OPERATION_DEADLINE_SECONDS
                    + LIFECYCLE_WAIT_MARGIN_SECONDS
                    + LIFECYCLE_IPC_MARGIN_SECONDS
                ),
            )
            worker = {**worker, "started": True, "live": True}
            changed.append("worker")

        try:
            profile, profile_changed = store.set_delivery_agent(
                profile.owner_key, proxy_name
            )
        except UserProfileError as error:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        changed.extend(profile_changed)
        return {
            "ok": True,
            "setup": "user-proxy",
            "schemaVersion": 1,
            "changed": changed,
            "name": proxy_name,
            "actor": target_uri,
            "adapter": adapter,
            "route": route_uri,
            "deliveryAgent": profile.delivery_agent,
            "deliveryAgentLive": True,
            "worker": worker,
            "profile": profile.to_json(),
        }

    services._execute(operation, json_output=json_output)
