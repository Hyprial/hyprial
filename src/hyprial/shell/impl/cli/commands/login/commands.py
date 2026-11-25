"""``hyprial login`` command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import HYPRIALHomeNotInitialized
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS, JsonObject, _INIT_READY_TIMEOUT_ENV
from hyprial.shell.impl.cli.output import CliResult


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
    ready_timeout: float = typer.Option(
        DAEMON_START_IDLE_BUDGET_SECONDS,
        "--ready-timeout",
        envvar=_INIT_READY_TIMEOUT_ENV,
        hidden=True,
        help=(
            "Seconds allowed without daemon startup phase progress when login "
            "starts the daemon for login "
            "(env: HYPRIAL_INIT_READY_TIMEOUT)."
        ),
    ),
) -> None:
    """Log in this home's user identity and provision the device key.

    The daemon owns the login flow end to end. On a home with no account yet,
    login starts the bootstrap daemon when none runs, shows the verification
    URL and user code, opens the browser (not with --no-open), and waits for
    that one daemon operation until it reaches serving. On a home that already
    has an owner, login starts the serving daemon if needed and runs one
    daemon-driven reauthentication until serving; logging in with a different
    account when local business data exists is refused. A device that cannot be
    registered never rolls the identity back: login still succeeds and reports
    ``device.ready: false`` with ``retry: serving-start``.

    After a new login, confirm organization invitations one at a time on a
    TTY. Non-TTY and --json runs only report pending invitations. To accept
    all invitations explicitly, use org pending --accept-all.
    """
    services = get_services()

    def operation() -> CliResult:
        services = get_services()
        route = _login_route(services)
        if route in {_FIRST, _LAUNCH}:
            started = False
            if route == _LAUNCH:
                _initialize_missing_home(services)
                launched = services._launch_daemon_process(
                    ready_timeout=ready_timeout, autoinstall=False
                )
                started = not launched.already_running
            return CliResult(
                _first_login(
                    services,
                    started=started,
                    no_open=no_open,
                    json_output=json_output,
                )
            )
        started = False
        if route == _LAUNCH_RELOGIN:
            launched = services._launch_daemon_process(
                ready_timeout=ready_timeout, autoinstall=False
            )
            started = not launched.already_running
        return CliResult(
            _serving_relogin(
                services,
                started=started,
                no_open=no_open,
                json_output=json_output,
            )
        )

    services.runtime.execute(operation, json_output=json_output, allow_missing_home=True, ensure_daemon=False)


@app.command()
def logout(
    yes: bool = typer.Option(
        False, "--yes", help="Confirm removal of this home's user login."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove this home's user login without removing machine or agent keys.

    Logout is one daemon request: the daemon removes secrets/login.json plus
    the Casdoor identity fields that login wrote in settings.json. It never
    removes agent credentials, Tailcat device keys, or unrelated settings. The
    command starts the daemon when none runs; a serving daemon then returns to
    the no-account phase in the same process, and the command does not wait for
    that. A home with no user login reports not-logged-in.

    It does not revoke already-issued access tokens at the issuer.
    Such tokens can remain valid until expiry.
    """
    services = get_services()

    def operation() -> CliResult:
        from hyprial.shell.impl.cli.commands.login.logout import run_logout

        return CliResult(
            run_logout(get_services(), yes=yes, json_output=json_output)
        )

    services.runtime.execute(
        operation,
        json_output=json_output,
        allow_missing_home=True,
        ensure_daemon=False,
    )


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
        result = services.runtime.request(
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

_OWNED = "owned"
_FIRST = "first-login"
_LAUNCH = "launch-first-login"
_LAUNCH_RELOGIN = "launch-relogin"


def _login_route(services) -> str:
    """Which route ``hyprial login`` takes, decided before anything is locked.

    One ``ping`` (``_daemon_probe`` never starts a daemon) and the local owner
    decide.  A bootstrap daemon, or a serving daemon while this home has no
    owner, means the daemon runs the login: the second case reaches the
    daemon's own ``IDENTITY_CONFLICT``, which is surfaced and not re-implemented
    here.  A serving daemon with a local owner, and a home with an owner (or
    ``HYPRIAL_OWNER``, or malformed settings) and no daemon, keep the owned
    route and start nothing; so does a probe that fails transiently (timeout,
    disconnect, a ping error) while an owner is resolvable, exactly as before
    the probe existed.  With no owner such a probe error is surfaced, never
    answered by launching a second daemon.  No daemon and no owner starts the
    bootstrap daemon.  A missing home counts as ownerless unless
    ``HYPRIAL_OWNER`` is set.  This must run before the
    resume shortcut: a bootstrap daemon holds the identity transaction lock.
    """
    probe_error: Exception | None = None
    try:
        probe = services._daemon_probe(timeout=0.5)
    except ipc_errors.DaemonUnavailableError:
        probe = None
    except (
        ipc_errors.IpcTimeoutError,
        ipc_errors.DaemonDisconnectedError,
        services.CliError,
    ) as error:
        probe = None
        probe_error = error

    from hyprial.daemon import node_owner_or_none

    home = services._hyprial_home()
    if isinstance(probe, dict) and probe.get("phase") == "bootstrap":
        return _FIRST
    try:
        owner = node_owner_or_none(hyprial_home=home)
    except ValueError:
        return _OWNED
    if owner is not None:
        if probe is None and probe_error is None:
            return _LAUNCH_RELOGIN
        return _OWNED
    if probe_error is not None:
        raise probe_error
    if probe is None and not home.is_dir():
        return _LAUNCH
    return _FIRST if probe is not None else _LAUNCH


def _initialize_missing_home(services) -> None:
    """A login on a home directory that does not exist yet creates it."""
    try:
        services.require_initialized_hyprial_home()
    except HYPRIALHomeNotInitialized:
        services.initialize_hyprial_home()
        services._initialize_org_context()


def _finalize_daemon_login(
    services, response: JsonObject, *, json_output: bool
) -> JsonObject:
    daemon = response["daemon"]
    if daemon.get("running") is True:
        from hyprial.shell.impl.cli.commands.login.aftercare import (
            _pickup_org_invites_after_login,
        )

        response["pending"] = _pickup_org_invites_after_login(
            services._hyprial_home(), json_output=json_output, services=services
        )
    steps = _organization_next_steps(services)
    if steps is not None:
        response["nextSteps"] = steps
    return response


def _first_login(
    services, *, started: bool, no_open: bool, json_output: bool
) -> JsonObject:
    """The daemon-run first login, then the same follow-ups as every login."""
    from hyprial.shell.impl.cli.commands.login.first_login import run_first_login

    response = run_first_login(
        no_open=no_open, json_output=json_output, started=started
    )
    return _finalize_daemon_login(services, response, json_output=json_output)


def _serving_relogin(
    services, *, started: bool, no_open: bool, json_output: bool
) -> JsonObject:
    """The daemon-run serving relogin, then the same follow-ups as every login."""
    from hyprial.shell.impl.cli.commands.login.relogin import run_serving_relogin

    response = run_serving_relogin(
        no_open=no_open, json_output=json_output, started=started
    )
    return _finalize_daemon_login(services, response, json_output=json_output)
