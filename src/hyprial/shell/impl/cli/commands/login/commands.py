"""``hyprial login`` command."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # deferred annotations only
    from hyprial.identity import IdentityTransactionLock

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import HYPRIALHomeNotInitialized
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS, JsonObject, _INIT_READY_TIMEOUT_ENV, _announce_setup_guidance, _fail
@app.command()
def login(
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help=(
            "Do not launch a browser; still print the verification URI and user code."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    switch_account: bool = typer.Option(
        False,
        "--switch-account",
        help=(
            "Allow switching to a different account than settings.owner "
            "(an explicit one-time switch). By default login stops the old "
            "daemon, proves exit, and starts the verified replacement. With "
            "--no-daemon the daemon must already be stopped. With --json the "
            "flag is the confirmation; otherwise you are asked to type yes."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Authenticate the candidate and preview owner migration from "
            "SQLite backup copies. Requires --switch-account; writes no "
            "credential/settings/state and does not stop, provision, or start."
        ),
    ),
    no_daemon: bool = typer.Option(
        False,
        "--no-daemon",
        help=(
            "Do not stop or start a daemon. An identity change is refused "
            "while the daemon is live or cannot be proven stopped."
        ),
    ),
    ready_timeout: float = typer.Option(
        # Init's progress-idle budget, not a second one: same default, same
        # environment variable, same Click parsing. Only a login that finds
        # the home missing starts a daemon, and then it finishes init.
        DAEMON_START_IDLE_BUDGET_SECONDS,
        "--ready-timeout",
        envvar=_INIT_READY_TIMEOUT_ENV,
        hidden=True,
        help=(
            "Seconds allowed without daemon startup phase progress when login "
            "initializes a missing home (env: HYPRIAL_INIT_READY_TIMEOUT)."
        ),
    ),
) -> None:
    """Log in this home's user identity and provision the device key.

    Since the tailnet cutover there is no sidecar install step and no
    control-plane join: the identity stage (U2) lands the owner in
    settings.json and the refresh token in secrets/login.json (0600), and
    the device stage asks the ``hyprial-tailcat`` sidecar to ensure this
    machine's device key exists (``genkey``), writing the public device
    record.  A missing or failing sidecar never rolls the identity back
    (D12): login still succeeds and reports ``device.ready: false`` with a
    next step — re-running login retries the device stage alone.  A
    different existing owner is switched only with --switch-account. By
    default login stops the old generation, proves exit, reclassifies
    stopped state, commits, and starts and verifies the replacement.
    ``--no-daemon`` preserves the explicit identity-only path and refuses a
    live identity change.  A home with no owner yet (neither settings.owner
    nor HYPRIAL_OWNER; a missing home always counts) is first-time setup
    instead: login commits the identity, then init's shared setup and
    daemon-start path starts the daemon once.
    """
    services = get_services()

    if dry_run and not switch_account:
        _fail(
            services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--dry-run requires --switch-account",
                {"requiredFlag": "--switch-account"},
            ),
            json_output=json_output,
        )

    def operation() -> JsonObject:
        services = get_services()
        if dry_run:
            # A preview writes nothing, so it must not create a missing home.
            try:
                services.require_initialized_hyprial_home()
            except HYPRIALHomeNotInitialized as error:
                raise services.CliError(
                    error.code,
                    f"{error}; --dry-run previews an existing home's account "
                    "switch and writes nothing, so it does not create one",
                    data=error.data,
                ) from error
        if dry_run or not _login_route_is_first_time_setup():
            return services._run_login_cli_flow(
                no_open=no_open,
                json_output=json_output,
                switch_account=switch_account,
                dry_run=dry_run,
                no_daemon=no_daemon,
            )

        try:
            services.require_initialized_hyprial_home()
        except HYPRIALHomeNotInitialized:
            home_was_missing = True
            services.initialize_hyprial_home()
            org_warning = services._initialize_org_context()
        else:
            home_was_missing = False
            org_warning = None

        held_identity_transaction: list[IdentityTransactionLock] = []
        try:
            try:
                response = services._run_login_cli_flow(
                    no_open=no_open,
                    json_output=json_output,
                    switch_account=switch_account,
                    no_daemon=True,
                    first_time_setup=True,
                    held_identity_transaction=held_identity_transaction,
                )
            except KeyboardInterrupt as error:
                if not home_was_missing:
                    raise
                raise services.CliError(
                    "INTERRUPTED",
                    "login was interrupted after home initialization; rerun `hyprial login`",
                    data={"nextSteps": ["hyprial login"]},
                ) from error
            except services.CliError as error:
                if not home_was_missing:
                    raise
                data = dict(error.data) if isinstance(error.data, dict) else {}
                data["nextSteps"] = ["hyprial login"]
                raise services.CliError(
                    error.code,
                    f"{error}; home is initialized, rerun `hyprial login`",
                    data=data,
                ) from error
            if not no_daemon:
                response["daemon"] = services._complete_initialization(
                    ready_timeout=ready_timeout,
                    listen=None,
                    connect=None,
                    org_warning=org_warning,
                    login_when_missing=None,
                    held_identity_transaction=held_identity_transaction,
                )
                migration = response["daemon"].get("migration")
                if isinstance(migration, dict):
                    response["migration"] = migration
                # First login is the moment a new member has no squire and
                # is watching the terminal: say it on stderr, not only in
                # the nested JSON.
                if not json_output:
                    _announce_setup_guidance(response["daemon"])
            return response
        finally:
            for transaction in held_identity_transaction:
                transaction.close()

    services._execute(operation, json_output=json_output, allow_missing_home=True)


def _login_route_is_first_time_setup() -> bool:
    """Whether ``hyprial login`` takes the first-time setup route.

    First-time setup means exactly "init would run login":
    ``node_owner_or_none()`` finds no owner in settings.json and no
    ``HYPRIAL_OWNER``.  A home directory without an owner is first-time
    setup: the identity step commits, then init's start path starts the
    daemon once, under the identity step's held transaction lock.  A
    missing home is always first-time setup -- it has no settings to switch
    away from, and the missing-home bootstrap creates it even when
    ``HYPRIAL_OWNER`` is set.  This is the one cell where the route differs
    from ``node_owner_or_none()`` alone.  Any owner on an existing home --
    including an older settings owner whose ``identityIssuer`` is empty, or
    only ``HYPRIAL_OWNER`` -- takes the orchestrated-switch path.  Malformed
    settings are not provably ownerless, so they also stay on that path,
    which reports them.
    """
    services = get_services()

    from hyprial.daemon import node_owner_or_none

    home = services._hyprial_home()
    if not home.is_dir():
        return True
    try:
        owner = node_owner_or_none(hyprial_home=home)
    except ValueError:
        return False
    return owner is None
