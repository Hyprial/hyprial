"""``hyprial lark-auth`` device authorization maintenance."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import warn

from hyprial.shell.impl.cli.commands.common.services import get_services

from pathlib import Path
import os
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject, _local_operator_identity
lark_auth_app = typer.Typer(
    help=(
        "Watch the lark-cli USER credential: detect expiry, walk the device "
        "flow to 'one click away', and push the link to a human."
    )
)


def _lark_auth_state_path() -> Path:
    services = get_services()
    from hyprial.daemon import credentials_reauth as reauth

    return services._state_dir() / reauth.STATE_FILENAME


def _canonical_lark_auth_state_path() -> Path:
    """The machine-global anchor: the real home, ignoring HYPRIAL_HOME /
    HARNESS_STATE_DIR overrides.  The pending device flow belongs to
    lark-cli's machine-global identity, and lark-cli honors neither variable.
    """
    services = get_services()

    from hyprial.daemon import credentials_reauth as reauth

    return services.default_hyprial_home()[0] / "state" / reauth.STATE_FILENAME


def _lark_auth_guard_notify_anchor(
    state_path: Path, notify_route: str | None, sender: str | None
) -> None:
    from hyprial.daemon import credentials_reauth as reauth

    route, from_identity = reauth.effective_notification_config(
        state_path, notify_route=notify_route, sender=sender, env=os.environ
    )
    reauth.ensure_notify_anchor_canonical(
        state_path=state_path,
        canonical_path=_canonical_lark_auth_state_path(),
        route=route,
        sender=from_identity,
    )


def _lark_auth_send(sender: str, route: str, text: str) -> None:
    """Deliver the handoff over the daemon's App credential (bot/tenant
    token) -- deliberately independent of the expired lark-cli user token."""
    services = get_services()

    services._daemon_request(
        "message.send",
        {
            "from": _local_operator_identity(),
            "onBehalfOf": sender,
            "to": [route],
            "message": text,
        },
    )


def _lark_auth_timer_check() -> JsonObject | None:
    """Best-effort lark-cli user-credential check for the autoupdate timer.

    The timer is the already-happening heartbeat this watchdog hangs on (#142:
    a mechanism nothing triggers is dead code).  This must never fail the
    upgrade it rides with: every error collapses into the returned record.
    """

    try:
        from hyprial.daemon import credentials_reauth as reauth

        executable = reauth.find_lark_cli()
        if executable is None:
            return None  # machine without lark-cli: nothing to watch
        state_path = _lark_auth_state_path()
        # The anchor guard runs here too: under a misconfigured timer env it
        # must surface in the record, not orphan a flow.
        _lark_auth_guard_notify_anchor(state_path, None, None)
        result = reauth.check(
            reauth.make_cli_runner(executable),
            state_path=state_path,
            send=_lark_auth_send,
        )
        record: JsonObject = {"status": result.status}
        if result.status == "awaiting_user":
            record["notified"] = result.notified
            if result.notify_route is not None:
                record["notifyRoute"] = result.notify_route
            if result.error is not None:
                record["notifyError"] = result.error
        return record
    except Exception as error:  # noqa: BLE001 - the timer must not fail on this
        return {"status": "error", "error": type(error).__name__}


@lark_auth_app.command("check")
def lark_auth_check(
    notify_route: str | None = typer.Option(
        None,
        "--notify-route",
        help="route:<adapter>:<route> that receives the authorization link. "
        "Precedence: this flag > $HYPRIAL_LARK_REAUTH_NOTIFY_ROUTE > the route "
        "remembered from an earlier run. Without one, the link only lands in "
        "the local state file and this command's output.",
    ),
    sender: str | None = typer.Option(
        None,
        "--from",
        help="Registered local identity the notification is sent from "
        "($HYPRIAL_LARK_REAUTH_FROM is the fallback). Required for delivery.",
    ),
    domain: list[str] = typer.Option(
        [],
        "--domain",
        help="Business domain(s) to request when re-authorization is needed; "
        "repeatable. Default: all (a default, not a law -- whether a fresh "
        "login restores previously granted scopes is unverified).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Check the lark-cli user identity; escalate to a one-click link.

    * ready/valid -> silent ok, nobody is bothered;
    * needs_refresh -> one side-effect-free read triggers lark-cli's lazy
      refresh; if that recovers the token, still silent (this is the
      unattended path and it never notifies);
    * missing/dead refresh -> `lark-cli auth login --no-wait` mints a
      verification link that is pushed to --notify-route, then
      `hyprial lark-auth complete` finishes the flow after the human clicks.
      Re-notification is cooled down per app (default 1h) and a still-valid
      pending link is never replaced by a newer one.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import credentials_reauth as reauth

        executable = reauth.find_lark_cli()
        if executable is None:
            return {"status": "unavailable", "reason": "lark-cli not installed"}
        try:
            state_path = _lark_auth_state_path()
            _lark_auth_guard_notify_anchor(state_path, notify_route, sender)
            result = reauth.check(
                reauth.make_cli_runner(executable),
                state_path=state_path,
                notify_route=notify_route,
                sender=sender,
                domains=tuple(domain) if domain else reauth.DEFAULT_DOMAINS,
                send=_lark_auth_send,
            )
        except reauth.ReauthError as error:
            raise services.CliError(error.code, str(error)) from error
        if result.status == "awaiting_user":
            where = (
                f"pushed to {result.notify_route}"
                if result.notified
                else "NOT pushed (no route/sender configured or delivery failed)"
            )
            warn(
                f"lark-cli 用户凭据需要重新授权,链接已生成({where}):\n"
                f"{result.verification_url}\n"
                "人点完后运行 `hyprial lark-auth complete` 收尾。",
                json_output=json_output,
            )
        return result.to_json()

    services._execute(operation, json_output=json_output)


@lark_auth_app.command("complete")
def lark_auth_complete(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Finish a pending device flow after the human clicked the link.

    Polls the pending device code (from the 0600 state file) until the
    platform confirms the authorization or the code expires; on success the
    remembered route gets a single all-clear notice.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import credentials_reauth as reauth

        executable = reauth.find_lark_cli()
        if executable is None:
            return {"status": "unavailable", "reason": "lark-cli not installed"}
        try:
            state_path = _lark_auth_state_path()
            result = reauth.complete(
                reauth.make_cli_runner(executable),
                state_path=state_path,
                send=_lark_auth_send,
            )
        except reauth.ReauthError as error:
            raise services.CliError(error.code, str(error)) from error
        canonical = _canonical_lark_auth_state_path()
        if (
            result.status == "no_pending"
            and state_path.resolve() != canonical.resolve()
            and canonical.exists()
        ):
            # A pending flow exists at the machine-global anchor while this
            # invocation reads an isolated one -- almost certainly an env
            # override the caller forgot about.
            result.extra["hint"] = (
                f"no pending flow at {state_path}, but one exists at the "
                f"canonical anchor {canonical}; rerun without HYPRIAL_HOME / "
                "HARNESS_STATE_DIR overrides to complete it"
            )
        return result.to_json()

    services._execute(operation, json_output=json_output)
