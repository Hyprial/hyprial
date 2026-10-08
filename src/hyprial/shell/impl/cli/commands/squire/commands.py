"""``hyprial squire`` and ``hyprial user-proxy`` setup."""

from __future__ import annotations

from collections.abc import Mapping

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, render_generic

from hyprial.kernel import LIFECYCLE_IPC_MARGIN_SECONDS, LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS
from pathlib import Path
from hyprial.kernel import ipc_errors
import json
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject, _resolved_agent_cwd, _user_proxy_launch_params
squire_app = typer.Typer(help="Configure and inspect the personal Squire agent.")


user_proxy_app = typer.Typer(
    help="Configure the personal user-proxy delivery channel."
)


def _identity_rows(
    method: str, params: JsonObject, *, key: str
) -> list[dict[str, object]]:
    result = get_services()._daemon_request(method, params)
    rows = result.get(key) if isinstance(result, Mapping) else None
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise get_services().CliError(
            ipc_errors.INVALID_RESPONSE,
            f"{method} result must contain object rows under {key}",
        )
    return [dict(row) for row in rows]


def _owner_member_key(owner: str) -> str:
    members = _identity_rows(
        "identity.users.list", {"kind": "member"}, key="users"
    )
    matches = [row for row in members if row.get("owner") == owner]
    if not matches:
        raise get_services().CliError(
            ipc_errors.INVALID_ARGUMENT,
            "no member record for the node owner; "
            f"run `hyprial user add --kind member --owner {owner} "
            "--confirmed-by <you>` first",
        )
    if len(matches) != 1:
        raise get_services().CliError(
            ipc_errors.INVALID_RESPONSE,
            f"identity.users.list returned multiple member records for owner {owner!r}",
        )
    user_key = matches[0].get("userKey", matches[0].get("user"))
    if not isinstance(user_key, str) or not user_key:
        raise get_services().CliError(
            ipc_errors.INVALID_RESPONSE,
            "identity.users.list member record has no user key",
        )
    return user_key


def _binding_accounts(
    rows: list[dict[str, object]], *, user_key: str, adapter: str | None = None
) -> set[tuple[str, str]]:
    accounts: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("user") != user_key or row.get("source") != "local-override":
            continue
        candidates = row.get("accounts")
        if not isinstance(candidates, list):
            continue
        for account in candidates:
            if not isinstance(account, Mapping):
                continue
            account_adapter = account.get("adapter")
            open_id = account.get("openId")
            if (
                isinstance(account_adapter, str)
                and isinstance(open_id, str)
                and (adapter is None or account_adapter == adapter)
            ):
                accounts.add((account_adapter, open_id))
    return accounts


def _stale_outbound_accounts(
    rows: list[dict[str, object]], *, user_key: str, current_adapter: str
) -> list[dict[str, str]]:
    # Review 1037: the daemon's account-only row also lists accounts that sit on
    # a union-keyed override; unbinding those would delete that override, so an
    # account seen on any union-keyed or non-account-only row is never stale.
    protected: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("unionId") is None and row.get("outboundOnly") is True:
            continue
        candidates = row.get("accounts")
        if isinstance(candidates, list):
            for account in candidates:
                if isinstance(account, Mapping):
                    adapter = account.get("adapter")
                    open_id = account.get("openId")
                    if isinstance(adapter, str) and isinstance(open_id, str):
                        protected.add((adapter, open_id))
    accounts: set[tuple[str, str]] = set()
    for row in rows:
        if (
            row.get("user") != user_key
            or row.get("source") != "local-override"
            or row.get("outboundOnly") is not True
            or row.get("unionId") is not None
        ):
            continue
        candidates = row.get("accounts")
        if not isinstance(candidates, list):
            continue
        for account in candidates:
            if not isinstance(account, Mapping):
                continue
            adapter = account.get("adapter")
            open_id = account.get("openId")
            if (
                isinstance(adapter, str)
                and isinstance(open_id, str)
                and adapter != current_adapter
                and (adapter, open_id) not in protected
            ):
                accounts.add((adapter, open_id))
    return [
        {
            "adapter": adapter,
            "openId": open_id,
            "suggestedCommand": (
                f"hyprial user unbind --adapter {adapter} --open-id {open_id}"
            ),
        }
        for adapter, open_id in sorted(accounts)
    ]


def _render_squire_setup(data: Mapping[str, object]) -> str:
    visible = dict(data)
    raw_stale = visible.pop("staleOutboundAccounts", [])
    lines = [render_generic(visible)]
    if not isinstance(raw_stale, list):
        return lines[0]
    for row in raw_stale:
        if not isinstance(row, Mapping):
            continue
        adapter = row.get("adapter")
        open_id = row.get("openId")
        command = row.get("suggestedCommand")
        if not all(isinstance(value, str) for value in (adapter, open_id, command)):
            continue
        lines.append(
            f"previous outbound-only account {adapter} {open_id} is still bound; "
            f"remove it with `{command}` if it is no longer valid"
        )
    return "\n".join(lines)


def _squire_setup_operation(
    *,
    owner_key: str | None = None,
    login_name: str | None = None,
    machine: str | None = None,
    machine_key: str | None = None,
    channel: str | None = None,
    owner_open_id: str | None = None,
    binding_code: str | None = None,
    home: Path | None = None,
    display_name: str | None = None,
    adapter: str | None = None,
    dm_route: str = "owner",
    provider: str = "deepseek",
    model: str = "deepseek-flash",
    preferred_harness: str = "pi",
    start_worker: bool = False,
    step: str | None = None,
) -> JsonObject:
    """The work behind ``squire setup``, shared with the first-run apply path.

    One implementation, two callers: the operator's command (with its flags) and
    ``onboarding apply squire`` (with the derived defaults). The first-run path
    must not grow a second copy of the setup rules, so the command delegatees
    here rather than owning the body.
    """
    services = get_services()

    from hyprial.daemon import resolve_node_owner
    from hyprial.daemon import DaemonOwnershipBusy
    from hyprial.daemon import (
        EnsureSquireRegistryCommand,
        ManagementError,
        OfflineManagementLease,
        SquireRegistryResult,
    )
    from hyprial.daemon import SquireSetup, derive_setup_identity

    # The owner segment is the user identity of this home (design §3.3);
    # a missing one raises the resolver's guidance (naming hyprial login).
    owner = resolve_node_owner()
    # The four host-side lookup keys are derived unless explicitly
    # overridden (Allen, 2026-09-18): machine from HYPRIAL_NODE_ID else
    # hostname, owner_key/machine_key as slugs, login_name from the
    # platform login. An explicit --machine that disagrees with a
    # configured node id still fails inside SquireSetup's cross-checks.
    identity = derive_setup_identity(
        owner,
        owner_key=owner_key,
        login_name=login_name,
        machine=machine,
        machine_key=machine_key,
    )

    class CliSquireManagement:
        @staticmethod
        def ensure_squire(
            command: EnsureSquireRegistryCommand,
        ) -> SquireRegistryResult:
            services = get_services()
            try:
                response = services._daemon_request(
                    "management.squire.ensure", command.to_payload()
                )
            except ipc_errors.DaemonUnavailableError:
                if command.start:
                    raise
                try:
                    with OfflineManagementLease(
                        services._state_dir(),
                        owner=command.owner,
                        machine=command.machine,
                        hyprial_home=services._hyprial_home(),
                    ) as management:
                        return management.ensure_squire(command)
                except DaemonOwnershipBusy as busy:
                    raise services.CliError(busy.code, str(busy)) from busy
                except ManagementError as managed_error:
                    raise services.CliError(
                        managed_error.code, str(managed_error)
                    ) from managed_error
            if not isinstance(response, dict):
                raise services.CliError(
                    ipc_errors.INVALID_RESPONSE,
                    "management.squire.ensure result must be an object",
                )
            return SquireRegistryResult.from_payload(response)

    setup = SquireSetup(
        hyprial_home=services._hyprial_home(),
        state_dir=services._state_dir(),
        management_port=CliSquireManagement(),
    )
    try:
        override_requested = owner_open_id is not None and step in {
            None,
            "owner-open-id",
        }
        if owner_open_id is not None and channel is None:
            raise ValueError("--owner-open-id requires --channel")
        if binding_code is not None and owner_open_id is None:
            raise ValueError("--binding-code requires --owner-open-id")
        resolved_user_key: str | None = None
        parsed_channel: tuple[str, str, str] | None = None
        binding_already_exists = False
        stale_outbound_accounts: list[dict[str, str]] = []
        if override_requested:
            assert owner_open_id is not None and channel is not None
            from hyprial.kernel import parse_channel_uri

            parsed_channel = parse_channel_uri(channel)
            if parsed_channel is None:
                raise ValueError(
                    "--channel must be a channel:<owner>:<machine>:<adapter> URI"
                )
            # The member store generates slug(owner); --owner-key only controls
            # the legacy Squire profile and is never identity authority.
            resolved_user_key = _owner_member_key(identity.owner)
            bindings = _identity_rows(
                "identity.bindings.list", {}, key="bindings"
            )
            current_accounts = _binding_accounts(
                bindings,
                user_key=resolved_user_key,
                adapter=parsed_channel[2],
            )
            binding_already_exists = (
                parsed_channel[2], owner_open_id
            ) in current_accounts
            stale_outbound_accounts = _stale_outbound_accounts(
                bindings,
                user_key=resolved_user_key,
                current_adapter=parsed_channel[2],
            )
            if not binding_already_exists:
                if binding_code is None:
                    raise ValueError(
                        "--binding-code is required unless this owner account "
                        "override already exists"
                    )
                # Spend the authorization before any scaffold/profile mutation.
                setup.setup_state.consume_verified(
                    identity.owner_key, channel, binding_code
                )
                try:
                    services._daemon_request(
                        "identity.override.set",
                        {
                            "adapter": parsed_channel[2],
                            "openId": owner_open_id,
                            "user": resolved_user_key,
                            "confirmedBy": identity.owner,
                        },
                    )
                except services.CliError as error:
                    raise services.CliError(
                        error.code,
                        f"{error}; the binding code is now used; "
                        "rerun step 3 for a new one",
                        error.data,
                    ) from error
                except ipc_errors.TransientDaemonError as error:
                    raise services.CliError(
                        error.code,
                        f"{error}; the binding code is now used; "
                        "rerun step 3 for a new one",
                    ) from error
        result = setup.run(
            identity,
            channel=channel,
            owner_open_id=None,
            binding_code=None,
            squire_home=home,
            display_name=display_name,
            adapter=adapter,
            dm_route=dm_route,
            provider=provider,  # squire harness/provider/model model-vendor field
            model=model,
            preferred_harness=preferred_harness,
            start=start_worker,
            step=step,
        )
        if override_requested:
            assert owner_open_id is not None
            assert parsed_channel is not None and resolved_user_key is not None
            steps = result.get("steps")
            if isinstance(steps, list):
                for candidate in steps:
                    if isinstance(candidate, dict) and candidate.get("id") == "owner-open-id":
                        candidate.clear()
                        candidate.update(
                            {
                                "id": "owner-open-id",
                                "status": "complete",
                                "detail": (
                                    "The owner account is bound through the daemon "
                                    "identity resolver."
                                ),
                            }
                        )
                result["complete"] = all(
                    isinstance(candidate, dict)
                    and candidate.get("status") == "complete"
                    for candidate in steps
                )
            result["staleOutboundAccounts"] = stale_outbound_accounts
        return result
    except ManagementError as error:
        raise services.CliError(error.code, str(error)) from error
    except ValueError as error:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error


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


@squire_app.command("setup")
def squire_setup(
    owner_key: str | None = typer.Option(
        None,
        "--owner-key",
        help="Profile owner key; default: slug of the resolved owner.",
    ),
    login_name: str | None = typer.Option(
        None,
        "--login-name",
        help="Profile login name; default: this account's platform (OS) login.",
    ),
    machine: str | None = typer.Option(
        None,
        "--machine",
        help="Receiver machine id; default: HYPRIAL_NODE_ID, else this host's "
        "name.",
    ),
    machine_key: str | None = typer.Option(
        None,
        "--machine-key",
        help="Receiver machine key; default: slug of the machine id.",
    ),
    channel: str | None = typer.Option(None, "--channel"),
    owner_open_id: str | None = typer.Option(None, "--owner-open-id"),
    binding_code: str | None = typer.Option(None, "--binding-code"),
    home: Path | None = typer.Option(
        None,
        "--home",
        "--cwd",
        help="Squire home (default: ~/squire); --cwd is a compatibility alias.",
    ),
    display_name: str | None = typer.Option(None, "--display-name"),
    adapter: str | None = typer.Option(None, "--adapter"),
    dm_route: str = typer.Option("owner", "--dm-route"),
    provider: str = typer.Option("deepseek", "--provider"),
    model: str = typer.Option("deepseek-flash", "--model"),
    preferred_harness: str = typer.Option("pi", "--preferred-harness"),
    start_worker: bool = typer.Option(False, "--start"),
    step: str | None = typer.Option(None, "--step"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Idempotently configure the local personal Squire.

    The owner is not a flag (U5, design §3.3): it is the home's user
    identity, resolved via ``resolve_node_owner`` — ``HYPRIAL_OWNER`` >
    ``settings.json`` owner (written by ``hyprial login``) — and a home without
    one fails with the resolver's guidance.  ``--owner-key``/
    ``--login-name``/
    ``--machine``/
    ``--machine-key`` remain as optional overrides: they are host-side lookup
    keys for the profile/receiver, not the user identity.  Omitted, they are
    derived — ``machine`` from ``HYPRIAL_NODE_ID`` (else the hostname),
    ``owner_key``/``machine_key`` as slugs of owner/machine, ``login_name``
    from the platform (OS) login — so ``hyprial squire setup --json`` with no
    further flags is the normal first run.
    """
    services = get_services()

    def operation() -> CliResult:
        result = _squire_setup_operation(
            owner_key=owner_key,
            login_name=login_name,
            machine=machine,
            machine_key=machine_key,
            channel=channel,
            owner_open_id=owner_open_id,
            binding_code=binding_code,
            home=home,
            display_name=display_name,
            adapter=adapter,
            dm_route=dm_route,
            provider=provider,
            model=model,
            preferred_harness=preferred_harness,
            start_worker=start_worker,
            step=step,
        )
        return CliResult(result, render=_render_squire_setup)

    services._execute(operation, json_output=json_output)


def _parse_combo(raw: str) -> tuple[str, str | None, str]:
    services = get_services()
    parts = raw.split(":")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"--combo {raw!r} must be harness:provider:model "
            "(provider may be empty, e.g. claude::sonnet)",
        )
    harness, provider, model = parts
    return harness, provider or None, model


def _delivery_agent_live(agent: str | None) -> bool:
    services = get_services()
    if agent is None:
        return False
    try:
        status = services._daemon_request("ps", {})
    except ipc_errors.DaemonUnavailableError:
        return False
    connectors = status.get("connectors")
    if not isinstance(connectors, list):
        return False
    return any(
        isinstance(candidate, dict)
        and candidate.get("runtime") == "user-proxy"
        and candidate.get("name") == agent
        and candidate.get("running") is True
        for candidate in connectors
    )


@squire_app.command("probe")
def squire_probe(
    owner_key: str | None = typer.Option(
        None, "--owner-key", help="Profile owner key; required with multiple users."
    ),
    combo: list[str] = typer.Option(
        [],
        "--combo",
        help="harness:provider:model to declare and probe; repeatable. "
        "Without --combo, every declared combination is re-probed.",
    ),
    timeout: float = typer.Option(180.0, "--timeout", help="Per-probe seconds."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show probe results without writing the profile."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Probe harness/provider/model combinations and record availability."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import (
            RuntimeProber,
            probe_combinations,
        )
        from hyprial.identity import (
            UserProfileError,
            UserProfileStore,
            )

        store = UserProfileStore(services._state_dir() / "users.json")
        key = owner_key
        if key is None:
            profiles = store.list()
            if not profiles:
                raise services.CliError(
                    "NOT_FOUND", "no user profile; run hyprial squire setup first"
                )
            if len(profiles) > 1:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "multiple user profiles; pass --owner-key",
                )
            key = profiles[0].owner_key
        combos = tuple(_parse_combo(raw) for raw in combo)
        if not combos:
            profile = store.get(key)
            declared = () if profile is None else profile.runtime_capabilities
            combos = tuple(
                (capability.harness, capability.provider, capability.model)
                for capability in declared
            )
        if not combos:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "no runtime capability combinations declared; "
                "pass --combo harness:provider:model",
            )
        try:
            prober = RuntimeProber(timeout=timeout)
            results, changed, profile = probe_combinations(
                store, key, combos, prober=prober, dry_run=dry_run
            )
        except UserProfileError as error:
            raise services.CliError("NOT_FOUND", str(error)) from error
        except ValueError as error:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        profile_json = profile.to_json()
        profile_json["deliveryAgentLive"] = _delivery_agent_live(
            profile.delivery_agent
        )
        return {
            "ok": True,
            "probe": "squire-runtime-capability",
            "schemaVersion": 1,
            "ownerKey": key,
            "dryRun": dry_run,
            "changed": list(changed),
            "results": [result.to_json() for result in results],
            "profile": profile_json,
        }

    services._execute(operation, json_output=json_output)
