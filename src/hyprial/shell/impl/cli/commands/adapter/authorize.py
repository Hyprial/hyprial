"""``hyprial adapter authorize`` interactive flow."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import notice

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Sequence
from hyprial.kernel import ipc_errors
import os
import typer

from hyprial.shell.impl.cli.commands.adapter.admin import _verification_expiry, adapter_app
from hyprial.shell.impl.cli.commands.common.support import JsonObject
def _present_authorization_prompt(
    prompt: Any,
    *,
    adapter: str,
    app_id: str,
    scopes: Sequence[str],
    warnings: Sequence[str],
) -> None:
    """Hand the one-click authorization link to a human, on stderr.

    stderr for the same reason as the onboarding prompt: ``--json`` owns stdout
    as exactly one JSON value. Unlike a log line, this link is usually copied to
    somebody else -- whoever can approve the App -- so it is printed with the
    scopes it will grant, and any privacy-widening scope is called out by name.
    """

    expiry = _verification_expiry(prompt)
    notes = "".join(f"  ! {line}\n" for line in warnings)
    notice(
        "\n"
        f"  Lark App authorization for adapter {adapter!r} (app {app_id}).\n"
        "  Opening this link in Feishu/Lark declares and authorizes the scopes\n"
        f"  below in a single confirmation -- no developer console visit{expiry}:\n"
        "\n"
        f"    {prompt.url}\n"
        "\n"
        f"  Requested tenant scopes: {', '.join(scopes)}\n"
        f"{notes}"
        "\n"
        "  Waiting for authorization... (Ctrl-C to abort)\n\n"
    )


def _authorize_interactively(name: str, capabilities: Sequence[str]) -> JsonObject:
    """Mint and present a one-click device-authorization link for an adapter.

    The adapter must already exist: its configured app_id is what makes this a
    re-authorization rather than an App creation. There is deliberately no
    ``ensure_lark_gateway_addable`` guard here -- that guard protects the
    *creation* path from stranding a new App behind a name clash, and applying
    it here would reject exactly the adapters this command is for.
    """
    services = get_services()

    import lark_oapi

    from hyprial.daemon import LARK_ONBOARDING_REQUIRED_EVENTS, LarkOnboardingError, reauthorize_lark_app
    from hyprial.daemon import SENSITIVE_CAPABILITY_NOTES, capability_scopes, configured_app_id
    from hyprial.kernel import PersistentConfigError

    requested = list(dict.fromkeys(capabilities))
    try:
        scopes = capability_scopes(requested)
    except ValueError as error:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    try:
        # Only the app_id is resolved: this flow authenticates the human, so no
        # app secret is fetched, sent, rotated, or re-persisted anywhere in it.
        app_id = configured_app_id(services._hyprial_home(), services._state_dir(), name)
    except PersistentConfigError as error:
        raise services.CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error

    warnings = [
        SENSITIVE_CAPABILITY_NOTES[item]
        for item in requested
        if item in SENSITIVE_CAPABILITY_NOTES
    ]
    prompts: list[Any] = []
    # Self-hosted or test accounts endpoint; both domains move together so the
    # SDK's tenant_brand switch cannot fall back to the public endpoint mid-flow.
    domain = os.environ.get("HYPRIAL_LARK_ACCOUNTS_DOMAIN")

    def present(prompt: Any) -> None:
        prompts.append(prompt)
        _present_authorization_prompt(
            prompt, adapter=name, app_id=app_id, scopes=scopes, warnings=warnings
        )

    try:
        authorized = reauthorize_lark_app(
            app_id=app_id,
            register_app=lark_oapi.register_app,
            on_verification_url=present,
            capabilities=requested,
            source="hyprial",
            **({"domain": domain, "lark_domain": domain} if domain else {}),
        )
    except LarkOnboardingError as error:
        raise services.CliError("LARK_AUTHORIZATION_FAILED", str(error)) from error
    except Exception as error:  # noqa: BLE001 - SDK raises its own error types
        raise services.CliError(
            "LARK_AUTHORIZATION_FAILED",
            f"the device-authorization flow failed: {type(error).__name__}: {error}",
        ) from error

    return {
        "ok": True,
        "adapter": name,
        "appId": authorized,
        "status": "authorized",
        "source": "device_authorization",
        "capabilities": requested,
        "scopes": list(scopes),
        "events": list(LARK_ONBOARDING_REQUIRED_EVENTS),
        **(
            {
                "verificationUrl": prompts[-1].url,
                "verificationExpiresIn": prompts[-1].expire_in,
            }
            if prompts
            else {}
        ),
        "nextStep": (
            f"Run 'hyprial adapter doctor {name}' to confirm the tenant grants; "
            "grants take effect on the Lark side, and 'hyprial adapter reload' "
            "covers any local config change -- no daemon restart is needed."
        ),
    }


@adapter_app.command("authorize")
def adapter_authorize(
    name: str = typer.Argument(..., help="Configured Lark adapter name."),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="Mint a one-click authorization link that declares AND authorizes "
        "the scopes, instead of only requesting already-declared ones.",
    ),
    capability: list[str] = typer.Option(
        [],
        "--capability",
        help="Optional gateway capability to add to an --interactive request; "
        "repeatable. Required capabilities are always included. See "
        "'hyprial adapter doctor <name>' for the capability ids.",
    ),
    scope: list[str] = typer.Option(
        [],
        "--scope",
        help="Generate a preselected administrator permission link for this "
        "scope; repeatable. Does not call Lark or change declared capabilities.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Get the App's scopes authorized by the tenant.

    Default: ask the tenant admin to approve the scopes the App has *already*
    declared (the v6 scopes/apply endpoint). It cannot add an undeclared scope,
    publish an App version, or approve the request; the returned official URL is
    the manual administrator handoff. An App with nothing declared but
    unapproved therefore comes back as 'nothing_to_request'.

    --interactive instead runs the official device-authorization flow against
    this adapter's existing App and prints a verification link. Because the
    scope/event declaration rides along with that link as the 'addons'
    parameter, one confirmation both declares and authorizes the scopes -- which
    is the only way to reach a capability the App never declared. The link goes
    to stderr, so '--json' stdout stays exactly one JSON value.

    Optional capabilities are never requested implicitly: pass --capability
    group-history to include im:message.group_msg, which lets the App read every
    message in its groups rather than only @-mentions.

    --scope is a separate, link-only handoff for already-known scope names. It
    cannot be combined with --interactive or --capability and never calls a
    Lark API.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import configured_app_id, configured_scope_client, developer_console_permission_url, request_scope_authorization, scope_apply_url, valid_scope_name
        from hyprial.kernel import PersistentConfigError

        if scope and (interactive or capability):
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--scope cannot be combined with --interactive or --capability",
            )
        if capability and not interactive:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--capability requires --interactive: the scopes/apply endpoint "
                "cannot declare a scope the App does not already have",
            )
        if interactive:
            return _authorize_interactively(name, capability)
        if scope:
            invalid = [value for value in scope if not valid_scope_name(value)]
            if invalid:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "invalid --scope value(s): "
                    + ", ".join(repr(value) for value in invalid)
                    + "; scope names must be non-empty and contain no whitespace "
                    "or commas",
                )
            selected = list(dict.fromkeys(scope))
            try:
                app_id = configured_app_id(services._hyprial_home(), services._state_dir(), name)
            except PersistentConfigError as error:
                raise services.CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error
            return {
                "ok": True,
                "adapter": name,
                "appId": app_id,
                "status": "link_generated",
                "scopes": selected,
                "authorizationUrl": scope_apply_url(app_id, selected),
                "manualApprovalRequired": True,
            }
        try:
            app_id, client = configured_scope_client(services._hyprial_home(), services._state_dir(), name)
            result = request_scope_authorization(client.apply_scopes())
        except PersistentConfigError as error:
            raise services.CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error
        return {
            **result,
            "adapter": name,
            "authorizationUrl": developer_console_permission_url(app_id),
            "authorizationUrlReason": (
                "missing scopes are unknown; no preselected scope-apply link "
                "was generated"
            ),
            "manualApprovalRequired": True,
            # A dead end here is usually an *undeclared* scope, which this
            # endpoint structurally cannot request. Name the way out instead of
            # leaving the caller at the console link.
            **(
                {}
                if result.get("ok")
                else {
                    "nextStep": (
                        f"Run 'hyprial adapter doctor {name}' to see which "
                        "capabilities are undeclared, then 'hyprial adapter "
                        f"authorize {name} --interactive' to declare and "
                        "authorize them with one link."
                    )
                }
            ),
        }

    services._execute(operation, json_output=json_output)
