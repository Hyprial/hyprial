"""``hyprial adapter`` lifecycle commands."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import notice

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from pathlib import Path
from hyprial.kernel import ipc_errors
import json
import os
import sys
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
adapter_app = typer.Typer(help="Manage external-platform adapters.")


def _parse_adapter_routes(raw_routes: list[str]) -> tuple[Any, ...]:
    services = get_services()
    from hyprial.daemon import RouteInput

    if not raw_routes:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "at least one --route name=native_id is required",
        )
    parsed: list[RouteInput] = []
    for item in raw_routes:
        route_name, separator, native_id = item.partition("=")
        if not separator or not route_name or not native_id:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"--route {item!r} must be name=native_id",
            )
        parsed.append(RouteInput(name=route_name, native_id=native_id))
    return tuple(parsed)


def _adapter_secret_from_text(text: str) -> str:
    """The bare app secret from what --secret-file/stdin supplied.

    Onboard writes ``secrets/lark-<name>.json`` as ``{"appSecret": ...}``, and
    feeding that file back to ``adapter add --secret-file`` stored its whole
    JSON text as the secret (allen-channel, 2026-09-26: a 53-char "secret",
    found only later as Lark 10014).  A JSON object is read for its
    ``appSecret``; any other JSON-looking input is refused, never stored.
    The value is never echoed in an error.
    """
    services = get_services()

    if not text.startswith(("{", "[")):
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    value = parsed.get("appSecret") if isinstance(parsed, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise services.CliError(
            "SECRET_FORMAT_INVALID",
            "the secret looks like JSON but is not an object with a non-empty "
            '"appSecret"; pass the bare secret, or a {"appSecret": "..."} file',
        )
    return value.strip()


def _read_adapter_secret(*, secret_file: Path | None, json_output: bool) -> str:
    """Obtain the Lark app secret without ever accepting it on argv.

    Precedence: ``--secret-file`` > piped stdin > interactive hidden prompt.
    A hidden prompt is only used on a TTY without ``--json`` (whose stdout must
    stay a single JSON value); otherwise an explicit source is required.
    """
    services = get_services()

    if secret_file is not None:
        try:
            secret = secret_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            reason = error.strerror or str(error)
            raise services.CliError(
                "SECRET_UNAVAILABLE",
                f"cannot read secret file {secret_file}: {reason}",
            ) from error
        if not secret:
            raise services.CliError("SECRET_REQUIRED", "secret file is empty")
        return _adapter_secret_from_text(secret)
    if not sys.stdin.isatty():
        secret = sys.stdin.read().strip()
        if not secret:
            raise services.CliError("SECRET_REQUIRED", "no app secret provided on stdin")
        return _adapter_secret_from_text(secret)
    if json_output:
        raise services.CliError(
            "SECRET_REQUIRED",
            "provide the app secret via --secret-file or stdin "
            "(an interactive prompt is unavailable with --json)",
        )
    import getpass

    secret = getpass.getpass("Lark app secret (hidden): ").strip()
    if not secret:
        raise services.CliError("SECRET_REQUIRED", "no app secret entered")
    return secret


@adapter_app.command("add")
def adapter_add(
    name: str = typer.Argument(
        ..., help="Gateway name; also names the credential file lark-<name>.json."
    ),
    app_id: str = typer.Option(..., "--app-id", help="Lark app_id."),
    route: list[str] = typer.Option(
        [],
        "--route",
        help="Direct route as name=native_chat_id; repeatable.",
    ),
    default_route: str | None = typer.Option(
        None, "--default-route", help="Route name to use as the gateway default."
    ),
    secret_file: Path | None = typer.Option(
        None,
        "--secret-file",
        help="Read the Lark app_secret from this file. Never pass the secret on "
        "the command line; use this flag, pipe it on stdin, or the hidden prompt.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing adapter of the same name."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Register a new external-platform (Lark) adapter in local config.

    Writes channels.json and secrets/lark-<name>.json (mode 0600), then asks
    the running daemon to reload its adapter configs so 'hyprial adapter start'
    sees the new adapter without a restart.  When no daemon answers, nothing
    is lost: the daemon reads the same config at its next start.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import add_lark_gateway
        from hyprial.kernel import PersistentConfigError

        routes = _parse_adapter_routes(route)
        secret = _read_adapter_secret(secret_file=secret_file, json_output=json_output)
        try:
            result = add_lark_gateway(
                hyprial_home=services._hyprial_home(),
                name=name,
                app_id=app_id,
                app_secret=secret,
                routes=routes,
                default_route=default_route,
                force=force,
            )
        except PersistentConfigError as error:
            from hyprial.daemon import AdapterExistsError

            code = (
                "ADAPTER_EXISTS"
                if isinstance(error, AdapterExistsError)
                else ipc_errors.INVALID_ARGUMENT
            )
            raise services.CliError(code, str(error)) from error
        # Best-effort hot reload: a reachable daemon picks the new adapter up
        # now; an unreachable one reads the same config at its next start.
        try:
            result["daemonReload"] = services._daemon_request(
                "adapter.reload", {}, timeout=5.0, restore_wait=0.0
            )
        except (services.CliError, ipc_errors.TransientDaemonError):
            result["daemonReload"] = None
        return result

    services._execute(operation, json_output=json_output)


def _verification_expiry(prompt: Any) -> str:
    minutes = max(1, round(prompt.expire_in / 60)) if prompt.expire_in else 0
    return f" (expires in ~{minutes} min)" if minutes else ""


def _present_verification_prompt(prompt: Any) -> None:
    """Put the device-authorization handoff in front of the human, on stderr.

    stderr rather than stdout because ``--json`` stdout must stay exactly one
    JSON value; this also keeps the URL visible when stdout is piped. It is a
    direct write rather than a log record because a log line that scrolls past
    is not a handoff -- the flow blocks on this URL being opened.
    """

    expiry = _verification_expiry(prompt)
    notice(
        "\n"
        "  Lark App onboarding needs you to authorize in a browser.\n"
        "  Open this URL, or scan it as a QR code, with the Feishu/Lark app\n"
        f"  of the tenant that should own this App{expiry}:\n\n"
        f"    {prompt.url}\n\n"
        "  Waiting for authorization... (Ctrl-C to abort)\n\n"
    )


@adapter_app.command("onboard")
def adapter_onboard(
    name: str = typer.Argument(
        ..., help="Gateway name; also names the credential file lark-<name>.json."
    ),
    app_id: str | None = typer.Option(
        None,
        "--app-id",
        help="Authorize this existing Lark app instead of creating a new one.",
    ),
    new: bool = typer.Option(
        False,
        "--new",
        help="Always create a new App, ignoring HYPRIAL_LARK_APP_ID/SECRET.",
    ),
    route: list[str] = typer.Option(
        [],
        "--route",
        help="Direct route as name=native_chat_id; repeatable. Optional: a "
        "brand-new App has not been messaged yet, so its ids are unknown.",
    ),
    default_route: str | None = typer.Option(
        None, "--default-route", help="Route name to use as the gateway default."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing adapter of the same name."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create or adopt a Lark App, then register it as an adapter.

    Unlike 'adapter add' (which registers an App you already have), this runs
    the official device-authorization flow: it prints a verification URL that
    you must open in Feishu/Lark to approve, then stores the resulting
    credential. The App is declared with the scopes and the
    im.message.receive_v1 event the gateway needs.

    If HYPRIAL_LARK_APP_ID and HYPRIAL_LARK_APP_SECRET are both set, they are used
    directly and no browser step happens (use --new to force creation anyway).
    Because a human must approve, this command needs a TTY: with --json or a
    non-interactive stdin it fails with USER_ACTION_REQUIRED and tells you what
    to run or set instead.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        import lark_oapi

        from hyprial.daemon import (
            AdapterExistsError,
            add_lark_gateway,
            ensure_lark_gateway_addable,
        )
        from hyprial.daemon import UserActionRequiredError, resolve_lark_app_credential
        from hyprial.kernel import PersistentConfigError

        if new and app_id is not None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "adapter onboard accepts either --new or --app-id, not both",
            )
        routes = _parse_adapter_routes(route) if route else ()
        if default_route is not None and not routes:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--default-route requires at least one --route",
            )
        # Fail on a bad or taken name *before* asking a human to approve the
        # creation of a real App: that approval cannot be taken back, and a
        # credential we then refuse to store is an App stranded on the tenant.
        try:
            ensure_lark_gateway_addable(hyprial_home=services._hyprial_home(), name=name, force=force)
        except PersistentConfigError as error:
            code = (
                "ADAPTER_EXISTS"
                if isinstance(error, AdapterExistsError)
                else ipc_errors.INVALID_ARGUMENT
            )
            raise services.CliError(code, str(error)) from error
        # A blocking device-authorization flow is only honest when a human is
        # actually watching; --json additionally owns stdout as one JSON value.
        non_interactive = json_output or not sys.stdin.isatty()
        prompts: list[Any] = []
        # Self-hosted or test accounts endpoint. Both the Feishu and the Lark
        # domain are overridden together so the SDK's tenant_brand switch can
        # never fall back to the public endpoint mid-flow.
        domain = os.environ.get("HYPRIAL_LARK_ACCOUNTS_DOMAIN")

        def present(prompt: Any) -> None:
            prompts.append(prompt)
            _present_verification_prompt(prompt)

        try:
            credential = resolve_lark_app_credential(
                name=name,
                register_app=lark_oapi.register_app,
                on_verification_url=present,
                non_interactive=non_interactive,
                app_id=app_id,
                create_only=new,
                source="hyprial",
                **({"domain": domain, "lark_domain": domain} if domain else {}),
            )
        except UserActionRequiredError as error:
            raise services.CliError("USER_ACTION_REQUIRED", str(error), error.action) from error

        try:
            registered = add_lark_gateway(
                hyprial_home=services._hyprial_home(),
                name=name,
                app_id=credential.app_id,
                app_secret=credential.app_secret,
                routes=routes,
                default_route=default_route,
                force=force,
                allow_no_routes=True,
            )
        except PersistentConfigError as error:
            code = (
                "ADAPTER_EXISTS"
                if isinstance(error, AdapterExistsError)
                else ipc_errors.INVALID_ARGUMENT
            )
            raise services.CliError(code, str(error)) from error

        # The secret is already persisted at 0600 by add_lark_gateway and is
        # deliberately absent from this summary.
        return {
            **registered,
            "onboarding": {
                "source": "environment" if not prompts else "device_authorization",
                "createdApp": bool(prompts),
                **(
                    {
                        "verificationUrl": prompts[-1].url,
                        "verificationExpiresIn": prompts[-1].expire_in,
                    }
                    if prompts
                    else {}
                ),
            },
            "nextStep": (
                f"Run 'hyprial adapter authorize {name}' to request tenant-admin "
                "approval for the declared scopes, then 'hyprial adapter reload' "
                "so a running daemon picks the adapter up without a daemon "
                "restart; afterwards 'hyprial adapter pin' and 'hyprial adapter "
                "start'."
            ),
        }

    services._execute(operation, json_output=json_output)


def _confirm_adapter_stopped(name: str, *, force: bool) -> None:
    """Fail closed while a live daemon reports the adapter as running.

    A running daemon is the only liveness signal for adapter workers: it spawns
    and supervises them, so when no daemon answers, none of its adapters can be
    running (or be restarted by it). Removal may then proceed, and the
    desired-state cleanup additionally prevents the next daemon boot from
    reviving the adapter. An ADAPTER_NOT_FOUND from the daemon means its
    gateway snapshot (taken at boot, refreshed by 'hyprial adapter reload')
    does not know this adapter, so it cannot be running it either.
    """
    services = get_services()

    try:
        status = services._daemon_request("adapter.status", {"name": name})
    # PR #332 F4②: the transient half of the old mixed set is the registered
    # class; ADAPTER_NOT_FOUND (permanent) keeps its code comparison.
    except ipc_errors.DaemonUnavailableError:
        return
    except services.CliError as error:
        if error.code != ipc_errors.ADAPTER_NOT_FOUND:
            raise
        return
    adapter = status.get("adapter", {}) if isinstance(status, dict) else {}
    running = (
        "pid" in adapter
        or adapter.get("online") is True
        or adapter.get("status") in {"online", "starting"}
    )
    if not running:
        return
    if not force:
        raise services.CliError(
            "ADAPTER_RUNNING",
            f"adapter {name!r} is still running; run 'hyprial adapter stop {name}' "
            "first, or pass --force to stop it as part of the removal",
        )
    stopped = services._daemon_request("adapter.stop", {"name": name})
    after = stopped.get("adapter", {}) if isinstance(stopped, dict) else {}
    if (
        "pid" in after
        or after.get("online") is True
        or after.get("status") in {"online", "starting"}
    ):
        raise services.CliError(
            "ADAPTER_STOP_FAILED",
            f"adapter {name!r} did not stop; refusing to remove it",
        )


@adapter_app.command("remove")
def adapter_remove(
    name: str = typer.Argument(..., help="Gateway name to de-register."),
    force: bool = typer.Option(
        False, "--force", help="Stop a running adapter first, then remove it."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """De-register an external-platform (Lark) adapter from local config.

    The adapter must be stopped first ('hyprial adapter stop <name>'), or pass
    --force to stop it as part of the removal. Removes the channels.json
    gateway entry, the secrets/lark-<name>.json credential, the desired-state
    harness entry, and any adapter pin referencing the gateway. A running
    daemon forgets the adapter from 'hyprial adapter list' after 'hyprial
    adapter reload' (it keeps a snapshot of the gateway list).
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import (
            AdapterConfigConflictError,
            AdapterNotFoundError,
            remove_lark_gateway,
        )
        from hyprial.kernel import DesiredStateError
        from hyprial.daemon import DaemonOwnershipBusy
        from hyprial.kernel import PersistentConfigError

        _confirm_adapter_stopped(name, force=force)
        try:
            return services._daemon_request("management.adapter.remove", {"name": name})
        except ipc_errors.DaemonUnavailableError:
            pass
        try:
            return remove_lark_gateway(
                hyprial_home=services._hyprial_home(), state_dir=services._state_dir(), name=name
            )
        except PersistentConfigError as error:
            code = (
                error.code
                if isinstance(error, AdapterConfigConflictError)
                else (
                    ipc_errors.ADAPTER_NOT_FOUND
                    if isinstance(error, AdapterNotFoundError)
                    else ipc_errors.INVALID_ARGUMENT
                )
            )
            raise services.CliError(code, str(error)) from error
        except DesiredStateError as error:
            # Fail closed: never rewrite config against a desired-state file
            # we cannot parse.
            raise services.CliError("DESIRED_STATE_ERROR", str(error)) from error
        except DaemonOwnershipBusy as error:
            raise services.CliError(error.code, str(error)) from error

    services._execute(operation, json_output=json_output)


@adapter_app.command("start")
def adapter_start(
    name: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Start an external-platform adapter."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("adapter.start", {"name": name}),
        json_output=json_output,
    )


@adapter_app.command("stop")
def adapter_stop(
    name: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Stop an external-platform adapter."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("adapter.stop", {"name": name}), json_output=json_output
    )


@adapter_app.command("status")
def adapter_status(
    name: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show one external-platform adapter."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("adapter.status", {"name": name}),
        json_output=json_output,
    )


@adapter_app.command("reload")
def adapter_reload(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Reload adapter configs into the running daemon (incremental)."""
    services = get_services()

    services._execute(lambda: services._daemon_request("adapter.reload", {}), json_output=json_output)


@adapter_app.command("list")
def adapter_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List external-platform adapters."""
    services = get_services()

    services._execute(lambda: services._daemon_request("adapter.list", {}), json_output=json_output)


@adapter_app.command("doctor")
def adapter_doctor(
    name: str = typer.Argument(..., help="Configured Lark adapter name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Inventory declared and tenant-authorized Lark App scopes."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import configured_scope_client, diagnose_scopes
        from hyprial.kernel import PersistentConfigError

        try:
            app_id, client = configured_scope_client(services._hyprial_home(), services._state_dir(), name)
            return diagnose_scopes(
                adapter=name, app_id=app_id, response=client.list_scopes()
            )
        except PersistentConfigError as error:
            raise services.CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error

    services._execute(operation, json_output=json_output)
