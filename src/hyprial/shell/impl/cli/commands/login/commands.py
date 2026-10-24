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
from hyprial.shell.impl.cli.output import CliResult, confirm


@app.command()
def login(
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-authenticate even when this home already has a coherent user login.",
    ),
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

    A home is already logged in when it has a valid secrets/login.json
    credential, settings.json has a Casdoor owner, the credential, settings,
    and selected profile issuers match, and the device key stage is complete.
    When only the device stage is incomplete, the default command resumes it
    without authenticating again. Otherwise the default command reports
    that local session (token expiry is
    unknown because the stored refresh token has no expiry field) and changes
    nothing. Use --force to authenticate again.

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

    def operation() -> CliResult:
        services = get_services()
        if not force and not switch_account and not dry_run:
            from hyprial.shell.impl.cli.commands.login.flow import (
                _login_device_id,
                _resume_login_cli_flow,
            )
            from hyprial.shell.impl.login.session import (
                inspect_login_session,
                login_device_stage_complete,
            )

            from hyprial.daemon import node_owner_or_none

            profile, _source = services.resolve_profile()
            home = services._hyprial_home()
            existing = inspect_login_session(home, expected_issuer=profile.issuer)
            try:
                effective_owner = node_owner_or_none(hyprial_home=home)
            except ValueError:
                effective_owner = None
            if existing is not None and existing["owner"] == effective_owner:
                device_id = _login_device_id()
                if login_device_stage_complete(
                    home, owner=existing["owner"], device_id=device_id
                ):
                    return CliResult(existing)
                return CliResult(
                    _resume_login_cli_flow(
                        existing,
                        expected_issuer=profile.issuer,
                        json_output=json_output,
                    )
                )
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
            return CliResult(
                services._run_login_cli_flow(
                    no_open=no_open,
                    json_output=json_output,
                    switch_account=switch_account,
                    dry_run=dry_run,
                    no_daemon=no_daemon,
                )
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
            steps = _organization_next_steps(services)
            if steps is not None:
                response["nextSteps"] = steps
            return CliResult(response)
        finally:
            for transaction in held_identity_transaction:
                transaction.close()

    services._execute(operation, json_output=json_output, allow_missing_home=True)


@app.command()
def logout(
    yes: bool = typer.Option(
        False, "--yes", help="Confirm removal of this home's user login."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove this home's user login without removing machine or agent keys.

    Logout removes secrets/login.json plus the Casdoor identity fields that
    login wrote in settings.json. It never removes agent credentials, Tailcat
    device keys, or unrelated settings. A running daemon is stopped and its
    exit proved before its persisted identity is removed. When no user-login
    artifacts exist, the command reports not-logged-in and succeeds without a
    confirmation.

    It does not revoke already-issued access tokens at the issuer.
    Such tokens can remain valid until expiry.
    """
    services = get_services()

    def operation() -> CliResult:
        from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock
        from hyprial.shell.impl.cli.commands.login.flow import _prove_daemon_absent
        from hyprial.shell.impl.login.session import (
            inspect_login_artifacts,
            remove_login_artifacts,
        )

        services = get_services()
        home = services._hyprial_home()
        artifacts = inspect_login_artifacts(home)
        if not artifacts.present:
            return CliResult(
                {
                    "status": "not-logged-in",
                    "home": str(home),
                    "removed": [],
                }
            )
        if not yes:
            if json_output or not services._stdin_isatty():
                raise services.CliError(
                    "CONFIRMATION_REQUIRED",
                    "logout from a non-interactive command requires --yes",
                )
            if not confirm(f"Remove the user login from {home}?"):
                raise services.CliError("CANCELLED", "logout cancelled")

        try:
            transaction = IdentityTransactionLock.acquire(home)
        except IdentityTransactionBusy as error:
            raise services.CliError(error.code, str(error)) from error
        try:
            # Re-read under the identity fence so a concurrent login cannot
            # land between the preview/confirmation and removal.
            artifacts = inspect_login_artifacts(home)
            if not artifacts.present:
                return CliResult(
                    {
                        "status": "not-logged-in",
                        "home": str(home),
                        "removed": [],
                    }
                )
            daemon: JsonObject = {"state": "not-affected"}
            if artifacts.casdoor_identity_present:
                try:
                    probe = services._daemon_probe(timeout=0.5)
                except ipc_errors.DaemonUnavailableError:
                    daemon = _prove_daemon_absent(
                        home,
                        services._state_dir(),
                        lambda phase, **details: {
                            "failedPhase": phase,
                            **details,
                        },
                    )
                else:
                    daemon = services._stop_daemon_for_identity_switch()
                    daemon["state"] = "stopped"
                    if isinstance(probe, dict):
                        daemon.setdefault("previousEpoch", probe.get("epoch"))
            removed = remove_login_artifacts(home)
            return CliResult(
                {
                    "status": "logged-out" if removed else "not-logged-in",
                    "home": str(home),
                    "removed": removed,
                    "daemon": daemon,
                }
            )
        finally:
            transaction.close()

    services._execute(operation, json_output=json_output, allow_missing_home=True)


_ORG_LIST_HINT_SECONDS = 2.0


def _organization_next_steps(services) -> list[str] | None:
    """Next steps to show when this home provably belongs to no organization.

    ``None`` means UNKNOWN, and unknown must stay silent: if the directory
    cannot be read (no daemon yet, IPC timeout), the caller must not be told
    to create an organization -- that would be a claim about a state we did
    not observe, and an existing member would be told to make a second one.

    Only a successful read whose result is empty is evidence of "no
    organization".  ``empty != unknown`` is the whole point of this function.
    """
    try:
        # A hint must never be why login feels slow: one bounded attempt, no
        # restore budget (that budget can be minutes), and no retry loop.  The
        # caller has already logged in successfully; the only thing at stake
        # here is whether one extra line is printed.
        result = services._daemon_request(
            "org.list", {}, timeout=_ORG_LIST_HINT_SECONDS, restore_wait=0.0
        )
    except Exception:  # noqa: BLE001 - D12: a hint is never worth failing login
        return None
    orgs = None
    if isinstance(result, dict):
        for key in ("orgs", "organizations", "items"):
            candidate = result.get(key)
            if isinstance(candidate, list):
                orgs = candidate
                break
    elif isinstance(result, list):
        orgs = result
    if orgs != []:
        return None
    return [
        "hyprial org create <name>",
        # The second step is run by someone else, so the text names who runs it and
        # marks which parts are placeholders the reader fills in.  A bare command
        # string would read as "run this", and the reader is not the one who can.
        (
            "ask an organization owner to run: "
            "hyprial org invite <org> --user <your-username>"
        ),
    ]

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
