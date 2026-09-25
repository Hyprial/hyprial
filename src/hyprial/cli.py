"""Command-line interface for Harness Bridge.

The CLI owns argument parsing and presentation only.  Daemon-backed commands
cross the versioned IPC boundary; they do not import daemon implementation
details.
"""

from __future__ import annotations

from hyprial.contracts.lifecycle_budgets import (
    LIFECYCLE_IPC_MARGIN_SECONDS,
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
import difflib
import errno
import fcntl
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

# Typer's Rich help renderer treats CI as a terminal and its bundled
# ``NO_COLOR`` handling only removes color styles, not all ANSI styling.  Set
# Typer's own escape hatch before importing Typer so help and errors honor the
# standard no-color contract even in CI capture.
if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
    os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

import typer

from hyprial.cli_inventory import render_top_level_help
from hyprial.log import PRE_TRAJECTORY_ARCHIVE, Logger
from hyprial.installers import install_application, upgrade_application
from hyprial.installers.core import MigrationScan, scan_app_migrations
from hyprial.identity_transaction import (
    IDENTITY_TRANSACTION_FD_ENV,
    IdentityTransactionBusy,
    IdentityTransactionLock,
)
from hyprial.home import (
    child_state_environment,
    HYPRIALHomeNotInitialized,
    configured_hyprial_home,
    default_hyprial_home,
    initialize_hyprial_home,
    require_initialized_hyprial_home,
)
from hyprial.forwarding_config import (
    ForwardingConfigurationError,
    daemon_forwarding_environment,
)
from hyprial.network_profile import (
    DEFAULT_PROFILE,
    PROFILE_FILENAME,
    NetworkProfile,
    read_profile,
    resolve_profile,
    validate_profile,
    write_profile,
)
from hyprial.routine.cli import routine_app
from hyprial.workflow.cli import workflow_app
from hyprial.contracts import ipc_errors
from hyprial.contracts.daemon_diagnostics import DAEMON_STARTUP_PHASES
from hyprial.contracts.daemon_launch import DaemonLaunchResult
from hyprial.process_diagnostics import process_cpu_seconds
from hyprial.autoupdate.alert import (
    UPGRADE_ALREADY_CURRENT,
    UPGRADE_DECLINED_DOWNGRADE,
    UPGRADE_FAILED,
    UPGRADE_INSTALLED,
    UPGRADE_AWAITING_RESTART,
    UPGRADE_UNCONFIRMED,
    RestartProcessObservation,
    RestartProcessState,
    latest_start_failure,
    notify_app_migration_required,
    notify_upgrade_failure,
    notify_upgrade_outcome,
    notify_restore_followup,
    record_alert_outcome,
    restart_failure_detail,
    run_self_check,
    start_failure_line,
    write_app_migration_marker,
    write_failure_marker,
)

# Typer's declarative API intentionally stores Argument/Option descriptors in
# function defaults.
# ruff: noqa: B008

try:  # Typer 0.27+ vendors Click; older Typer exposes the external class.
    from typer._click.exceptions import ClickException as TyperClickException
except ImportError:  # pragma: no cover - compatibility with older Typer
    from click import ClickException as TyperClickException

try:  # Abort moved out of Typer's vendored exceptions in 0.27.2.
    from typer._click.exceptions import Abort as TyperAbort
except ImportError:  # pragma: no cover - compatibility across Typer layouts
    from click import Abort as TyperAbort


JsonObject = dict[str, Any]

_INIT_READY_TIMEOUT_ENV = "HYPRIAL_INIT_READY_TIMEOUT"

app = typer.Typer(
    add_completion=False,
    help="Harness Bridge command line interface.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
adapter_app = typer.Typer(help="Manage external-platform adapters.")
daemon_app = typer.Typer(help="Run and stop the Harness daemon.")
mcp_app = typer.Typer(help="Run local MCP adapters.")
squire_app = typer.Typer(help="Configure and inspect the personal Squire agent.")
outbox_app = typer.Typer(help="Inspect and prune the durable outbox.")
delivery_app = typer.Typer(help="Ask what actually happened to messages you sent.")
autoupdate_app = typer.Typer(
    help="Inspect daemon-owned autoupdate and manage its legacy timer unit."
)
config_app = typer.Typer(
    help="Read and write operator switches in settings.json."
)
lark_auth_app = typer.Typer(
    help=(
        "Watch the lark-cli USER credential: detect expiry, walk the device "
        "flow to 'one click away', and push the link to a human."
    )
)
profile_app = typer.Typer(
    help=(
        "Inspect, create and select network profiles. One profile is one "
        "HYPRIAL_HOME; the only selection mechanism is HYPRIAL_HOME itself."
    )
)
org_app = typer.Typer(
    help=(
        "Inspect and locally adopt organization context. Adoption is a local "
        "owner decision: the mesh can only stage candidates, and only "
        "`org import <file>` fills the single accepted slot."
    )
)
agent_app = typer.Typer(
    help=(
        "Create, inspect and destroy the agents registered on this machine. "
        "An agent is an identity plus its configuration; the harness it runs "
        "on is a runtime binding, not part of the agent, so the same agent "
        "can be started on claude today and pi tomorrow."
    )
)
secret_app = typer.Typer(
    help="Manage explicit per-agent secret grants without exposing values."
)
agent_app.add_typer(secret_app, name="secret")
app.add_typer(adapter_app, name="adapter")
app.add_typer(daemon_app, name="daemon")
app.add_typer(mcp_app, name="mcp")
app.add_typer(squire_app, name="squire")
app.add_typer(outbox_app, name="outbox")
app.add_typer(delivery_app, name="delivery")
app.add_typer(autoupdate_app, name="autoupdate")
app.add_typer(config_app, name="config")
app.add_typer(lark_auth_app, name="lark-auth")
app.add_typer(agent_app, name="agent")
app.add_typer(org_app, name="org")
app.add_typer(profile_app, name="profile")
app.add_typer(workflow_app, name="workflow")
dispatch_app = typer.Typer(
    help="Dispatch capability matrix (read-only).", no_args_is_help=True
)
app.add_typer(dispatch_app, name="dispatch")
app.add_typer(routine_app, name="routine")


def _routine_identity(json_output: bool, claimed: str | None = None) -> dict[str, str]:
    from hyprial.workflow.cli import _identity
    from hyprial.pac.errors import PacError
    try:
        return _identity(json_output, claimed)
    except PacError as error:
        raise CliError(error.code, str(error)) from error


@routine_app.command("add")
def routine_add(
    file: Path | None = typer.Argument(None, help="Path to the routine.yaml to register."),
    from_identity: str | None = typer.Option(
        None, "--from", help="Registered identity owning a file-based routine."
    ),
    template: str | None = typer.Option(None, "--template", help="Built-in template name."),
    coordinator: str | None = typer.Option(None, "--for", help="Coordinator actor URI (template owner)."),
    escalate_to: str | None = typer.Option(None, "--escalate-to", help="Template escalation destination: user:<owner>."),
    name: str | None = typer.Option(None, "--name", help="Override template routine name."),
    interval: str | None = typer.Option(None, "--interval", help="Override template interval (minimum 60s)."),
    source: str | None = typer.Option(None, "--source", help="Template source: pac-journal or taskwarrior."),
    filter_expr: str | None = typer.Option(None, "--filter", help="Taskwarrior source filter."),
    idle_threshold: str | None = typer.Option(None, "--idle-threshold", help="PAC assignment idle threshold."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Register a YAML file or render and register one built-in template."""

    def operation() -> Any:
        from hyprial.routine.schema import RoutineSchemaError
        from hyprial.routine.templates import render_template

        if (file is None) == (template is None):
            raise CliError(ipc_errors.INVALID_ARGUMENT, "provide exactly one of FILE or --template")
        if template is not None:
            if from_identity is not None or coordinator is None or escalate_to is None:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "--template requires --for and --escalate-to; do not use --from",
                )
            try:
                text = render_template(
                    template, owner=coordinator, escalate_to=escalate_to,
                    name=name, interval=interval, source=source, filter_expr=filter_expr,
                    idle_threshold=idle_threshold,
                )
            except RoutineSchemaError as error:
                raise CliError("ROUTINE_SCHEMA_ERROR", str(error)) from None
            except ValueError as error:
                raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from None
        else:
            if any(
                value is not None
                for value in (coordinator, escalate_to, name, interval, source, filter_expr, idle_threshold)
            ):
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "template options require --template",
                )
            assert file is not None
            try:
                text = file.read_text(encoding="utf-8")
            except OSError as error:
                raise CliError(ipc_errors.INVALID_ARGUMENT, f"cannot read {file}: {error}") from None
        # Coordinator admission includes one shared lifecycle start.
        result = _daemon_request("routine.add", {"yaml": text, **_routine_identity(json_output, from_identity)}, timeout=60.0)
        if not isinstance(result, dict) or not isinstance(result.get("name"), str):
            raise CliError("INVALID_RESPONSE", "routine.add must return name")
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


@routine_app.command("list")
def routine_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List registered routines."""

    def operation() -> Any:
        result = _daemon_request("routine.list", _routine_identity(json_output))
        if not isinstance(result, dict) or not isinstance(result.get("routines"), list):
            raise CliError("INVALID_RESPONSE", "routine.list must return routines")
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


@routine_app.command("status")
def routine_status(
    name: str = typer.Argument(..., help="Routine name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show one routine's state, in-flight tasks, and outcome ledger."""

    def operation() -> Any:
        result = _daemon_request("routine.status", {"name": name, **_routine_identity(json_output)})
        if not isinstance(result, dict) or not isinstance(result.get("name"), str):
            raise CliError("INVALID_RESPONSE", "routine.status must return name")
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


@routine_app.command("rm")
def routine_rm(
    name: str = typer.Argument(..., help="Routine name to remove."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove a routine and its in-flight map."""

    def operation() -> Any:
        result = _daemon_request("routine.remove", {"name": name, **_routine_identity(json_output)})
        if not isinstance(result, dict) or result.get("removed") is not True:
            raise CliError(
                "INVALID_RESPONSE", "routine.remove must return removed=true"
            )
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


@routine_app.command("pause")
def routine_pause(
    name: str = typer.Argument(..., help="Routine name to pause."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Pause a routine (stops new duty cycles)."""

    def operation() -> Any:
        result = _daemon_request("routine.pause", {"name": name, **_routine_identity(json_output)})
        if not isinstance(result, dict) or result.get("enabled") is not False:
            raise CliError(
                "INVALID_RESPONSE", "routine.pause must return enabled=false"
            )
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


@routine_app.command("resume")
def routine_resume(
    name: str = typer.Argument(..., help="Routine name to resume."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Resume a paused routine with a clean breaker ledger."""

    def operation() -> Any:
        result = _daemon_request("routine.resume", {"name": name, **_routine_identity(json_output)})
        if not isinstance(result, dict) or result.get("enabled") is not True:
            raise CliError(
                "INVALID_RESPONSE", "routine.resume must return enabled=true"
            )
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


# -- network profiles (login U1) ---------------------------------------------
#
# One profile = one HYPRIAL_HOME (``~/.hyprial`` is the built-in hyprial profile; a
# home without profile.json *is* the default). These three commands never
# start a daemon, never read state, and never write settings.json.


def _require_profile_name(name: str) -> None:
    """Reject names that cannot be an ``org`` or a path segment under
    ``~/.hyprial/profiles/`` (same rule as the record's ``org`` field, plus the
    two dot-names that would escape the directory)."""

    if not name or ":" in name or "/" in name or name in {".", ".."}:
        raise CliError(
            "PROFILE_NAME_INVALID",
            f"profile name must be a non-empty string without ':' or '/' "
            f"(and not '.' or '..'); got {name!r}",
        )


def _profile_rows() -> list[JsonObject]:
    """The default home's row plus one row per ``~/.hyprial/profiles/*/`` home."""

    base = default_hyprial_home()[0]
    current, _source = configured_hyprial_home()

    def row(name: str, profile: NetworkProfile, home: Path) -> JsonObject:
        return {
            "name": name,
            "issuer": profile.issuer,
            "clientId": profile.client_id,
            "controlPlane": {
                "kind": profile.control_plane_kind,
                "url": profile.control_plane_url,
            },
            "join": profile.join,
            "home": str(home),
            "current": home.resolve() == current,
        }

    default_record = base / PROFILE_FILENAME
    if default_record.exists():
        default_profile = read_profile(default_record)
        rows = [row(default_profile.org, default_profile, base)]
    else:
        rows = [row(DEFAULT_PROFILE.org, DEFAULT_PROFILE, base)]
    profiles_root = base / "profiles"
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            record = entry / PROFILE_FILENAME
            if not record.is_file():
                continue
            rows.append(row(entry.name, read_profile(record), entry))
    return rows


@profile_app.command("list")
def profile_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List this machine's network profiles (read-only; no daemon, no state)."""

    if json_output:
        _execute(
            lambda: {"ok": True, "profiles": _profile_rows()},
            json_output=True,
            allow_missing_home=True,
        )
        return

    try:
        rows = _profile_rows()
    except Exception as error:  # noqa: BLE001 - CLI error boundary
        _fail(error, json_output=False)
    name_width = max([len("name")] + [len(str(item["name"])) for item in rows])
    home_width = max([len("home")] + [len(str(item["home"])) for item in rows])
    lines = [
        f"  {'name':<{name_width}}  issuer / control plane  {'home':<{home_width}}"
    ]
    for item in rows:
        marker = "*" if item["current"] else " "
        lines.append(
            f"{marker} {str(item['name']):<{name_width}}  "
            f"{item['issuer']}  {item['controlPlane']['kind']}:"
            f"{item['controlPlane']['url']}  {str(item['home']):<{home_width}}"
        )
    lines.append("(* = current: the home configured_hyprial_home() resolves to)")
    typer.echo("\n".join(lines))


@profile_app.command("use")
def profile_use(
    name: str = typer.Argument(..., help="Profile name under ~/.hyprial/profiles/."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Print the export that selects this profile's home (writes nothing).

    D-U1-1: use prints ``export HYPRIAL_HOME=<home>`` for
    ``eval "$(hyprial profile use <name>)"`` and deliberately persists no
    machine-level pointer — the resident daemon's home is pinned by its
    service unit, and a second selector would let the CLI and the daemon
    disagree about which home is active.
    """

    def operation() -> JsonObject:
        _require_profile_name(name)
        home = (default_hyprial_home()[0] / "profiles" / name).resolve()
        record = home / PROFILE_FILENAME
        if not record.exists():
            raise CliError(
                "PROFILE_NOT_FOUND",
                f"no profile named {name!r}: {record} does not exist. Create "
                "it with: hyprial profile create --issuer <url> --control-plane "
                "<tailscale|headscale> --url <url>",
                data={"name": name, "path": str(record)},
            )
        return {
            "ok": True,
            "home": str(home),
            "profile": read_profile(record).as_record(),
        }

    if json_output:
        _execute(operation, json_output=True, allow_missing_home=True)
        return
    # Text mode must emit exactly one eval-safe line, so bypass _emit/Pretty.
    try:
        result = operation()
    except Exception as error:  # noqa: BLE001 - CLI error boundary
        _fail(error, json_output=False)
    typer.echo(f"export HYPRIAL_HOME={result['home']}")


@profile_app.command("create")
def profile_create(
    name: str = typer.Argument(
        ..., help="Profile name — becomes the record's org and ~/.hyprial/profiles/<name>/."
    ),
    issuer: str = typer.Option(
        ...,
        "--issuer",
        help="Identity issuer URL (https://, no trailing slash; stored verbatim).",
    ),
    client_id: str = typer.Option(
        ...,
        "--client-id",
        help=(
            "Public OIDC client id for this issuer (D-U2-1: public value, not "
            "a secret; hyprial login presents it to the issuer)."
        ),
    ),
    control_plane: str = typer.Option(
        ..., "--control-plane", help="Control plane kind: tailscale or headscale."
    ),
    url: str = typer.Option(
        ...,
        "--url",
        help="Control plane URL (https://…; tailscale uses https://controlplane.tailscale.com explicitly).",
    ),
    join: str = typer.Option(
        "interactive", "--join", help="Join mode: interactive or preauthkey (unattended; either kind)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create ~/.hyprial/profiles/<name>/ and write its profile.json.

    Idempotent for an identical existing record; refuses a differing one
    (changing a profile in place is U5's switch semantics — there is no
    --force here). Never calls hyprial init, never starts a daemon, never
    writes settings.json.
    """

    def operation() -> JsonObject:
        _require_profile_name(name)
        profile = NetworkProfile(
            org=name,
            issuer=issuer,
            client_id=client_id,
            control_plane_kind=control_plane,
            control_plane_url=url,
            join=join,
        )
        home = default_hyprial_home()[0] / "profiles" / name
        record = home / PROFILE_FILENAME
        if record.exists():
            existing = read_profile(record)  # loud when the file is broken
            if existing != profile:
                raise CliError(
                    "PROFILE_EXISTS",
                    f"profile {name!r} already exists at {record} with "
                    "different values; refusing to overwrite. Changing a "
                    "profile's values is a switch (U5) and there is no "
                    "--force here.",
                    data={"name": name, "path": str(record)},
                )
            return {
                "ok": True,
                "name": name,
                "home": str(home),
                "created": False,
                "profile": profile.as_record(),
            }
        validate_profile(profile, source=record)  # loud before any mkdir
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_profile(profile, hyprial_home=home)
        return {
            "ok": True,
            "name": name,
            "home": str(home),
            "created": True,
            "profile": profile.as_record(),
        }

    _execute(operation, json_output=json_output, allow_missing_home=True)


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
            "credential/settings/state and does not stop, join, or start."
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
    preauthkey_file: str | None = typer.Option(
        None,
        "--preauthkey-file",
        help=(
            "Preauth key file for join=preauthkey profiles ('-' reads "
            "stdin). The key is never written anywhere — it goes to the "
            "sidecar's stdin only. Rejected for interactive profiles."
        ),
    ),
    join_timeout: float = typer.Option(
        300.0,
        "--join-timeout",
        min=0.001,
        help=(
            "Seconds to wait for the network join to reach ready. Default "
            "300s — five minutes of headroom over the OIDC device-code "
            "lifetime, the same budget the identity stage polls (the "
            "interactive join waits for a human click)."
        ),
    ),
    skip_join: bool = typer.Option(
        False,
        "--skip-join",
        help=(
            "Run the identity stage only; network is reported as "
            "not-attempted (for CI)."
        ),
    ),
    install_sidecar_flag: bool = typer.Option(
        False,
        "--install-sidecar",
        help=(
            "Install or upgrade the pinned hyprial-tsnet sidecar without "
            "asking y/n, then continue login. The source, tag, version, and "
            "expected sha256 are still printed before download."
        ),
    ),
    only_install_sidecar: bool = typer.Option(
        False,
        "--only-install-sidecar",
        help=(
            "Repair or upgrade the pinned hyprial-tsnet sidecar in an "
            "initialized home, then exit without profile resolution, OIDC "
            "login, or network join. This implies --install-sidecar."
        ),
    ),
    ready_timeout: float = typer.Option(
        # init's serving-boundary budget, not a second one: same default,
        # same environment variable, same Click parsing. Only a login that
        # finds the home missing starts a daemon, and then it finishes init.
        15.0,
        "--ready-timeout",
        envvar=_INIT_READY_TIMEOUT_ENV,
        hidden=True,
        help=(
            "Seconds to wait for the daemon to reach its serving boundary when "
            "login initializes a missing home (env: HYPRIAL_INIT_READY_TIMEOUT)."
        ),
    ),
) -> None:
    """Log in this home's user identity and join the overlay network.

    Before identity, a missing or out-of-date sidecar is offered using the
    pinned source/tag/sha256 consent step.  ``--install-sidecar`` accepts that
    step without y/n; ``--only-install-sidecar`` implies it and exits before
    profile resolution or identity.  Identity stage (U2): the owner lands in
    settings.json, the refresh
    token in secrets/login.json (0600).  Join stage (U3b): the installed
    $HYPRIAL_HOME/bin/hyprial-tsnet sidecar (sha256 + hello version re-verified
    every run; install it with `hyprial login --install-sidecar`) is driven over
    protocol v1
    to `ready`, landing a summary in state/tsnet/node.json.  A join
    failure never rolls the identity back (D12) — re-running retries the
    join alone.  No --provider — the control plane is
    profile.controlPlane.kind (D-U2-3).  A different existing owner is
    switched only with --switch-account. By default login stops the old
    generation, proves exit, reclassifies stopped state, commits, and starts
    and verifies the replacement. ``--no-daemon`` preserves the explicit
    identity-only path and refuses a live identity change.  A home with no
    owner yet (neither settings.owner nor HYPRIAL_OWNER; a missing home
    always counts) is first-time setup instead: login commits the identity,
    then init's shared setup and daemon-start path starts the daemon once.
    """

    if dry_run and not switch_account:
        _fail(
            CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--dry-run requires --switch-account",
                {"requiredFlag": "--switch-account"},
            ),
            json_output=json_output,
        )
    if dry_run and (install_sidecar_flag or only_install_sidecar):
        _fail(
            CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--dry-run cannot be combined with sidecar installation flags",
            ),
            json_output=json_output,
        )

    if only_install_sidecar:
        # This branch deliberately precedes resolve_profile(): an initialized
        # home with a polluted sidecar can be repaired without a profile or OIDC
        # identity.  It is not a clean-machine bootstrap path; the shared login
        # flow enforces the existing-home precondition. only-install implies the
        # y/n bypass because repair is the command's sole requested action.
        _execute(
            lambda: _run_login_cli_flow(
                no_open=no_open,
                json_output=json_output,
                switch_account=switch_account,
                preauthkey_file=preauthkey_file,
                join_timeout=join_timeout,
                skip_join=skip_join,
                install_sidecar_flag=True,
                only_install_sidecar=True,
            ),
            json_output=json_output,
            allow_missing_home=True,
        )
        return

    def operation() -> JsonObject:
        if dry_run:
            # A preview writes nothing, so it must not create a missing home.
            try:
                require_initialized_hyprial_home()
            except HYPRIALHomeNotInitialized as error:
                raise CliError(
                    error.code,
                    f"{error}; --dry-run previews an existing home's account "
                    "switch and writes nothing, so it does not create one",
                    data=error.data,
                ) from error
        if dry_run or not _login_route_is_first_time_setup():
            return _run_login_cli_flow(
                no_open=no_open,
                json_output=json_output,
                switch_account=switch_account,
                preauthkey_file=preauthkey_file,
                join_timeout=join_timeout,
                skip_join=skip_join,
                install_sidecar_flag=install_sidecar_flag,
                dry_run=dry_run,
                no_daemon=no_daemon,
            )

        try:
            require_initialized_hyprial_home()
        except HYPRIALHomeNotInitialized:
            home_was_missing = True
            initialize_hyprial_home()
            org_warning = _initialize_org_context()
        else:
            home_was_missing = False
            org_warning = None

        held_identity_transaction: list[IdentityTransactionLock] = []
        try:
            try:
                response = _run_login_cli_flow(
                    no_open=no_open,
                    json_output=json_output,
                    switch_account=switch_account,
                    preauthkey_file=preauthkey_file,
                    join_timeout=join_timeout,
                    skip_join=skip_join,
                    install_sidecar_flag=install_sidecar_flag,
                    no_daemon=True,
                    first_time_setup=True,
                    held_identity_transaction=held_identity_transaction,
                )
            except KeyboardInterrupt as error:
                if not home_was_missing:
                    raise
                raise CliError(
                    "INTERRUPTED",
                    "login was interrupted after home initialization; rerun `hyprial login`",
                    data={"nextSteps": ["hyprial login"]},
                ) from error
            except CliError as error:
                if not home_was_missing:
                    raise
                data = dict(error.data) if isinstance(error.data, dict) else {}
                data["nextSteps"] = ["hyprial login"]
                raise CliError(
                    error.code,
                    f"{error}; home is initialized, rerun `hyprial login`",
                    data=data,
                ) from error
            if not no_daemon:
                response["daemon"] = _complete_initialization(
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
            # The daemon is started first, so the identity and the daemon are
            # kept exactly as on #493's route; only the exit code reports that
            # the join the operator asked for did not happen.
            network = response.get("network")
            if (
                isinstance(network, dict)
                and network.get("status") == "failed"
                and _login_join_was_explicitly_requested(
                    skip_join=skip_join,
                    preauthkey_file=preauthkey_file,
                    network=network,
                )
            ):
                raise _login_partial_failure(response)
            return response
        finally:
            for transaction in held_identity_transaction:
                transaction.close()

    _execute(operation, json_output=json_output, allow_missing_home=True)


class CliError(Exception):
    """A stable, user-facing CLI error."""

    def __init__(self, code: str, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _hyprial_home() -> Path:
    return configured_hyprial_home()[0]


def _agent_workspace(actor: str) -> Path:
    """Return one validated local actor's default private workspace path."""

    from hyprial.agents.registry import AgentRegistry

    name = AgentRegistry.normalize_actor(actor)
    return _hyprial_home() / "agents" / name / "workspace"


def _resolved_agent_cwd(actor: str, cwd: Path | None) -> Path:
    return cwd.expanduser().resolve() if cwd is not None else _agent_workspace(actor)


def _stdin_isatty() -> bool:
    """Whether a CLI confirmation can be answered (a narrow test seam)."""

    return sys.stdin.isatty()


#: ``hyprial init``'s two identity sources for an ownerless home (U6,
#: 2026-09-18).  The fork lives at init only — ``hyprial login`` stays the
#: Hyprial-service path (Allen: 「login只在选择使用我们服务的时候需要」).
_IDENTITY_CHOICE_SERVICE = "hyprial-service"
_IDENTITY_CHOICE_SELF_HOST = "self-hosted-tailnet"


def _choose_identity_source(*, json_output: bool) -> str:
    """Ask a human where this ownerless home's identity comes from (U6).

    No default is chosen on the machine's behalf: with ``--json`` or a
    non-TTY stdin nobody can answer, and the command fails
    ``USER_ACTION_REQUIRED`` naming the exact rerun commands — the same
    shape ``adapter onboard`` uses for its unattended device flow.
    """

    if json_output or not _stdin_isatty():
        raise CliError(
            "USER_ACTION_REQUIRED",
            "this home has no user identity, and choosing its source needs a "
            "human: rerun `hyprial init` interactively to choose between the "
            "Hyprial service and a self-hosted tailscale, or run "
            "`hyprial login` directly for the Hyprial service",
            {
                "type": "choose_identity_source_interactively",
                "command": "hyprial init",
                "environment": ["HYPRIAL_OWNER"],
                "reason": (
                    "hyprial init asks whether the identity comes from the "
                    "Hyprial service or from this machine's own tailscale; "
                    "--json and non-interactive stdin cannot answer that "
                    "question"
                ),
            },
        )
    typer.echo("This home has no user identity yet. Choose where it comes from:")
    typer.echo(
        "  1) Hyprial service — log in with a Hyprial account and join the "
        "Hyprial network"
    )
    typer.echo(
        "  2) Self-hosted tailscale — use this machine's own tailscale "
        "(official or headscale); the owner is whatever `tailscale whoami` "
        "reports; no Hyprial login, sidecar, or network join"
    )
    while True:
        try:
            answer = typer.prompt("Enter 1 or 2", show_default=False)
        except typer.Abort as error:
            raise CliError(
                "INTERRUPTED",
                "identity source choice was interrupted; rerun `hyprial init` "
                "to choose again, or run `hyprial login` for the Hyprial "
                "service",
                {"nextSteps": ["hyprial login", "hyprial init"]},
            ) from error
        value = answer.strip()
        if value == "1":
            return _IDENTITY_CHOICE_SERVICE
        if value == "2":
            return _IDENTITY_CHOICE_SELF_HOST
        typer.echo("Enter 1 (Hyprial service) or 2 (self-hosted tailscale).")


def _run_selfhost_cli_flow(
    *,
    json_output: bool,
    held_identity_transaction: list[IdentityTransactionLock] | None = None,
) -> JsonObject:
    """Commit this home's identity from the host tailnet (U6 self-host branch).

    No Hyprial service is involved: no profile resolution, no OIDC, no
    sidecar, no join — the node already runs on the host's own tailscale
    (official or self-hosted headscale).  The owner is what that control
    plane asserts via ``tailscale whoami``, never manual input; the adopted
    value is printed (human mode) and recorded (JSON) so the user can see
    exactly what was adopted — including the tagged-node case where whoami
    answers with the node's DNS name.
    """

    from hyprial.daemon.identity import (
        read_settings_identity,
        write_settings_identity,
    )
    from hyprial.login import LoginError, _check_owner_gates
    from hyprial.tailnet_identity import (
        TailnetIdentityError,
        resolve_host_tailnet_identity,
    )

    try:
        identity = resolve_host_tailnet_identity()
    except TailnetIdentityError as error:
        raise CliError(
            error.code,
            str(error),
            {
                **(error.data or {}),
                "failedPhase": "identity",
                "identityCommitted": False,
            },
        ) from error

    owner = identity.login_name
    home = _hyprial_home()
    try:
        # Same gates as the service login (grammar, D7 override) so the two
        # entrances can never drift; a differing owner cannot exist on this
        # path (init only asks when no owner is resolvable), but the gate —
        # not the caller — is what guarantees that.
        _check_owner_gates(
            owner,
            environ=os.environ,
            hyprial_home=home,
            switch_account=False,
            target_mode="tailscale-selfhost",
            target_issuer=None,
        )
    except LoginError as error:
        raise CliError(error.code, str(error), data=error.data or None) from error

    def failure(next_step: str) -> JsonObject:
        return {
            "failedPhase": "prepare",
            "identityCommitted": False,
            "network": {"status": "not-attempted"},
            "daemon": {"state": "not-attempted"},
            "nextStep": next_step,
        }

    transaction: IdentityTransactionLock | None = None
    try:
        try:
            transaction = IdentityTransactionLock.acquire(home)
        except IdentityTransactionBusy as error:
            raise CliError(
                error.code,
                str(error),
                failure("wait for the active identity transaction and rerun `hyprial init`"),
            ) from error
        except OSError as error:
            raise CliError(
                "IDENTITY_TRANSACTION_FAILED",
                "cannot acquire the home identity transaction: "
                f"{type(error).__name__}",
                failure("repair home permissions and rerun `hyprial init`"),
            ) from error
        if read_settings_identity(hyprial_home=home) is not None:
            raise CliError(
                "IDENTITY_TRANSACTION_STALE",
                "settings identity appeared while the tailnet identity was "
                "being resolved",
                failure("rerun `hyprial init` against the committed identity"),
            )
        write_settings_identity(
            owner,
            mode="tailscale-selfhost",
            issuer=None,
            hyprial_home=home,
        )
    except BaseException:
        if transaction is not None:
            transaction.close()
        raise
    if held_identity_transaction is not None:
        # Same handoff as the service flow: init's start path launches the
        # daemon under this lock and closes it.
        held_identity_transaction.append(transaction)
    else:
        transaction.close()

    if not json_output:
        typer.echo(
            f"identity: owner {owner!r} adopted from the host tailnet "
            "(tailscale whoami)"
        )
        if identity.tags:
            typer.echo(
                "  node tags: "
                + ", ".join(identity.tags)
                + " (whoami on a tagged node answers with the node DNS name; "
                "adopted as-is per U6)"
            )
        elif "@" not in owner and "." in owner:
            typer.echo(
                "  note: the adopted owner looks like a node DNS name, not a "
                "user login"
            )
    return {
        "ok": True,
        "identity": {
            "status": "authenticated",
            "owner": owner,
            "mode": "tailscale-selfhost",
            "issuer": None,
            "source": "host-tailnet",
            "assertedBy": "tailscale whoami",
            "committed": True,
            "tags": list(identity.tags),
            "tailnet": identity.tailnet_name,
        },
        "network": {
            "status": "not-attempted",
            "kind": "host-tailnet",
            "controlUrl": None,
            "join": "host",
            "hostname": identity.dns_name,
            "ip4": identity.ip4,
            "user": owner,
            "reason": (
                "self-hosted: this node already runs on the host's own "
                "tailnet; no Hyprial network join"
            ),
            "warnings": [],
        },
    }


def _login_route_is_first_time_setup() -> bool:
    """Whether ``hyprial login`` takes the first-time setup route.

    Merge glue between #493 (orchestrated identity switch) and #508 (init and
    login share onboarding).  First-time setup means exactly "init would run
    login": ``node_owner_or_none()`` finds no owner in settings.json and no
    ``HYPRIAL_OWNER``.  A home directory without an owner is first-time setup
    (#508: the identity step commits, then init's start path starts the daemon
    once, under the identity step's held transaction lock).  A missing home is
    always first-time setup -- it has no settings to switch away from, and
    #508's missing-home bootstrap (``--install-sidecar`` included) creates it
    even when ``HYPRIAL_OWNER`` is set.  This is the one cell where the
    route differs from ``node_owner_or_none()`` alone (hq-adjutant approved,
    2026-09-15).  Pinned by the routing table
    ``tests/test_cli_onboarding.py::test_login_routes_first_time_setup_by_owner_not_by_home_directory``
    (cell ``home-missing-env-owner``) and end to end by
    ``tests/test_tsnet_sidecar.py::test_cli_login_install_sidecar_missing_home_initializes_before_transport``
    (``HYPRIAL_OWNER`` is set by the autouse fixture in ``tests/conftest.py``);
    removing the
    missing-home guard turns both red.
    Any owner on an existing home --
    including an older settings owner whose ``identityIssuer`` is empty, or
    only ``HYPRIAL_OWNER`` -- takes #493's path.  Malformed settings are not
    provably ownerless, so they also stay on #493's path, which reports them.
    """

    from hyprial.daemon.identity import node_owner_or_none

    home = _hyprial_home()
    if not home.is_dir():
        return True
    try:
        owner = node_owner_or_none(hyprial_home=home)
    except ValueError:
        return False
    return owner is None


def _login_join_was_explicitly_requested(
    *, skip_join: bool, preauthkey_file: str | None, network: object
) -> bool:
    """Whether the operator asked this login to join the network.

    A key file, or a ``preauthkey`` profile (whose join cannot happen without
    one), is an explicit request; ``--skip-join`` never is.  An interactive
    profile with no key is login doing onboarding's incidental join, which
    first-time setup reports as a ``NETWORK_JOIN_FAILED`` warning (#508).
    """

    if skip_join:
        return False
    join = network.get("join") if isinstance(network, dict) else None
    return preauthkey_file is not None or join == "preauthkey"


def _login_partial_failure(response: JsonObject) -> CliError:
    """#493's envelope for "identity committed, network join failed".

    One constructor for both routes, so an explicit join that fails exits 1
    with the same data whether or not the home had an owner before.
    """

    return CliError(
        "LOGIN_PARTIAL_FAILURE",
        "identity committed and daemon handled, but network join failed",
        {
            **response,
            "ok": False,
            "failedPhase": "join",
            "identityCommitted": True,
            "nextStep": "fix network enrollment and retry login; identity stays committed",
        },
    )


def _run_login_cli_flow(
    *,
    no_open: bool,
    json_output: bool,
    switch_account: bool,
    preauthkey_file: str | None,
    join_timeout: float,
    skip_join: bool,
    install_sidecar_flag: bool,
    only_install_sidecar: bool = False,
    dry_run: bool = False,
    no_daemon: bool = False,
    first_time_setup: bool = False,
    held_identity_transaction: list[IdentityTransactionLock] | None = None,
) -> JsonObject:
    """Run login's sidecar, identity, and join logic without CLI recursion."""

    from hyprial.tsnet_sidecar import install_sidecar, verify_installed_sidecar

    def acquire_sidecar(*, force: bool) -> JsonObject | None:
        """Offer the one consent-shaped acquisition step when the pin is absent.

        ``force`` means the user supplied an installation flag.  It skips only
        y/n: the full plan is still emitted before ``install_sidecar`` fetches.
        JSON keeps stdout reserved for the command result, so its plan is a
        progress event on stderr like the identity-stage device event.
        """

        # Home is created by init/login's shared initialization path. Sidecar
        # repair still requires it to exist: both normal login and only-install
        # converge here, and install_sidecar creates bin/ before fetching or
        # checking SHA.
        try:
            hyprial_home = require_initialized_hyprial_home()
        except HYPRIALHomeNotInitialized as error:
            raise CliError(
                error.code,
                f"{error}; run: hyprial init",
                data=error.data,
            ) from error
        _path, reason = verify_installed_sidecar(hyprial_home)
        if reason is None and not force:
            return None
        if reason is not None and not force and (
            json_output or not _stdin_isatty()
        ):
            # No prompt where nobody can answer it.  The unchanged join result
            # below reports SIDECAR_MISSING/MISMATCH and the explicit flag.
            return None

        def confirm(plan: JsonObject) -> bool:
            if json_output:
                sys.stderr.write(
                    json.dumps({"event": "sidecar-install-plan", **plan}) + "\n"
                )
                sys.stderr.flush()
            else:
                _emit(plan, json_output=False)
            if force:
                return True
            try:
                return typer.confirm(
                    f"Download hyprial-tsnet {plan['version']} from "
                    f"{plan['source']} (tag {plan['tag']})?"
                )
            except typer.Abort:
                # EOF and a closed stdin are declines, not command failures.
                return False

        return install_sidecar(
            hyprial_home,
            confirm=confirm,
            json_output=json_output,
        )

    if only_install_sidecar:
        return {"ok": True, "sidecar": acquire_sidecar(force=True)}

    from hyprial.login import LoginError, run_login

    profile, source = resolve_profile()
    events: list[dict[str, Any]] = []
    sidecar_result = (
        None if dry_run else acquire_sidecar(force=install_sidecar_flag)
    )

    def emit(kind: str, data: dict[str, Any]) -> None:
        # stdout in --json mode carries exactly the final result object;
        # progress events (verification URI / user code — both public,
        # never the device code or any token) go to stderr there and to
        # stdout in human mode.
        events.append({"event": kind, **data})
        if json_output:
            sys.stderr.write(json.dumps({"event": kind, **data}) + "\n")
            sys.stderr.flush()
        elif kind == "device":
            typer.echo(
                "device authorization required — open this URL and enter the code:"
            )
            typer.echo(f"  {data['verificationUri']}")
            typer.echo(f"  code: {data['userCode']}")
            typer.echo(
                "  (waiting for authorization; server interval "
                f"{data.get('interval')}s, expires in "
                f"{data.get('expiresIn')}s)"
            )
        elif kind == "authenticated":
            typer.echo(
                f"authenticated as {data['owner']} (issuer {data['issuer']})"
            )

    transaction: IdentityTransactionLock | None = None
    daemon_before: JsonObject = {"state": "unknown"}
    # No classification has run yet, so a failure report must say the
    # migration was not attempted — the earlier "not-required" default
    # read as "we checked and nothing was needed", which a stop-phase
    # failure cannot know.
    stopped_preview: JsonObject = {"status": "not-attempted"}
    state_dir = _state_dir()
    home = _hyprial_home()

    def failure_data(
        phase: str,
        *,
        committed: bool,
        network: JsonObject | None = None,
        daemon: JsonObject | None = None,
        migration: JsonObject | None = None,
        next_step: str,
    ) -> JsonObject:
        return {
            "failedPhase": phase,
            "identityCommitted": committed,
            "network": network or {"status": "not-attempted"},
            "daemon": daemon if daemon is not None else daemon_before,
            "migration": migration or stopped_preview,
            "nextStep": next_step,
        }

    def classify(
        target_owner: str, phase: str, *, allow_retry: bool
    ) -> JsonObject:
        from hyprial.login_preview import (
            LoginPreviewError,
            preview_owner_migration,
        )

        preview = None
        for attempt in range(3):
            try:
                preview = preview_owner_migration(
                    state_dir=state_dir,
                    hyprial_home=home,
                    target_owner=target_owner,
                )
                break
            except LoginPreviewError as error:
                # A read-only SQLite WAL reader may materialize its own
                # -wal/-shm pair on a first snapshot.  S2 correctly rejects
                # that attempt.  The ONLINE preview (plan-live) may retry
                # because it advances only from a later stable snapshot.
                # The STOPPED replay (plan-stopped) may not: the old
                # generation is proven gone, so a source that still
                # changes means another writer is active — refuse and
                # report instead of retrying the evidence away.
                if (
                    error.code == "PREVIEW_SOURCE_CHANGED"
                    and attempt < 2
                    and allow_retry
                ):
                    continue
                raise CliError(
                    error.code,
                    str(error),
                    failure_data(
                        phase,
                        committed=False,
                        migration={"status": "failed", **error.data},
                        next_step="inspect the named preview source; identity is unchanged",
                    ),
                ) from error
        assert preview is not None
        projection = preview.as_dict()
        if not preview.ready:
            raise CliError(
                "MIGRATION_PREVIEW_BLOCKED",
                "owner migration contains unclassified values",
                failure_data(
                    phase,
                    committed=False,
                    migration=projection,
                    next_step="classify every reported value before retrying login",
                ),
            )
        return projection

    def before_commit(
        previous_owner: str | None,
        target_owner: str,
        observed_identity: tuple[str, str | None, str | None] | None,
    ) -> None:
        nonlocal transaction, daemon_before, stopped_preview
        if previous_owner is not None:
            classify(target_owner, "plan-live", allow_retry=True)
        try:
            transaction = IdentityTransactionLock.acquire(home)
        except IdentityTransactionBusy as error:
            raise CliError(
                error.code,
                str(error),
                failure_data(
                    "prepare",
                    committed=False,
                    next_step="wait for the active identity transaction and retry",
                ),
            ) from error
        except OSError as error:
            raise CliError(
                "IDENTITY_TRANSACTION_FAILED",
                f"cannot acquire the home identity transaction: {type(error).__name__}",
                failure_data(
                    "prepare",
                    committed=False,
                    next_step="repair home permissions and retry login",
                ),
            ) from error
        try:
            from hyprial.daemon.identity import read_settings_identity

            current_identity = read_settings_identity(hyprial_home=home)
            if current_identity != observed_identity:
                raise CliError(
                    "IDENTITY_TRANSACTION_STALE",
                    "settings identity changed while this login was authenticating",
                    failure_data(
                        "prepare",
                        committed=False,
                        next_step="retry login against the newly committed identity",
                    ),
                )
            try:
                probe = _daemon_probe(timeout=0.5)
            except ipc_errors.DaemonUnavailableError:
                daemon_before = _prove_daemon_absent(home, state_dir, failure_data)
            else:
                daemon_before = {
                    "state": "running",
                    "pid": probe.get("pid"),
                    "epoch": probe.get("epoch"),
                    "owner": probe.get("owner"),
                    "identityMode": probe.get("identityMode"),
                    "identityIssuer": probe.get("identityIssuer"),
                }
                runtime_matches = (
                    probe.get("owner") == target_owner
                    and probe.get("identityMode") == "casdoor"
                    and probe.get("identityIssuer") == profile.issuer
                )
                must_stop = previous_owner is not None or not runtime_matches
                if (
                    must_stop
                    and probe.get("owner") != target_owner
                    and not switch_account
                ):
                    raise CliError(
                        "DAEMON_IDENTITY_CONFLICT",
                        "running daemon identity differs from the login candidate; "
                        "use --switch-account",
                        failure_data(
                            "stop",
                            committed=False,
                            next_step="retry with --switch-account",
                        ),
                    )
                if must_stop:
                    _stop_daemon_for_identity_switch()
                    daemon_before["state"] = "stopped"
            if previous_owner is not None:
                stopped_preview = classify(
                    target_owner, "plan-stopped", allow_retry=False
                )
        except BaseException as error:
            transaction.close()
            transaction = None
            if isinstance(error, CliError):
                details = error.data or {}
                if "failedPhase" in details:
                    raise
                raise CliError(
                    error.code,
                    str(error),
                    {
                        **details,
                        **failure_data(
                            "stop",
                            committed=False,
                            next_step="prove the old daemon generation exited, then retry",
                        ),
                    },
                ) from error
            if isinstance(error, Exception):
                raise CliError(
                    "LOGIN_ORCHESTRATION_FAILED",
                    f"identity orchestration failed: {type(error).__name__}",
                    failure_data(
                        "stop",
                        committed=False,
                        next_step="inspect the old generation and retry login",
                    ),
                ) from error
            raise

    def before_commit_no_daemon(
        previous_owner: str | None,
        target_owner: str,
        observed_identity: tuple[str, str | None, str | None] | None,
    ) -> None:
        """--no-daemon: the liveness check and the commit share one OS lock.

        login.py's heartbeat-only gate cannot see a daemon that is still
        constructing: the constructor already holds this identity
        transaction but has not claimed its heartbeat yet, so a check
        outside the lock can pass and then commit into the construction
        window, splitting the in-memory old owner from the disk new one.
        The gate is therefore repeated under the lock, and the lock is
        held until the commit writes land.
        """

        nonlocal transaction
        try:
            transaction = IdentityTransactionLock.acquire(home)
        except IdentityTransactionBusy as error:
            raise CliError(
                error.code,
                str(error),
                failure_data(
                    "prepare",
                    committed=False,
                    next_step="wait for the active identity transaction and retry",
                ),
            ) from error
        except OSError as error:
            raise CliError(
                "IDENTITY_TRANSACTION_FAILED",
                "cannot acquire the home identity transaction: "
                f"{type(error).__name__}",
                failure_data(
                    "prepare",
                    committed=False,
                    next_step="repair home permissions and retry login",
                ),
            ) from error
        try:
            from hyprial.daemon.identity import read_settings_identity

            current_identity = read_settings_identity(hyprial_home=home)
            if current_identity != observed_identity:
                raise CliError(
                    "IDENTITY_TRANSACTION_STALE",
                    "settings identity changed while this login was authenticating",
                    failure_data(
                        "prepare",
                        committed=False,
                        next_step="retry login against the newly committed identity",
                    ),
                )
            identity_changes = (
                current_identity is not None
                and current_identity
                != (target_owner, "casdoor", profile.issuer)
            )
            if identity_changes:
                from hyprial.daemon.home_guard import live_daemon_pid

                pid = live_daemon_pid(home)
                if pid is not None:
                    raise CliError(
                        "DAEMON_RUNNING",
                        "cannot change the persisted identity for "
                        f"{target_owner!r} while this home's daemon is "
                        f"running (pid {pid}): its in-memory identity "
                        "would split from disk",
                        failure_data(
                            "prepare",
                            committed=False,
                            next_step=(
                                "stop the daemon first — `hyprial daemon "
                                "stop` — or omit --no-daemon so login can "
                                "orchestrate the restart"
                            ),
                        ),
                    )
        except BaseException:
            transaction.close()
            transaction = None
            raise

    identity_committed = False
    post_commit_phase = "join"
    post_commit_network: JsonObject | None = None
    post_commit_daemon: JsonObject | None = None
    post_commit_migration: JsonObject | None = None

    try:
        try:
            result = run_login(
                profile,
                profile_source=source,
                switch_account=switch_account,
                dry_run=dry_run,
                state_dir=state_dir,
                identity_mode="casdoor",
                identity_issuer=getattr(profile, "issuer", None),
                orchestrate_daemon=not no_daemon and not dry_run,
                before_commit=(
                    None
                    if dry_run
                    else before_commit_no_daemon
                    if no_daemon
                    else before_commit
                ),
                assume_yes=json_output,
                open_browser=not no_open,
                emit=emit,
            )
        except LoginError as error:
            data = error.data or {}
            if not dry_run and "failedPhase" not in data:
                if error.code == "OWNER_WRITE_FAILED":
                    # The credential is already durable, and on the
                    # orchestrated path the old generation is already
                    # stopped; a plain "fix and retry" hides both facts.
                    stopped_note = (
                        " and the previous daemon generation is stopped"
                        if daemon_before.get("state") == "stopped"
                        else ""
                    )
                    retry_next_step = (
                        f"the credential is already written{stopped_note}; "
                        "re-run hyprial login to complete the owner write"
                    )
                else:
                    retry_next_step = "fix the reported error and retry login"
                data = {
                    **data,
                    **failure_data(
                        "commit" if transaction is not None else "prepare",
                        committed=False,
                        next_step=retry_next_step,
                    ),
                }
            raise CliError(error.code, str(error), data=data or None) from error

        identity_committed = True
        if no_daemon and not first_time_setup and transaction is not None:
            # The identity commit has landed; holding the transaction
            # through join would only block a daemon start without
            # guarding anything further.
            transaction.close()
            transaction = None

        identity: JsonObject = {
            "status": "switched" if result.switched else "authenticated",
            "owner": result.owner,
            "mode": "casdoor",
            "issuer": result.issuer,
            "source": result.profile_source,
        }
        if result.switched:
            identity["previousOwner"] = result.previous_owner
        if getattr(result, "dry_run", False):
            identity["status"] = "planned"
            identity["committed"] = False
            return {
                "ok": True,
                "identity": identity,
                "migration": getattr(result, "migration_preview", None),
                "network": {
                    "status": "not-attempted",
                    "kind": profile.control_plane_kind,
                    "controlUrl": profile.control_plane_url,
                    "join": profile.join,
                    "hostname": None,
                    "ip4": None,
                    "user": None,
                    "reason": "dry-run",
                    "warnings": [],
                },
                "daemon": {"state": "not-attempted"},
                "verificationUri": result.verification_uri,
                "userCode": result.user_code,
            }

        from hyprial.tsnet_join import JoinOutcome, default_open_url, run_join

        def joined_notify(kind: str, data: dict[str, Any]) -> None:
            events.append({"event": f"network-{kind}", **data})
            if json_output:
                sys.stderr.write(
                    json.dumps({"event": f"network-{kind}", **data}) + "\n"
                )
                sys.stderr.flush()
            elif kind == "state":
                typer.echo(f"  network: {data.get('state')}")
            elif kind == "browse_to_url":
                typer.echo("  network: open this URL to authorize the node:")
                typer.echo(f"  {data.get('url')}")
            elif kind == "joined":
                typer.echo(
                    f"  network joined: {data.get('hostname')} "
                    f"({data.get('ip4')})"
                )
            elif kind == "failed":
                message = data.get("message") or ""
                hint = f" — {data['hint']}" if data.get("hint") else ""
                suffix = f" ({message})" if message else ""
                typer.echo(
                    f"  network join failed: {data.get('reason')}{suffix}{hint}"
                )
            elif kind == "skipped":
                typer.echo("  network join skipped (--skip-join)")

        def unexpected_join_outcome(error: Exception) -> JoinOutcome:
            reason = getattr(error, "code", None)
            return JoinOutcome(
                status="failed",
                kind=profile.control_plane_kind,
                control_url=profile.control_plane_url,
                join=profile.join,
                hostname=(
                    os.environ.get("HYPRIAL_NODE_ID", "").strip()
                    or socket.gethostname().split(".", 1)[0]
                ),
                reason=reason if isinstance(reason, str) else "JOIN_FAILED",
                warnings=["unexpected join failure; inspect daemon launch logs"],
            )

        try:
            outcome = run_join(
                profile,
                preauthkey_file=preauthkey_file,
                join_timeout=join_timeout,
                skip_join=skip_join,
                open_browser=not no_open,
                emit=joined_notify,
                open_url=default_open_url,
            )
        except Exception as error:
            # Identity is already committed.  Even an unexpected join
            # implementation failure must not strand the home daemonless;
            # convert it to the typed network outcome and continue start.
            outcome = unexpected_join_outcome(error)
        try:
            network = outcome.as_network()
        except Exception as error:
            # A malformed outcome/facade is still a join failure, not a
            # licence to skip the mandatory post-commit daemon start.
            outcome = unexpected_join_outcome(error)
            network = outcome.as_network()
        post_commit_network = network
        post_commit_phase = "start"
        daemon_result: JsonObject
        migration_result = stopped_preview
        if first_time_setup:
            # Merge glue (#493 x #508): first-time setup commits identity
            # only; init's start path starts the daemon, and a join failure
            # keeps the committed identity instead of failing the login.
            daemon_result = {"state": "not-attempted"}
            migration_result = {"status": "not-required"}
        elif no_daemon:
            daemon_result = {"state": "not-attempted"}
            migration_result = {
                "status": "pending" if result.switched else "not-required"
            }
            identity["nextStep"] = (
                "start the daemon to apply and verify migration"
                if result.switched
                else "start the daemon when ready"
            )
        elif transaction is None:
            # Compatibility for protocol-shaped test stubs which predate
            # the before_commit callback (e.g. test_tsnet_sidecar's
            # _stub_login_flow models an identity-only run_login).
            # Making this fail loudly forces those shared stubs into
            # real daemon launches; review item 7 stays unfixed for that
            # reason — the real-path contract is pinned by every
            # orchestration test that requires probe/stop/launch.
            daemon_result = {"state": "not-attempted"}
        elif daemon_before.get("state") == "running" and not result.switched:
            daemon_result = dict(daemon_before)
            migration_result = {"status": "not-required"}
        else:
            try:
                launched = _launch_daemon_process(
                    ready_timeout=15.0,
                    identity_transaction=transaction,
                )
            except Exception as error:
                code = getattr(error, "code", ipc_errors.DAEMON_START_FAILED)
                if code in _CUSTODY_STARTUP_ERROR_SHAPES:
                    # #513 x #493 cross-acceptance (infra-op / hq-adjutant):
                    # the switch committed and the old generation is proven
                    # gone, but the new generation refused to start at the
                    # owner-migration custody gate.  This is a *named*
                    # outcome of the switch, not a generic startup failure:
                    # the identity stays committed (no rewind — same shape
                    # as "join failure does not rewind"), the daemon state
                    # is reported as-is, and nextStep names the same real,
                    # non-destructive first recourse the daemon's refusal
                    # message gives (charter 5e: every named command exists).
                    bounded = getattr(error, "data", None)
                    bounded = bounded if isinstance(bounded, dict) else {}
                    previous = result.previous_owner or "the previous spelling"
                    raise CliError(
                        code,
                        str(error),
                        failure_data(
                            "start",
                            committed=True,
                            network=network,
                            migration={
                                "status": "refused",
                                "reason": (
                                    "owner-migration-custody-conflict"
                                    if code
                                    == ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT
                                    else "owner-migration-custody-unreadable"
                                ),
                                **{
                                    name: bounded[name]
                                    for name, _kind in (
                                        _CUSTODY_STARTUP_ERROR_SHAPES[code]
                                    )
                                    if name in bounded
                                },
                            },
                            next_step=(
                                "the daemon refused to start at the "
                                "owner-migration custody gate; the identity "
                                "stays committed. First recourse "
                                "(non-destructive): switch the login "
                                f"identity back to {previous!r} — "
                                "`hyprial login --switch-account` — then "
                                "start under that spelling (`hyprial "
                                "daemon run`); the daemon's refusal message "
                                "(daemon-launch.log) names the grant-level "
                                "ways out and #493"
                            ),
                        ),
                    ) from error
                raise CliError(
                    code,
                    str(error),
                    failure_data(
                        "start",
                        committed=True,
                        network=network,
                        migration={"status": "unknown"},
                        next_step="inspect daemon launch diagnostics; do not roll back settings alone",
                    ),
                ) from error
            if not isinstance(launched, DaemonLaunchResult):
                raise CliError(
                    "LOGIN_VERIFY_FAILED",
                    "daemon launcher returned an invalid result contract",
                    failure_data(
                        "verify",
                        committed=True,
                        network=network,
                        daemon={"state": "unknown"},
                        migration={"status": "unknown"},
                        next_step="inspect launcher diagnostics; do not roll back settings alone",
                    ),
                )
            payload = launched.to_payload()
            daemon_result = {
                "state": "running" if launched.running else "unknown",
                "pid": launched.pid,
                "epoch": launched.epoch,
                "owner": payload.get("owner"),
                "identityMode": payload.get("identityMode"),
                "identityIssuer": payload.get("identityIssuer"),
            }
            migration_value = payload.get("migration")
            migration_result = (
                migration_value
                if isinstance(migration_value, dict)
                else {"status": "unknown"}
            )
            old_epoch = daemon_before.get("epoch")
            verified = (
                launched.running
                and payload.get("owner") == result.owner
                and payload.get("identityMode") == "casdoor"
                and payload.get("identityIssuer") == profile.issuer
                and migration_result.get("status") == "applied"
                and (not isinstance(old_epoch, str) or launched.epoch != old_epoch)
            )
            if not verified:
                raise CliError(
                    "LOGIN_VERIFY_FAILED",
                    "new daemon did not verify the committed identity and migration",
                    failure_data(
                        "verify",
                        committed=True,
                        network=network,
                        daemon=daemon_result,
                        migration=migration_result,
                        next_step="inspect the reported daemon generation; do not roll back settings alone",
                    ),
                )

        post_commit_daemon = daemon_result
        post_commit_migration = migration_result
        post_commit_phase = "verify"
        network_failed = (
            getattr(outcome, "status", network.get("status")) == "failed"
        )
        response: JsonObject = {
            "ok": first_time_setup or not network_failed,
            "identity": {**identity, "committed": True},
            "network": network,
            "daemon": daemon_result,
            "migration": migration_result,
            "verificationUri": result.verification_uri,
            "userCode": result.user_code,
        }
        if sidecar_result is not None:
            response["sidecar"] = sidecar_result
        if network_failed and not first_time_setup:
            raise _login_partial_failure(response)
        if held_identity_transaction is not None and transaction is not None:
            # Merge glue: first-time setup hands the held lock to init's
            # start path, which launches under it and then closes it.
            held_identity_transaction.append(transaction)
            transaction = None
        return response
    except KeyboardInterrupt as error:
        if not identity_committed:
            raise
        # Commit has landed, so a bare "interrupted" loses the facts an
        # operator needs: which phase was in flight and that the identity
        # stays committed.  `_execute` used to flatten this into an
        # empty INTERRUPTED via the finally below.
        raise CliError(
            "INTERRUPTED",
            "login interrupted after the identity commit",
            failure_data(
                post_commit_phase,
                committed=True,
                network=post_commit_network or {"status": "unknown"},
                daemon=post_commit_daemon,
                migration=post_commit_migration or {"status": "unknown"},
                next_step=(
                    "the identity stays committed; inspect network and "
                    "daemon state, then re-run hyprial login"
                ),
            ),
        ) from error
    finally:
        if transaction is not None:
            transaction.close()


def _prove_daemon_absent(
    home: Path,
    state_dir: Path,
    failure_data: Callable[..., JsonObject],
) -> JsonObject:
    """Fail-safe proof that no old daemon generation survives.

    A free ``daemon.lock`` proves only that the previous holder reached the
    end of ``DaemonApplication._close`` — not that the process exited.  On
    2026-08-30 teardown completed, the lock came back, and the process lived
    nine more hours holding its descendants; ``hyprial daemon stop``
    reports exactly that survivor as ``survivingPid``.  An identity commit
    therefore needs positive process evidence: the durable generation
    records name who to watch — the ``.active_daemon`` heartbeat carries
    pid + birth identity, ``daemon.json`` carries a pid — and only positive
    death (missing pid, birth-identity mismatch) counts as gone.  Alive,
    uninspectable, or malformed all land on the refuse side.

    Residual, documented rather than papered over: a generation that
    finished every cleanup step and then clung to life left no record at
    all; that window is bounded by the daemon's exit backstop and cannot be
    told apart from a clean stop from here.
    """

    def unproven(reason: str) -> CliError:
        return CliError(
            "DAEMON_STOP_UNPROVEN",
            "daemon did not answer and its exit is unproven: "
            f"{reason}; refusing an identity commit",
            failure_data(
                "stop",
                committed=False,
                next_step=(
                    "inspect the recorded daemon generation; remove stale "
                    "records only after the named pid is gone, then retry"
                ),
            ),
        )

    from hyprial.daemon.ownership import (
        DaemonOwnershipBusy,
        DaemonStateOwnershipFence,
    )

    try:
        with DaemonStateOwnershipFence.acquire(state_dir):
            pass
    except DaemonOwnershipBusy as error:
        raise unproven("its state lock is still held") from error

    from hyprial.mcp.channel import _OwnerProcessStatus, _owner_process_status

    try:
        heartbeat: Any = json.loads(
            (Path(home) / ".active_daemon").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        heartbeat = None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise unproven(
            f"the heartbeat record is unreadable ({type(error).__name__})"
        ) from error
    if heartbeat is not None:
        if not isinstance(heartbeat, dict):
            raise unproven("the heartbeat record is not a JSON object")
        heartbeat_pid = heartbeat.get("pid")
        heartbeat_identity = heartbeat.get("processIdentity")
        if (
            not isinstance(heartbeat_pid, int)
            or isinstance(heartbeat_pid, bool)
            or heartbeat_pid <= 0
            or not isinstance(heartbeat_identity, str)
            or not heartbeat_identity
        ):
            raise unproven(
                "the heartbeat record names no usable pid/birth identity"
            )
        status = _owner_process_status(heartbeat_pid, heartbeat_identity)
        if status not in {
            _OwnerProcessStatus.PID_MISSING,
            _OwnerProcessStatus.IDENTITY_MISMATCH,
        }:
            raise unproven(
                f"the heartbeat record names pid {heartbeat_pid} and that "
                "process is still alive or cannot be positively reaped"
            )

    try:
        marker: Any = json.loads(
            (Path(state_dir) / "daemon.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        marker = None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise unproven(
            f"the daemon state marker is unreadable ({type(error).__name__})"
        ) from error
    if marker is not None:
        marker_pid = marker.get("pid") if isinstance(marker, dict) else None
        if (
            not isinstance(marker_pid, int)
            or isinstance(marker_pid, bool)
            or marker_pid <= 0
        ):
            raise unproven("the daemon state marker names no usable pid")
        try:
            os.kill(marker_pid, 0)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError) as error:
            raise unproven(
                f"the state marker names pid {marker_pid}, which cannot be "
                "inspected"
            ) from error
        else:
            raise unproven(
                f"the state marker names pid {marker_pid} and a process is "
                "still there"
            )
    return {"state": "stopped"}


def _state_dir() -> Path:
    """State root: ``HARNESS_STATE_DIR`` when set, else ``<HYPRIAL_HOME>/state``.

    Both variables are isolation boundaries and the repo's own isolated
    layouts set them as siblings (``root/home`` + ``root/state``: the unit
    conftest's autouse fixture, 47 test files, the E2E ``IsolatedDaemon``),
    so "state dir outside the home" is a first-class shape, not a conflict.
    Do not rule precedence by value: the two values alone cannot tell that
    layout from a child that named its own ``HYPRIAL_HOME`` while inheriting a
    parent's ``HARNESS_STATE_DIR`` (card 85dd41e2; a "home wins when they
    disagree" rule turned 45 tests red on 2026-09-05).  That leak is closed on
    the exporting side instead: ``child_state_environment`` hands a child
    ``HARNESS_STATE_DIR`` only when the state really lives outside
    ``<home>/state``, so an inherited environment usually carries no state
    dir for an explicit ``HYPRIAL_HOME`` to lose against.
    """
    configured = os.environ.get("HARNESS_STATE_DIR")
    return (
        Path(configured).expanduser().resolve() if configured else _hyprial_home() / "state"
    )


def _endpoint_args(raw: str) -> tuple[str, ...]:
    """Parse a CLI endpoint flag value; an empty string clears the side."""

    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _socket_path() -> Path:
    # An explicit state/home root is an isolation boundary.  Agent runtimes may
    # inject HARNESS_SOCKET_PATH for their own production daemon; allowing it
    # to override a test-owned state root would silently escape isolation.
    if "HARNESS_STATE_DIR" in os.environ or "HYPRIAL_HOME" in os.environ:
        return _state_dir() / "daemon.sock"
    configured = os.environ.get("HARNESS_SOCKET_PATH")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else _state_dir() / "daemon.sock"
    )


def _json_failure(error: Exception) -> JsonObject:
    # PR #332 F4②: a surfaced transient renders exactly like the CliError it
    # used to be -- ok:false with its code (the runner classifies on it).
    if isinstance(
        error, (CliError, HYPRIALHomeNotInitialized, ipc_errors.TransientDaemonError)
    ) or (
        isinstance(getattr(error, "code", None), str)
        and isinstance(getattr(error, "data", None), dict)
    ):
        result: JsonObject = {
            "ok": False,
            "code": str(getattr(error, "code")),
            "error": str(error),
        }
        data = getattr(error, "data", None)
        if data is not None:
            result["data"] = data
        return result
    return {"ok": False, "error": str(error)}


def _emit(value: Any, *, json_output: bool, json_indent: int | None = None) -> None:
    if json_output:
        # Deliberately bypass Rich: --json stdout is exactly one JSON value and
        # Rich must remain completely silent, including on error paths.
        if json_indent is None:
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            rendered = json.dumps(value, ensure_ascii=False, indent=json_indent)
        sys.stdout.write(rendered + "\n")
        return
    from rich.console import Console
    from rich.pretty import Pretty

    console = Console()
    if isinstance(value, str):
        console.print(value)
    else:
        console.print(Pretty(value, expand_all=True))


def _fail(error: Exception, *, json_output: bool) -> NoReturn:
    if json_output:
        _emit(_json_failure(error), json_output=True)
    else:
        from rich.console import Console

        Console(stderr=True).print(f"[bold red]hyprial:[/bold red] {error}")
    raise typer.Exit(code=1)


def _execute(
    operation: Callable[[], Any],
    *,
    json_output: bool,
    json_indent: int | None = None,
    allow_missing_home: bool = False,
) -> None:
    try:
        if not allow_missing_home:
            require_initialized_hyprial_home()
        result = operation()
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - CLI error boundary
        if isinstance(error, KeyboardInterrupt):
            error = CliError("INTERRUPTED", "operation interrupted")
        _fail(error, json_output=json_output)
    _emit(result, json_output=json_output, json_indent=json_indent)


_PEER_GONE_ERRNOS = frozenset(
    {
        # EPIPE: the peer is gone at write time (macOS).  ECONNRESET: the
        # peer is gone at write OR read time (Linux).  Same fact, different
        # platforms -- discriminate by errno, not by exception class, so the
        # next platform cannot leak a third spelling.
        errno.EPIPE,
        errno.ECONNRESET,
    }
)

_CONNECT_BACKLOG_ERRNOS = frozenset(
    {
        # Linux answers connect() to a full AF_UNIX accept queue with an
        # immediate EAGAIN -- even a timeout socket never waits for a slot
        # there (CPython surfaces that EAGAIN as BlockingIOError after its
        # one select() round), so a patient caller must retry within its
        # budget or a healthy request looks starved whenever the dispatcher
        # is briefly slower than its clients.  EWOULDBLOCK is the same value
        # on POSIX but is listed so the intent survives a platform where the
        # two diverge.
        errno.EAGAIN,
        errno.EWOULDBLOCK,
    }
)

# Retry cadence while waiting out a full listen backlog.  Each failed
# attempt returns instantly, so this is the effective poll interval; it
# stays well below the millisecond granularity of any caller timeout.
_CONNECT_BACKLOG_RETRY_INTERVAL = 0.01


def _connect_daemon_socket(socket_path: Path, timeout: float) -> socket.socket:
    """Connect to the daemon, waiting out a momentarily full listen backlog.

    The retry budget is the request's own ``timeout``: the kernel only parks
    a connect() against a full AF_UNIX accept queue for fully blocking
    sockets, and this caller always runs with a timeout, so patience has to
    be explicit.  Only the full-backlog spelling (EAGAIN/EWOULDBLOCK) is
    retried -- ECONNREFUSED still fails fast because it also describes a
    stale socket file whose daemon is gone, which must not take the whole
    budget to report.
    """

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                client.connect(str(socket_path))
            except OSError as error:
                if (
                    error.errno in _CONNECT_BACKLOG_ERRNOS
                    and time.monotonic() < deadline
                ):
                    time.sleep(_CONNECT_BACKLOG_RETRY_INTERVAL)
                    continue
                raise
            return client
    except BaseException:
        client.close()
        raise


def _peer_gone(
    socket_path: Path, error: OSError | None = None
) -> ipc_errors.DaemonDisconnectedError:
    detail = f" ({error})" if error is not None else ""
    # PR #332 F4②: transport mints use the registered transient classes so
    # every daemon-request failure is catchable as TransientDaemonError
    # without a second string-comparison system.
    return ipc_errors.DaemonDisconnectedError(
        f"connection closed by the Harness daemon at {socket_path} "
        f"before any answer{detail}"
    )


#: Poll cadence of the DAEMON_RESTORING wait loop (F4①).  A refused frame
#: costs the daemon one dispatch rejection, so a quarter-second cadence is
#: cheap for the whole wait budget.
_RESTORE_WAIT_POLL_SECONDS = 0.25
_RESTORE_WAIT_DEFAULT_SECONDS = 120.0
_RESTORE_WAIT_MAX_SECONDS = 900.0

#: Upper bound for one ordinary daemon IPC round trip: connect, request write,
#: handler work, response encoding, and response read. Long-running handlers
#: compose their own work budget with this transport/settlement remainder.
_DAEMON_IPC_ROUNDTRIP_SECONDS = 15.0

#: Cadence of the post-restart restore poll (card 3c116ad2 P2).  The daemon
#: is busy restoring while this runs, so the cadence stays gentle against
#: its socket -- one light ping, never a burst.  Tests shrink it through the
#: function parameter, not by editing this constant.
_RESTORE_FOLLOWUP_POLL_INTERVAL_SECONDS = 2.0


def _restore_wait_budget() -> float:
    """Bounded seconds a user command waits out a DAEMON_RESTORING refusal.

    PR #332 F4①: while the daemon's restore gate is closed, heavy methods
    answer DAEMON_RESTORING at dispatch -- BEFORE any side effect -- so a
    refused mutation never executed and waiting the refusal out is safe.
    The default covers a derived restore round (F1:
    (ceil(targets/width)+1) x per-start timeout) for typical fleets;
    HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS=0 restores the fail-fast behaviour for
    scripts that prefer it.
    """

    raw = os.environ.get("HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS")
    try:
        value = _RESTORE_WAIT_DEFAULT_SECONDS if raw is None else float(raw)
    except ValueError:
        return _RESTORE_WAIT_DEFAULT_SECONDS
    if not math.isfinite(value) or not 0.0 <= value <= _RESTORE_WAIT_MAX_SECONDS:
        return _RESTORE_WAIT_DEFAULT_SECONDS
    return value


def _daemon_request(
    method: str,
    params: JsonObject | None = None,
    *,
    timeout: float = _DAEMON_IPC_ROUNDTRIP_SECONDS,
    restore_wait: float | None = None,
) -> Any:
    """Call the daemon's version-1 newline-delimited JSON IPC protocol.

    ``restore_wait`` bounds how long a DAEMON_RESTORING refusal is waited
    out before it surfaces as a CliError (None: the
    HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS budget, 0: fail fast).  Callers that
    answer the refusal with their own readiness projection (``ps``) or run
    in best-effort branches pass ``restore_wait=0.0`` so they never sit
    out the budget just to swallow or re-render the same error.
    """

    budget = _restore_wait_budget() if restore_wait is None else restore_wait
    deadline = time.monotonic() + budget
    while True:
        try:
            return _daemon_request_once(method, params, timeout=timeout)
        # PR #332 F4②: the restore-wait judgment is the registered class,
        # not a code comparison.  The other transient classes (transport
        # blips) propagate untouched, exactly as the old ``code !=
        # DAEMON_RESTORING`` raise did.
        except ipc_errors.DaemonRestoringError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_RESTORE_WAIT_POLL_SECONDS)


def _daemon_request_once(
    method: str,
    params: JsonObject | None = None,
    *,
    timeout: float = _DAEMON_IPC_ROUNDTRIP_SECONDS,
) -> Any:
    """Call the daemon's version-1 newline-delimited JSON IPC protocol."""

    request_id = str(uuid4())
    frame: JsonObject = {"version": 1, "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    client: socket.socket | None = None
    try:
        try:
            client = _connect_daemon_socket(_socket_path(), timeout)
        except OSError as error:
            # Report only what happened: which socket, and why the connect
            # failed.  The cause (never started / mid-restart / hijacked
            # HYPRIAL_HOME / permissions) cannot be told apart from here, so no
            # remedy is embedded -- guidance belongs to skills and docs,
            # which can be updated; a string constant cannot.
            raise ipc_errors.DaemonUnavailableError(
                f"cannot connect to the Harness daemon socket "
                f"{_socket_path()}: {error}"
            ) from error
        try:
            client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        except OSError as error:
            if error.errno in _PEER_GONE_ERRNOS:
                raise _peer_gone(_socket_path(), error) from error
            raise
        buffer = bytearray()
        while len(buffer) <= 8 * 1024 * 1024:
            try:
                chunk = client.recv(64 * 1024)
            except OSError as error:
                if error.errno in _PEER_GONE_ERRNOS:
                    raise _peer_gone(_socket_path(), error) from error
                raise
            if not chunk:
                raise _peer_gone(_socket_path())
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not line.strip():
                    continue
                try:
                    response = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise CliError(
                        "INVALID_JSON", f"invalid daemon IPC response: {error}"
                    ) from error
                if not isinstance(response, dict) or response.get("version") != 1:
                    raise CliError(
                        ipc_errors.VERSION_MISMATCH, "unsupported daemon IPC version"
                    )
                if response.get("id") != request_id:
                    # Event frames and responses for other request IDs are not
                    # the result of this one-shot request.
                    continue
                failure = response.get("error")
                if isinstance(failure, dict):
                    code = str(failure.get("code", ipc_errors.DAEMON_ERROR))
                    message = str(failure.get("message", "daemon request failed"))
                    # PR #332 F4②: transient envelope codes deserialise
                    # through the shared registry into the SAME class the
                    # daemon minted; every other code stays a CliError (the
                    # CLI's user-facing surface).
                    transient = ipc_errors.transient_error_from_code(
                        code, message, failure.get("data")
                    )
                    if transient is not None:
                        raise transient
                    raise CliError(
                        code,
                        message,
                        failure.get("data"),
                    )
                if "result" not in response:
                    raise CliError(
                        "INVALID_RESPONSE", "daemon response is missing result"
                    )
                return response["result"]
        raise CliError("IPC_RESPONSE_TOO_LARGE", "daemon IPC response exceeded 8 MiB")
    except TimeoutError as error:
        raise ipc_errors.IpcTimeoutError(
            f"timed out after {timeout:g}s waiting for the Harness daemon at "
            f"{_socket_path()} to answer method {method!r}",
        ) from error
    finally:
        if client is not None:
            client.close()


def _wait_for_daemon(
    *, timeout: float, process: subprocess.Popen[bytes] | None = None
) -> JsonObject:
    """Wait for the phase-① boundary: daemon.json for our pid, then a ping.

    daemon.json is written when the daemon can serve -- socket bound, accept
    running, ping answerable -- not after restore, so this returns as soon as
    the daemon is answering instead of after every connector has come back.
    Restore progress is carried by ping's ``phase``/``restorePending`` fields
    for whoever needs it; readiness of the daemon itself is a constant.
    """

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise CliError(
                ipc_errors.DAEMON_START_FAILED,
                f"daemon exited during startup with status {process.returncode}",
            )
        if process is not None and not _daemon_ready_for_process(process.pid):
            # The pid check is still the stale-client fence: an old
            # generation's socket must not pass this generation's wait.
            time.sleep(0.05)
            continue
        try:
            # One probe path (E2E-010 on intg, "unknown daemon method ping"):
            # `_daemon_probe` answers for every daemon generation -- ping, or
            # ps for a peer older than the ping contract -- and this wait must
            # not be the one place that forgets the fallback.
            result = _daemon_probe(timeout=0.5)
        # PR #332 F4②: the boot-wait retry set is spelled as the registered
        # classes -- the same three transport codes the old literal set held;
        # DAEMON_RESTORING deliberately stays out (ping is in the restore
        # gate's light set, and the restore budget lives in
        # ``_daemon_request``).
        except (
            ipc_errors.DaemonUnavailableError,
            ipc_errors.DaemonDisconnectedError,
            ipc_errors.IpcTimeoutError,
        ) as error:
            last_error = error
            time.sleep(0.05)
            continue
        except CliError as error:
            last_error = error
            raise
        if _probe_reports_running(result):
            return result
        last_error = CliError("INVALID_RESPONSE", "daemon did not report ready")
        time.sleep(0.05)
    raise CliError(
        ipc_errors.DAEMON_START_TIMEOUT,
        f"daemon did not become ready within {timeout:g}s"
        + (f" ({last_error})" if last_error is not None else ""),
    )


def _daemon_ready_for_process(pid: int) -> bool:
    try:
        marker = json.loads((_state_dir() / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and marker.get("pid") == pid


def _daemon_probe(timeout: float = 0.5) -> JsonObject:
    """One readiness probe that works against any daemon generation.

    ``ping`` is the light method every daemon from this build on answers,
    including mid-restore.  A daemon older than that contract answers
    METHOD_NOT_FOUND, and for exactly that peer we fall back to ``ps`` --
    heavy, but the only readiness answer it has.  The fallback keeps `hyprial
    init`/`service` truthful during a rolling upgrade instead of failing
    against a daemon that is merely old.
    """

    try:
        result = _daemon_request("ping", timeout=timeout)
    except CliError as error:
        if error.code != ipc_errors.METHOD_NOT_FOUND:
            raise
        result = _daemon_request("ps", timeout=timeout)
    if not isinstance(result, dict):
        raise CliError("INVALID_RESPONSE", "daemon probe result must be an object")
    return result


def _probe_reports_running(probe: JsonObject) -> bool:
    """Normalise the two probe shapes: ping is flat, the legacy ps nests."""

    if probe.get("running") is True:
        return True
    daemon = probe.get("daemon")
    return isinstance(daemon, dict) and daemon.get("running") is True


_SAFE_DAEMON_STARTUP_EVENTS = frozenset(
    {
        # The two halves of startup recovery, and they travel together:
        # adapters are the Lark specs, harnesses are everything else. Listing
        # one without the other makes the launch summary report that adapters
        # came back while saying nothing about the connectors -- which is the
        # shape of the outage that made this event exist.
        "adapter.recovery.completed",
        "harness.recovery.completed",
        # ...and the failure names of the same story. This allowlist was born
        # success-only, so a daemon that died at the ipc-server step reported
        # `daemonEvents: []` -- byte-identical to a daemon that never logged.
        # The daemon now mirrors these two to stderr as JSON envelopes (see
        # `_mirror_startup_event_to_stderr`), and the summary's name-only
        # filter admits exactly these names: level alone never admits an
        # event, so the set stays closed.
        "daemon.start.failed",
        # One persisted interactive session can conflict with an already-live
        # connector during actor-runtime recovery.  The daemon stays ready,
        # while this event names the isolated agent and the refusal reason.
        "agent.recovery.failed",
        "agent.recovery.cleaned",
        "harness.recovery.failed",
        "daemon.ready",
        "daemon.stopping",
        "service.recovery.completed",
        # Candidate forwarding starts while `hyprial init` is still waiting for
        # daemon readiness.  These three events name that startup failure;
        # omitting them makes the launch summary indistinguishable from a
        # daemon that emitted no diagnosis at all.
        "zenoh.forwarding.exited",
        "zenoh.forwarding.failed",
        "zenoh.forwarding.start_failed",
        "zenoh.endpoints.unset",
        "workflow.remote_unavailable",
        # Degraded workflow startup (#708): the daemon stays up with the
        # workflow/routine capabilities disabled, and these name why.  Without
        # them the launch summary shows a healthy start while dispatch is off.
        "workflow.cutover_failed",
        "workflow.recovery_unavailable",
        "workflow.pac_actor_unavailable",
        "workflow.degrade_cleanup_failed",
        "routine.recovery_unavailable",
    }
)

_DAEMON_LOG_READ_BYTES = 64 * 1024


def _daemon_startup_phase_summary(
    state_dir: Path, *, spawned_at: datetime
) -> JsonObject:
    """Read only this launch's bounded startup phase names from daemon.jsonl."""

    result: JsonObject = {"lastStartupPhase": None, "phasesSeen": 0}
    path = state_dir / "logs" / "daemon.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            start = max(0, end - _DAEMON_LOG_READ_BYTES)
            stream.seek(start)
            raw = stream.read(_DAEMON_LOG_READ_BYTES)
        if start:
            _, separator, raw = raw.partition(b"\n")
            if not separator:
                return result
    except OSError:
        return result

    phases_seen = 0
    last_phase: str | None = None
    for raw_line in raw.splitlines():
        try:
            entry = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(entry, dict) or entry.get("event") != "daemon.start.begin":
            continue
        timestamp = entry.get("ts")
        if not isinstance(timestamp, str):
            continue
        try:
            observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if observed_at.tzinfo is None or observed_at < spawned_at:
            continue
        # Saturation keeps even corrupted or adversarial logs finite while
        # preserving the useful distinction between none, some, and more than
        # the daemon's closed set of phases.
        phases_seen = min(phases_seen + 1, len(DAEMON_STARTUP_PHASES) + 1)
        phase = entry.get("phase")
        last_phase = (
            phase
            if isinstance(phase, str) and phase in DAEMON_STARTUP_PHASES
            else "unknown"
        )
    result["lastStartupPhase"] = last_phase
    result["phasesSeen"] = phases_seen
    return result


def _daemon_startup_failure_evidence(
    process: subprocess.Popen[bytes],
    *,
    state_dir: Path,
    spawned_at: datetime,
    started_monotonic: float,
) -> JsonObject:
    """Take one post-wait snapshot for a failed daemon launch."""

    status = process.poll()
    alive = status is None
    # One probe only.  process_cpu_seconds uses one /proc read on Linux or one
    # one-second-capped ps invocation on macOS; it never waits on or signals
    # the daemon child and returns None if the child vanishes or probing fails.
    cpu_seconds = process_cpu_seconds(process.pid) if alive else None
    phase = _daemon_startup_phase_summary(state_dir, spawned_at=spawned_at)
    return {
        **phase,
        "elapsedSeconds": round(max(0.0, time.monotonic() - started_monotonic), 6),
        "child": {
            "alive": alive,
            "exitCode": None if alive else status,
            "cpuSeconds": cpu_seconds,
        },
    }


# Named startup refusals the launch log is allowed to surface (#513): the
# daemon child's ``--json`` failure line carries these codes with bounded,
# daemon-minted data; the launcher maps them to CliErrors with the same code
# so the login orchestration can report the named switch outcome.  Each entry
# names the fields (and their types) that may cross this boundary — free text
# from the log never does.
_CUSTODY_STARTUP_ERROR_SHAPES: dict[str, tuple[tuple[str, type], ...]] = {
    ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT: (
        ("old", str),
        ("new", str),
        ("grants", int),
        ("homes", int),
    ),
    ipc_errors.OWNER_MIGRATION_CUSTODY_UNREADABLE: (
        ("database", str),
        ("table", str),
        ("errorType", str),
    ),
}


def _daemon_launch_log_summary(
    stream: Any, *, marker: bytes, offset: int
) -> JsonObject:
    """Classify one launch log without returning attacker-controlled text."""

    unavailable: JsonObject = {
        "logAvailable": False,
        "daemonEvents": [],
        "recentLog": None,
    }

    try:
        stream.flush()
        end = os.fstat(stream.fileno()).st_size
        if end <= offset:
            return unavailable
        stream.seek(0)
        if stream.read(len(marker)) != marker:
            # Truncation or replacement invalidates the birth boundary. Never
            # surface bytes whose ownership can no longer be proven.
            return unavailable
        start = max(offset, end - 16 * 1024)
        stream.seek(start)
        raw = stream.read(end - start)
        if start > offset:
            # The inspection window starts inside an arbitrary record. Only
            # complete records can contribute an allow-listed category.
            _, separator, raw = raw.partition(b"\n")
            if not separator:
                raw = b""
    except (OSError, ValueError):
        return unavailable

    events: list[str] = []
    startup_error: JsonObject | None = None
    for raw_line in raw.splitlines():
        try:
            entry = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(entry, dict):
            continue
        event = entry.get("event")
        if (
            isinstance(event, str)
            and event in _SAFE_DAEMON_STARTUP_EVENTS
            and event not in events
        ):
            events.append(event)
        if entry.get("code") == ipc_errors.HYPRIAL_HOME_IN_USE:
            data = entry.get("data")
            if (
                isinstance(data, dict)
                and isinstance(data.get("path"), str)
                and isinstance(data.get("pid"), int)
                and data["pid"] > 0
            ):
                startup_error = {
                    "code": ipc_errors.HYPRIAL_HOME_IN_USE,
                    "data": {"path": data["path"], "pid": data["pid"]},
                }
        # The startup owner-migration custody gate refuses with a named
        # code (#513): the daemon child's ``--json`` failure line carries
        # it, and the launcher / login orchestration branch on it to report
        # the named switch outcome.  Only the bounded, daemon-minted fields
        # named in the shape table are copied — never free text from the log.
        shape = _CUSTODY_STARTUP_ERROR_SHAPES.get(entry.get("code"))
        if shape is not None:
            data = entry.get("data")
            if isinstance(data, dict) and all(
                isinstance(data.get(name), kind) and not isinstance(
                    data.get(name), bool
                )
                for name, kind in shape
            ):
                startup_error = {
                    "code": entry["code"],
                    "data": {name: data[name] for name, _kind in shape},
                }
    summary: JsonObject = {
        "logAvailable": True,
        "daemonEvents": events,
        # Additive compatibility field: arbitrary daemon output is never
        # copied into CLI JSON, even after attempted redaction.
        "recentLog": None,
    }
    if startup_error is not None:
        summary["startupError"] = startup_error
    return summary


def _daemon_launch_capture(state_dir: Path) -> tuple[Any, Path, bytes, int]:
    """Create a per-launch inode and atomically expose its compatibility path."""

    nonce = uuid4().hex
    capture_path = state_dir / f".daemon-launch.{nonce}.log"
    compatibility_path = state_dir / "daemon-launch.log"
    temporary_link = state_dir / f".daemon-launch-link.{nonce}"
    stream = capture_path.open("x+b", buffering=0)
    os.chmod(capture_path, 0o600)
    marker = f"hyprial-launch-boundary:{nonce}\n".encode()
    try:
        stream.write(marker)
        offset = stream.tell()
        os.link(capture_path, temporary_link)
        os.replace(temporary_link, compatibility_path)
    except BaseException:
        stream.close()
        temporary_link.unlink(missing_ok=True)
        capture_path.unlink(missing_ok=True)
        raise
    return stream, capture_path, marker, offset


def _launch_process_error(
    error: CliError,
    process: subprocess.Popen[bytes],
    process_identity: str | None,
) -> CliError:
    """Keep the child birth handle off the public CLI error payload."""

    setattr(error, "_daemon_process_pid", process.pid)
    setattr(error, "_daemon_process_identity", process_identity)
    setattr(error, "_daemon_process_exited", process.poll() is not None)
    return error


def _launch_daemon_process(
    *,
    ready_timeout: float,
    listen: str | None = None,
    connect: str | None = None,
    identity_transaction: IdentityTransactionLock | None = None,
) -> DaemonLaunchResult:
    """Probe and spawn under the home identity transaction OS lock.

    The delegated locked body preserves the one-launch endpoint overrides:
    ``environment["HYPRIAL_ZENOH_LISTEN"] = listen`` and
    ``environment["HYPRIAL_ZENOH_CONNECT"] = connect``.  It also derives the
    child variables with ``daemon_forwarding_environment(...)`` and then
    ``environment.update(forwarding_environment)`` before spawn.
    """

    if identity_transaction is not None:
        return _launch_daemon_process_locked(
            ready_timeout=ready_timeout,
            listen=listen,
            connect=connect,
            identity_transaction=identity_transaction,
        )
    try:
        transaction = IdentityTransactionLock.acquire(_hyprial_home())
    except IdentityTransactionBusy as error:
        raise CliError(error.code, str(error)) from error
    with transaction:
        return _launch_daemon_process_locked(
            ready_timeout=ready_timeout,
            listen=listen,
            connect=connect,
            identity_transaction=transaction,
        )


def _launch_daemon_process_locked(
    *,
    ready_timeout: float,
    listen: str | None,
    connect: str | None,
    identity_transaction: IdentityTransactionLock,
) -> DaemonLaunchResult:
    """Start ``daemon run`` detached from the invoking command's pipes.

    The child writes only to a daemon-owned regular file and starts a new
    session.  Keeping this as the one launch primitive lets ``init`` and the
    post-upgrade restart share the macOS-safe path instead of spawning a
    captured ``hyprial init`` whose child could inherit a short-lived pipe.

    Returns the typed ``DaemonLaunchResult`` (F2): ``init``, ``upgrade`` and
    the e2e runner all read that one type; the flat JSON form it carries is
    what ``hyprial init --json`` emits.
    """

    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = state_dir / "daemon-launch.log"
    lock_path = state_dir / "daemon-launch.lock"
    with lock_path.open("a+b") as launch_lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(launch_lock.fileno(), fcntl.LOCK_EX)
        # Two launchers can both miss an optimistic readiness probe.  Recheck
        # under the lock so only one daemon generation is created.
        try:
            status = _daemon_probe(timeout=0.5)
        except ipc_errors.DaemonUnavailableError:
            pass
        else:
            if listen is not None or connect is not None:
                raise CliError(
                    "DAEMON_ALREADY_RUNNING",
                    "stop the running daemon before changing zenoh endpoints",
                )
            return DaemonLaunchResult.existing(status)

        # Endpoints are NOT persisted (Allen 2026-08-28). A machine's own
        # address is not hyprial's to remember: written to desired-state it
        # survives the machine changing address, silently, which is the
        # constant we just deleted from the onboarding document relocated
        # into every node's own state file.
        #
        # Durable configuration belongs in whatever launches the daemon --
        # launchd unit, systemd, shell -- via HYPRIAL_ZENOH_LISTEN /
        # HYPRIAL_ZENOH_CONNECT, which are re-read every start and therefore
        # cannot go stale.
        if listen is not None or connect is not None:
            typer.echo(
                "note: --listen/--connect apply to THIS launch only and are "
                "no longer persisted. To make them durable, set "
                "HYPRIAL_ZENOH_LISTEN / HYPRIAL_ZENOH_CONNECT in the environment that "
                "starts the daemon (launchd/systemd/shell) -- that is where "
                "machine-specific configuration belongs, and it is re-read "
                "on every start rather than remembered and going stale.",
                err=True,
            )

        log_stream, capture_path, marker, launch_log_offset = _daemon_launch_capture(
            state_dir
        )
        try:
            environment = dict(os.environ)
            # --listen/--connect are sugar for a one-launch environment
            # override, which is the whole of their meaning now that nothing
            # is persisted: the daemon child inherits this environment, and
            # `_zenoh_endpoints` reads exactly these two variables.
            #
            # Without this the flags would be silently inert -- desired-state
            # used to be their only route to the daemon, so removing
            # persistence alone turns them into a no-op that still prints a
            # reassuring note.
            if listen is not None:
                environment["HYPRIAL_ZENOH_LISTEN"] = listen
            if connect is not None:
                environment["HYPRIAL_ZENOH_CONNECT"] = connect
            try:
                forwarding_environment = daemon_forwarding_environment(
                    _hyprial_home(),
                    environment,
                    node_id=(
                        environment.get("HYPRIAL_NODE_ID", "").strip()
                        or socket.gethostname().strip()
                    ),
                )
            except ForwardingConfigurationError as error:
                raise CliError(error.code, str(error)) from error
            environment.update(forwarding_environment)
            # These markers select the scheduler child path only.  A restarted
            # daemon inherits every operator variable (especially PATH), but
            # must not mistake itself for the one-shot updater child.
            from hyprial.autoupdate import (
                AUTOUPDATE_CHILD_ENV,
                AUTOUPDATE_TRIGGER_ENV,
            )

            environment.pop(AUTOUPDATE_CHILD_ENV, None)
            environment.pop(AUTOUPDATE_TRIGGER_ENV, None)
            environment[IDENTITY_TRANSACTION_FD_ENV] = str(
                identity_transaction.fileno
            )
            spawned_at = datetime.now(UTC)
            started_monotonic = time.monotonic()
            process = subprocess.Popen(
                [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                close_fds=True,
                pass_fds=(identity_transaction.fileno,),
                start_new_session=True,
                env=environment,
            )
            try:
                status = _wait_for_daemon(timeout=ready_timeout, process=process)
            except CliError as error:
                # Capture the birth fence only after the unchanged readiness
                # wait ends. Reading it before the wait could add a bounded ps
                # probe in front of the 15-second budget and silently lengthen
                # the contract this change must leave untouched.
                from hyprial.mcp.channel import _read_process_identity

                process_identity = _read_process_identity(process.pid)
                if error.code not in {
                    ipc_errors.DAEMON_START_FAILED,
                    ipc_errors.DAEMON_START_TIMEOUT,
                }:
                    raise
                diagnostics = _daemon_launch_log_summary(
                    log_stream,
                    marker=marker,
                    offset=launch_log_offset,
                )
                startup_evidence = _daemon_startup_failure_evidence(
                    process,
                    state_dir=state_dir,
                    spawned_at=spawned_at,
                    started_monotonic=started_monotonic,
                )
                startup_error = diagnostics.pop("startupError", None)
                if (
                    isinstance(startup_error, dict)
                    and startup_error.get("code") == ipc_errors.HYPRIAL_HOME_IN_USE
                ):
                    data = startup_error.get("data")
                    if isinstance(data, dict):
                        path = data.get("path")
                        pid = data.get("pid")
                        if isinstance(path, str) and isinstance(pid, int):
                            launch_error = CliError(
                                ipc_errors.HYPRIAL_HOME_IN_USE,
                                f"HYPRIAL home {path} is being used by another "
                                f"daemon (pid {pid})",
                                data,
                            )
                            raise _launch_process_error(
                                launch_error, process, process_identity
                            ) from error
                if (
                    isinstance(startup_error, dict)
                    and startup_error.get("code")
                    in _CUSTODY_STARTUP_ERROR_SHAPES
                ):
                    # #513: the daemon refused to start at the owner-migration
                    # custody gate.  The named code crosses the boundary so the
                    # login orchestration reports the named switch outcome; the
                    # operator-facing full way out stays where the daemon wrote
                    # it — daemon-launch.log, which outlives the capture.
                    custody_code = str(startup_error["code"])
                    custody_data = startup_error.get("data")
                    bounded = (
                        dict(custody_data) if isinstance(custody_data, dict) else {}
                    )
                    if custody_code == ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT:
                        launch_error = CliError(
                            custody_code,
                            "daemon refused to start: owner migration custody "
                            "conflict — state under "
                            f"{bounded.get('old')!r} holds "
                            f"{bounded.get('grants')} live secret grant(s); the "
                            "daemon's refusal message in daemon-launch.log "
                            "names the full way out",
                            {
                                "phase": "startup",
                                "errorType": "custodyRefusal",
                                "exitCode": process.returncode,
                                "logPath": str(log_path),
                                **bounded,
                                **diagnostics,
                            },
                        )
                    else:
                        launch_error = CliError(
                            custody_code,
                            "daemon refused to start: owner-migration custody "
                            "state unreadable "
                            f"({bounded.get('table')} in {bounded.get('database')}: "
                            f"{bounded.get('errorType')}); the daemon's refusal "
                            "message in daemon-launch.log names the way out",
                            {
                                "phase": "startup",
                                "errorType": "custodyRefusal",
                                "exitCode": process.returncode,
                                "logPath": str(log_path),
                                **bounded,
                                **diagnostics,
                            },
                        )
                    raise _launch_process_error(
                        launch_error, process, process_identity
                    ) from error
                if (
                    error.code == ipc_errors.DAEMON_START_TIMEOUT
                    and startup_evidence["child"]["alive"] is True
                ):
                    # ① never arrived: the process is alive but did not bind
                    # and answer within the budget.  That is a startup
                    # failure, not a slow restore -- restore is outside this
                    # wait by construction.  The process is still left alone:
                    # killing what we cannot explain is how survivors are
                    # made, so the pid goes into the report instead.
                    launch_error = CliError(
                        error.code,
                        f"{error}; the daemon process (pid {process.pid}) "
                        "was still running when the wait ended",
                        {
                            "phase": "startup",
                            "errorType": "servingTimeout",
                            "pid": process.pid,
                            "logPath": str(log_path),
                            **diagnostics,
                            **startup_evidence,
                        },
                    )
                    raise _launch_process_error(
                        launch_error, process, process_identity
                    ) from error
                launch_error = CliError(
                    error.code,
                    str(error),
                    {
                        "phase": "startup",
                        "errorType": "processExit",
                        "exitCode": process.returncode,
                        "logPath": str(log_path),
                        **diagnostics,
                        **startup_evidence,
                    },
                )
                raise _launch_process_error(
                    launch_error, process, process_identity
                ) from error
        finally:
            log_stream.close()
            capture_path.unlink(missing_ok=True)

        # The readiness answer (``status``) is the daemon's flat ping
        # payload; the old code spread it by hand and carried a `zenoh`
        # override that was provably dead (the variable was only ever
        # None here) -- the type builds the same flat JSON form without
        # either.
        return DaemonLaunchResult.launched(status)


def _semantic_version(value: str) -> str:
    """Translate the installed PEP 440 development version to SemVer."""

    translated = re.sub(r"\.dev(\d+)$", r"-dev.\1", value)
    translated = re.sub(r"(a|b|rc)(\d+)$", r"-\1.\2", translated)
    if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", translated):
        return translated
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value)
    return ".".join(match.groups()) if match else "0.0.0-dev.0"


def _version_result() -> JsonObject:
    """Local-only identity fields; remote tag probing lives in upgrade."""

    from hyprial import __version__
    from hyprial import updates

    installation = updates.read_installation()
    installed = installation.version or __version__
    local = _semantic_version(installed)
    url = updates.installation_git_url(installation)
    result: JsonObject = {
        "ok": True,
        "packageVersion": installed,
        "localVersion": local,
        "registry": url,
    }
    warning = updates.retired_track_warning(_hyprial_home())
    if warning is not None:
        result["warning"] = warning
    return result


RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _parse_boundary(flag: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    if not RFC3339.fullmatch(value):
        raise CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"{flag} requires an RFC 3339 timestamp with a timezone",
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise CliError(
            ipc_errors.INVALID_ARGUMENT, f"{flag} requires a valid RFC 3339 timestamp"
        ) from error


def _entry_timestamp(entry: JsonObject) -> datetime | None:
    raw = entry.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_log_entry(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _entry_timestamp(value) is not None
        and value.get("level") in {"debug", "info", "warn", "error"}
        and isinstance(value.get("component"), str)
        and isinstance(value.get("event"), str)
    )


def _read_log_history() -> tuple[list[JsonObject], int]:
    entries: list[JsonObject] = []
    skipped = 0
    logs_dir = _state_dir() / "logs"
    files = sorted(logs_dir.rglob("*")) if logs_dir.is_dir() else []
    for path in files:
        if logs_dir / PRE_TRAJECTORY_ARCHIVE in path.parents:
            continue
        if not path.is_file() or re.search(r"\.jsonl(?:\.\d+)?$", path.name) is None:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            skipped += 1
            continue
        for line in lines:
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not _is_log_entry(entry):
                skipped += 1
                continue
            entries.append(entry)
    entries.sort(
        key=lambda entry: _entry_timestamp(entry) or datetime.min.replace(tzinfo=UTC)
    )
    return entries, skipped


def _log_result(
    *,
    component: str | None,
    name: str | None,
    level: str | None,
    actor: str | None,
    conversation: str | None,
    since: str | None,
    until: str | None,
    correlation_id: str | None,
) -> JsonObject:
    if level is not None and level not in {"debug", "info", "warn", "error"}:
        raise CliError(
            ipc_errors.INVALID_ARGUMENT, "--level must be debug, info, warn, or error"
        )
    since_at = _parse_boundary("--since", since)
    until_at = _parse_boundary("--until", until)
    if since_at is not None and until_at is not None and since_at > until_at:
        raise CliError(
            ipc_errors.INVALID_ARGUMENT, "--since must not be later than --until"
        )

    history, skipped = _read_log_history()
    entries: list[JsonObject] = []
    for entry in history:
        entry_at = _entry_timestamp(entry)
        assert entry_at is not None
        actor_matches = (
            actor is None
            or any(
                entry.get(field) == actor
                for field in ("sender", "target", "recipient", "actorId", "actor")
            )
            or entry.get("route") == f"delivered:{actor}"
        )
        if (
            (component is None or entry.get("component") == component)
            and (name is None or entry.get("name") == name)
            and (level is None or entry.get("level") == level)
            and actor_matches
            and (conversation is None or entry.get("conversationId") == conversation)
            and (correlation_id is None or entry.get("correlationId") == correlation_id)
            and (since_at is None or entry_at >= since_at)
            and (until_at is None or entry_at <= until_at)
        ):
            entries.append(entry)
    return {"ok": True, "entries": entries, "skippedLines": skipped}


_AGENT_TIMELINE_EVENTS = frozenset(
    {
        "agent.harness.handover",
        "agent.binding.superseded",
        "worker.started",
        "worker.ready",
        "worker.exited",
        "worker.stopped",
        "session.registered",
        "session.unregistered",
    }
)

_TRAJECTORY_LOG_EVENTS = frozenset(
    {
        "adapter.inbound",
        "send.received",
        "send.target_unresolved",
        "wake.signalled",
        "wake.busy",
        "wake.offline",
        "wake.failed",
        "worker.turn.started",
        "worker.turn.completed",
        "worker.turn.failed",
        "worker.turn.interrupted",
        "outbox.pruned",
        "inbox.pruned",
    }
)


def _is_trajectory_log_entry(entry: JsonObject) -> bool:
    """Validate the minimum persisted message-path contract before projection.

    ``_is_log_entry`` intentionally validates only the shared logger envelope
    so ``hyprial log`` can inspect heterogeneous component history.  This stricter
    check is data-corruption defense for trajectory projection, not a legacy
    format adapter.
    """

    event = entry.get("event")
    if event not in _TRAJECTORY_LOG_EVENTS:
        return False
    required = ("name", "messageId", "correlationId", "node")
    if not all(
        isinstance(entry.get(field), str) and entry[field] for field in required
    ):
        return False
    return entry["messageId"] == entry["correlationId"]


def _trajectory_ordering() -> JsonObject:
    return {
        "key": "ts",
        "authoritative": False,
        "detail": "cross-node timestamps are display order only",
    }


def _trajectory_event_state(
    entry: JsonObject, trajectory_entries: Sequence[JsonObject]
) -> str:
    event = str(entry["event"])
    if event == "worker.turn.started":
        terminal = {
            "worker.turn.completed",
            "worker.turn.failed",
            "worker.turn.interrupted",
        }
        entry_at = _entry_timestamp(entry)
        for candidate in trajectory_entries:
            if candidate.get("event") not in terminal:
                continue
            if candidate.get("name") != entry.get("name"):
                continue
            candidate_at = _entry_timestamp(candidate)
            if entry_at is None or candidate_at is None or candidate_at >= entry_at:
                return "completed"
        return "running"
    return "completed"


def _trajectory_node(
    entry: JsonObject, trajectory_entries: Sequence[JsonObject]
) -> JsonObject:
    event = str(entry["event"])
    return {
        "ts": entry["ts"],
        "node": entry["node"],
        "component": entry["component"],
        "name": entry["name"],
        "event": event,
        "state": _trajectory_event_state(entry, trajectory_entries),
        "messageId": entry["messageId"],
        "correlationId": entry["correlationId"],
        "source": "log",
        "details": {
            key: value
            for key, value in entry.items()
            if key
            not in {
                "ts",
                "level",
                "component",
                "name",
                "event",
                "messageId",
                "correlationId",
                "node",
            }
        },
    }


def _status_timestamp(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    raw = record.get("recordedAtMs")
    if not isinstance(raw, int):
        return None
    return (
        datetime.fromtimestamp(raw / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _trajectory_result(message_id: str) -> JsonObject:
    history, skipped = _read_log_history()
    trajectory_entries: list[JsonObject] = []
    for entry in history:
        if entry.get("event") not in _TRAJECTORY_LOG_EVENTS:
            continue
        if not _is_trajectory_log_entry(entry):
            skipped += 1
            continue
        if entry.get("correlationId") == message_id:
            trajectory_entries.append(entry)
    base: JsonObject = {
        "schemaVersion": 1,
        "ok": True,
        "found": bool(trajectory_entries),
        "messageId": message_id,
        "nodes": [],
        "agentTimeline": [],
        "skippedLines": skipped,
        "ordering": _trajectory_ordering(),
    }
    if not trajectory_entries:
        base["message"] = "本机日志无此消息"
        return base

    actors = {
        value
        for entry in trajectory_entries
        for field in ("sender", "target", "recipient", "actorId", "actor")
        if isinstance((value := entry.get(field)), str) and value
    }
    sender = next(
        (
            value
            for entry in trajectory_entries
            if isinstance((value := entry.get("sender")), str) and value
        ),
        None,
    )
    nodes = [
        _trajectory_node(entry, trajectory_entries) for entry in trajectory_entries
    ]

    status: JsonObject | None = None
    status_error: str | None = None
    if sender is None:
        status_error = "sender unavailable in local trajectory events"
    else:
        try:
            value = _daemon_request(
                "message.status",
                {"from": sender, "messageId": message_id},
                restore_wait=0.0,
            )
            if isinstance(value, dict):
                status = value
            else:
                status_error = "message.status returned a non-object"
        except (
            CliError,
            ConnectionError,
            OSError,
            RuntimeError,
            TimeoutError,
        ) as error:
            status_error = str(error)

    if status is not None and status.get("messageId") not in {None, message_id}:
        status_error = (
            "message.status returned a different messageId: "
            f"{status.get('messageId')!r}"
        )
        status = None
    status_value = status.get("state") if status is not None else "unknown"
    if status_value not in {
        "fetched",
        "expired",
        "pending",
        "unknown",
        "unconfirmed",
    }:
        status_error = f"unsupported message.status state: {status_value!r}"
        status_value = "unknown"
    status_state = (
        "completed"
        if status_value in {"fetched", "expired"}
        else "running"
        if status_value == "pending"
        else "unknown"
    )
    raw_records = status.get("records", []) if status is not None else []
    records = raw_records if isinstance(raw_records, list) else []
    selected_record = next(
        (
            record
            for record in records
            if isinstance(record, dict) and record.get("messageId") == message_id
        ),
        None,
    )
    if records and selected_record is None:
        status_error = "message.status records did not contain the requested message"
        status_value = "unknown"
        status_state = "unknown"
    nodes.append(
        {
            "ts": _status_timestamp(selected_record),
            "node": "delivery-terminal",
            "component": "message-status",
            "name": (
                selected_record.get("holder", "query")
                if isinstance(selected_record, dict)
                else "query"
            ),
            "event": f"message.status.{status_value}",
            "state": status_state,
            "messageId": message_id,
            "correlationId": message_id,
            "source": "message-status",
            "details": {
                **({"record": selected_record} if selected_record is not None else {}),
                **(
                    {"diagnostics": status.get("diagnostics")}
                    if status is not None and "diagnostics" in status
                    else {}
                ),
                **({"error": status_error} if status_error is not None else {}),
            },
        }
    )
    nodes.sort(
        key=lambda node: (
            node["ts"] is None,
            _entry_timestamp({"ts": node["ts"]})
            if node["ts"] is not None
            else datetime.max.replace(tzinfo=UTC),
            str(node["event"]),
        )
    )
    base["nodes"] = nodes

    dated_nodes = [
        parsed
        for node in nodes
        if isinstance(node.get("ts"), str)
        and (parsed := _entry_timestamp({"ts": node["ts"]})) is not None
    ]
    if dated_nodes:
        started_at = min(dated_nodes)
        ended_at = datetime.now(UTC) if status_state == "running" else max(dated_nodes)
        from hyprial.uri import short_actor_name

        actor_names = {short_actor_name(actor) for actor in actors}
        base["agentTimeline"] = [
            entry
            for entry in history
            if entry.get("event") in _AGENT_TIMELINE_EVENTS
            and (entry_at := _entry_timestamp(entry)) is not None
            and started_at <= entry_at <= ended_at
            and (
                any(entry.get(field) in actors for field in ("actor", "actorId"))
                or entry.get("name") in actor_names
            )
        ]
    return base


def _format_trajectory(result: JsonObject) -> str:
    skipped = result.get("skippedLines", 0)
    skipped = skipped if isinstance(skipped, int) and skipped >= 0 else 0
    if result.get("found") is not True:
        lines = [f"本机日志无此消息：{result.get('messageId', 'unknown')}"]
        lines.append(f"skipped {skipped} malformed lines")
        return "\n".join(lines)
    lines = [f"Trajectory {result.get('messageId', 'unknown')}"]
    nodes = result.get("nodes", [])
    if not isinstance(nodes, list):
        nodes = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        timestamp = node.get("ts") or "—"
        lines.append(
            f"{timestamp} [{node.get('state', 'unknown')}] "
            f"{node.get('node', 'unknown')} {node.get('event', 'unknown')} "
            f"({node.get('component', 'unknown')})"
        )
    timeline = result.get("agentTimeline", [])
    if isinstance(timeline, list) and timeline:
        lines.append("Agent timeline:")
        for event in timeline:
            if not isinstance(event, dict):
                continue
            lines.append(
                f"{event.get('ts', '—')} [annotation] "
                f"{event.get('event', 'unknown')} ({event.get('component', 'unknown')})"
            )
    lines.append("Note: cross-node timestamps are display order only.")
    lines.append(f"skipped {skipped} malformed lines")
    return "\n".join(lines)


def _doctor_result() -> JsonObject:
    checks: list[JsonObject] = []
    try:
        result = _daemon_request("ps", timeout=2.0, restore_wait=0.0)
        running = (
            isinstance(result, dict)
            and isinstance(result.get("daemon"), dict)
            and result["daemon"].get("running") is True
        )
        if running:
            checks.append(
                {"name": "daemon", "status": "ok", "detail": "daemon IPC is available"}
            )
            checks.append(_zenoh_doctor_check(result))
            duplicate_check = _duplicate_instance_doctor_check(result)
            if duplicate_check is not None:
                checks.append(duplicate_check)
            dsh_check = _dsh_doctor_check(result)
            if dsh_check is not None:
                checks.append(dsh_check)
            lark_check = _lark_inbound_doctor_check(result)
            if lark_check is not None:
                checks.append(lark_check)
            channel_check = _mcp_channel_doctor_check(result)
            if channel_check is not None:
                checks.append(channel_check)
            historical_inbox_check = _historical_inbox_doctor_check(result)
            if historical_inbox_check is not None:
                checks.append(historical_inbox_check)
            routine_check = _routine_health_doctor_check()
            if routine_check is not None:
                checks.append(routine_check)
        else:
            checks.append(
                {
                    "name": "daemon",
                    "status": "fail",
                    "detail": "daemon did not report a running process",
                    "action": {
                        "command": "hyprial init",
                        "description": "Start the Harness daemon.",
                    },
                }
            )
    except (CliError, ipc_errors.TransientDaemonError) as error:
        checks.append(
            {
                "name": "daemon",
                "status": "fail",
                "detail": str(error),
                "action": {
                    "command": "hyprial init",
                    "description": "Start the Harness daemon.",
                },
            }
        )
    summary = {
        status: sum(item["status"] == status for item in checks)
        for status in ("ok", "warn", "fail")
    }
    return {
        "ok": summary["fail"] == 0,
        "schemaVersion": 1,
        "checks": checks,
        "summary": summary,
    }


def _routine_health_doctor_check() -> JsonObject | None:
    """Surface quarantined routines and address migrations (2026-09-14).

    A routine whose stored spec no longer validates (e.g. a legacy bare-name
    ``escalate_to`` with no unique roster match) is quarantined: it stops
    scheduling and refuses resume until the spec is fixed. That must be
    visible from ``hyprial doctor`` -- the incident this fixes was fifteen
    days of silence. The address-migration ledger is reported as metrics so
    automatic rewrites are auditable without paging anyone.
    """

    try:
        audit = _daemon_request("routine.audit", {}, timeout=2.0)
    except (CliError, ipc_errors.TransientDaemonError):
        return None
    if not isinstance(audit, dict):
        return None
    quarantined = [
        item
        for item in audit.get("quarantined", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    migrations = audit.get("addressMigrations", [])
    migration_list = (
        [
            {
                "routine": m["routine"],
                "field": m["field"],
                "before": m["before"],
                "after": m["after"],
            }
            for m in migrations
            if isinstance(m, dict)
        ]
        if isinstance(migrations, list)
        else []
    )
    if not quarantined and not migration_list:
        return None
    check: JsonObject = {
        "name": "routine-addresses",
        "status": "warn" if quarantined else "ok",
        "detail": (
            f"{len(quarantined)} routine(s) quarantined for schema faults "
            f"(scheduling stopped, resume refused); {len(migration_list)} stored "
            "address migration(s) applied automatically, resolved against THIS "
            "machine's agents roster (confirm each rewrite is the intended "
            "recipient — a same-name agent on another machine would have been "
            "redirected silently)"
        ),
        "metrics": {
            "quarantined": [item["name"] for item in quarantined],
            "addressMigrations": migration_list,
        },
    }
    if quarantined:
        check["action"] = {
            "command": "hyprial routine status <name> --json",
            "description": (
                "Fix the quarantined spec (full agent URI / route: / user: "
                "addresses only), then remove and re-add the routine."
            ),
        }
    return check


def _zenoh_doctor_check(result: JsonObject) -> JsonObject:
    """Check the running daemon's explicit Zenoh transport endpoints.

    Discovery is disabled by design, so a daemon with neither a listen nor a
    connect endpoint is deterministically isolated from every other node.
    That is a hard failure (not a warning): the node cannot send or receive
    across the mesh no matter what.
    """

    zenoh = result.get("zenoh")
    if not isinstance(zenoh, dict):
        return {
            "name": "zenoh",
            "status": "warn",
            "detail": "daemon did not report Zenoh endpoints; upgrade the daemon",
        }
    listen = zenoh.get("listen")
    connect = zenoh.get("connect")
    if not isinstance(listen, list) or not isinstance(connect, list):
        listen = connect = ()
    if not listen and not connect:
        return {
            "name": "zenoh",
            "status": "fail",
            "detail": (
                "no explicit Zenoh listen/connect endpoints are configured and "
                "auto-discovery is disabled; this node cannot reach any other "
                "node (configuration check: no endpoints means no peers by "
                "construction)"
            ),
            "action": {
                "command": (
                    "hyprial init --listen tcp/<this-host>:<port> "
                    "--connect tcp/<peer-host>:<port>"
                ),
                "description": "Configure explicit Zenoh endpoints, then restart the daemon.",
            },
        }
    return {
        "name": "zenoh",
        "status": "ok",
        "detail": (
            "explicit Zenoh endpoints are configured; configured is not "
            "reachable -- actual peer connectivity must be proven by a real "
            "send/ack"
        ),
        "metrics": {"listen": list(listen), "connect": list(connect)},
    }


def _duplicate_instance_doctor_check(result: JsonObject) -> JsonObject | None:
    """Surface the daemon's duplicate-instance verdict.

    Returns None against a daemon old enough to not report the field -- the
    check's absence is then the honest signal, exactly like the other
    version-gated checks.  The detail repeats the coverage limit from the
    daemon's payload: mesh detection sees only peers that declare a
    generation liveliness token, so a green check must never be read as
    "no duplicate exists anywhere".
    """

    info = result.get("duplicateInstance")
    if not isinstance(info, dict):
        return None
    coverage = info.get("meshDetectionCoverage")
    coverage_note = f" Coverage: {coverage}" if isinstance(coverage, str) else ""
    if info.get("active") is not True:
        return {
            "name": "duplicate-instance",
            "status": "ok",
            "detail": (
                "no duplicate daemon instance of this node identity detected."
                + coverage_note
            ),
        }
    peers = info.get("meshPeerGenerations")
    startup = info.get("startupRecord")
    sources: list[str] = []
    if isinstance(peers, list) and peers:
        sources.append(f"mesh peer generations: {', '.join(str(p) for p in peers)}")
    if isinstance(startup, dict):
        sources.append(
            "startup copied-home detection: "
            f"record pid {startup.get('recordPid')} "
            f"generation {startup.get('recordGeneration')}"
        )
    return {
        "name": "duplicate-instance",
        "status": "fail",
        "detail": (
            "a duplicate live daemon instance of this node identity was "
            f"detected ({'; '.join(sources)}); detection and alarm only -- "
            "no process is killed or taken offline automatically."
            + coverage_note
        ),
        "action": {
            "command": "hyprial ps --json",
            "description": (
                "Inspect duplicateInstance, find the second daemon process "
                "(a copied HYPRIAL_HOME is the known cause), and stop it by "
                "PID. Automatic remediation is deliberately not performed."
            ),
        },
    }


def _dsh_host_describe(endpoint: str, *, timeout_seconds: float = 2.0) -> object:
    """Actively probe one DSH endpoint through its public HTTP API."""

    import asyncio
    import threading

    from hyprial.harnesses.dsh import DshHttpApi

    api = DshHttpApi(endpoint, timeout_seconds=timeout_seconds)
    # wait_for alone cannot stop to_thread socket/DNS I/O. The transport's
    # close fence aborts both, including a server that trickles response bytes.
    deadline = threading.Timer(timeout_seconds, api.close)
    deadline.daemon = True
    deadline.start()
    try:
        return asyncio.run(api.call("host.describe", {}))
    finally:
        deadline.cancel()
        api.close()


def _dsh_endpoint_label(endpoint: str) -> str:
    """Diagnostics never print credentials, query strings or deployment paths."""

    from urllib.parse import urlparse

    try:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "[invalid endpoint]"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        path = "/[redacted-path]" if parsed.path.strip("/") else ""
        return f"{parsed.scheme}://{host}{port}{path}"
    except ValueError:
        return "[invalid endpoint]"


def _dsh_doctor_check(result: JsonObject) -> JsonObject | None:
    """Probe every self-launched DSH endpoint reported by status.

    The endpoint is an output of the worker's current generation, so a worker
    without one has not readied a child yet; liveness alone is not evidence
    that its ``/api`` works.
    """

    raw_connectors = result.get("connectors")
    if not isinstance(raw_connectors, list):
        return None
    connectors = [
        item
        for item in raw_connectors
        if isinstance(item, dict) and item.get("runtime") == "dsh"
    ]
    if not connectors:
        return None

    missing = sorted(
        str(item.get("name"))
        for item in connectors
        if not isinstance(item.get("endpoint"), str) or not item.get("endpoint")
    )
    endpoints = sorted({
        item["endpoint"].rstrip("/") for item in connectors
        if isinstance(item.get("endpoint"), str) and item["endpoint"]
    })
    labels = {endpoint: _dsh_endpoint_label(endpoint) for endpoint in endpoints}
    unreachable: dict[str, str] = {}
    not_ready: list[str] = []
    unprobed: list[str] = []
    # A large roster must not multiply the command's network wait by N.
    deadline = time.monotonic() + 4.0
    for endpoint in endpoints:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            unprobed.append(endpoint)
            continue
        try:
            description = _dsh_host_describe(endpoint, timeout_seconds=min(2.0, remaining))
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            # HTTP error bodies and exception strings can contain credentials.
            unreachable[endpoint] = type(error).__name__
            continue
        if (
            not isinstance(description, dict)
            or not isinstance(description.get("provider"), str)
            or not description["provider"].strip()
            or not isinstance(description.get("model"), str)
            or not description["model"].strip()
        ):
            not_ready.append(endpoint)

    metrics: JsonObject = {
        "endpoints": [labels[e] for e in endpoints],
        "reachable": [labels[e] for e in endpoints if e not in unreachable and e not in unprobed],
        "unreachable": [labels[e] for e in sorted(unreachable)],
        "missingModelConfiguration": [labels[e] for e in not_ready],
        "unprobed": [labels[e] for e in unprobed],
        "missingEndpoint": missing,
    }
    action = {
        "command": "hyprial doctor --json",
        "description": (
            "Inspect the worker's self-launched DSH child (its status endpoint "
            "and dshHome, plus the child io log) and model configuration, then "
            "rerun the probe."
        ),
    }
    if unreachable:
        details = "; ".join(
            f"{labels[endpoint]}: {unreachable[endpoint]}" for endpoint in sorted(unreachable)
        )
        return {
            "name": "dsh-endpoint",
            "status": "fail",
            "detail": f"DSH endpoint unreachable: {details}",
            "action": action,
            "metrics": metrics,
        }
    if not_ready:
        return {
            "name": "dsh-endpoint",
            "status": "fail",
            "detail": (
                "DSH endpoint is reachable but host.describe did not expose a "
                f"configured model route: {', '.join(labels[e] for e in not_ready)}"
            ),
            "action": action,
            "metrics": metrics,
        }
    if missing or unprobed:
        return {
            "name": "dsh-endpoint",
            "status": "warn",
            "detail": (
                "DSH probe incomplete: a worker has no live self-launched "
                "generation reporting an endpoint, or the total probe budget "
                "was exhausted"
            ),
            "action": action,
            "metrics": metrics,
        }
    return {
        "name": "dsh-endpoint",
        "status": "ok",
        "detail": "DSH host.describe reachable with model configuration; inference, credentials and quota were not tested",
        "metrics": metrics,
    }


def _historical_inbox_doctor_check(result: JsonObject) -> JsonObject | None:
    """Warn when same-name messages remain under another node identity."""

    raw_entries = result.get("historicalInboxRecipients")
    if not isinstance(raw_entries, list) or not raw_entries:
        return None
    entries = [
        item
        for item in raw_entries
        if isinstance(item, dict)
        and isinstance(item.get("recipient"), str)
        and isinstance(item.get("currentRecipient"), str)
        and isinstance(item.get("pending"), int)
        and not isinstance(item.get("pending"), bool)
        and item["pending"] > 0
    ]
    if not entries:
        return None
    return {
        "name": "historical-inbox-recipients",
        "status": "warn",
        "detail": (
            "pending messages use same-name agent URIs from another node id; "
            "identity continuity is not proven, so hyprial will not consume them "
            "through the current actor automatically"
        ),
        "action": {
            "command": "hyprial ps --json",
            "description": (
                "Review historicalInboxRecipients, then use the exact old URI "
                "with read/ack only after confirming that the node rename was "
                "intentional."
            ),
        },
        "metrics": {
            "recipients": len(entries),
            "pending": sum(int(item["pending"]) for item in entries),
            "historicalRecipients": [item["recipient"] for item in entries],
            "currentRecipients": [item["currentRecipient"] for item in entries],
        },
    }


def _mcp_channel_doctor_check(result: JsonObject) -> JsonObject | None:
    """Report code generation/fencing, never infer stdio process liveness."""

    from hyprial.contracts.channel import CHANNEL_PROTOCOL_VERSION, channel_generation

    raw_sessions = result.get("interactiveSessions")
    if not isinstance(raw_sessions, list):
        return None
    channels = [
        item
        for item in raw_sessions
        if isinstance(item, dict)
        and (
            item.get("source") == "claude-channel"
            or item.get("runtime") == "claude_interactive"
        )
    ]
    if not channels:
        return None
    generations = [
        channel_generation(
            build_version=item.get("channelBuildVersion"),
            protocol_version=item.get("channelProtocolVersion"),
        )
        for item in channels
    ]
    legacy = generations.count("legacy_or_unknown")
    unfenced = sum(item.get("ownerFence") is not True for item in channels)
    confirmed = sum(
        item.get("channelCurrentThisGeneration") is True
        and isinstance(item.get("channelCurrentEpoch"), str)
        for item in channels
    )
    confirmed_current = sum(
        generation == "current"
        and item.get("channelCurrentThisGeneration") is True
        and isinstance(item.get("channelCurrentEpoch"), str)
        for item, generation in zip(channels, generations, strict=True)
    )
    alive = sum(item.get("channelAlive") is True for item in channels)
    recently_observed = sum(
        item.get("channelRecentlyObserved") is True for item in channels
    )
    metrics: JsonObject = {
        "total": len(channels),
        "current": confirmed_current,
        "telemetryCurrent": generations.count("current"),
        "notCurrentThisGeneration": len(channels) - confirmed,
        "legacyOrUnknown": legacy,
        "unfenced": unfenced,
        "protocolVersion": CHANNEL_PROTOCOL_VERSION,
        "recentlyObserved": recently_observed,
        "alive": alive,
    }
    if legacy or unfenced or confirmed != len(channels) or alive != len(channels):
        return {
            "name": "mcp-channel-generation",
            "status": "warn",
            "detail": (
                "one or more persisted interactive Claude registrations use "
                "legacy/unknown Channel code, lack the stable owner fence, or "
                "have not completed a ref-guarded refresh and recent "
                "operational-liveness observation for this daemon generation"
            ),
            "action": {
                "command": "hyprial start claude --name <name> [--resume <session-id>]",
                "description": (
                    "Start or resume the coordinator through the upgraded hyprial "
                    "launcher to generate a current, fenced Channel process."
                ),
            },
            "metrics": metrics,
        }
    return {
        "name": "mcp-channel-generation",
        "status": "ok",
        "detail": (
            "interactive Claude registrations report the current Channel "
            "telemetry and owner fence, and hold a recently observed "
            "operational-liveness lease in this daemon generation"
        ),
        "metrics": metrics,
    }


def _lark_inbound_doctor_check(result: JsonObject) -> JsonObject | None:
    """Summarize desired Lark adapters using inbound-stream health."""

    from hyprial.adapters.lark import lifecycle as lark_lifecycle
    from hyprial.contracts.lark import lark_recovery_coverage

    raw_adapters = result.get("adapters")
    if not isinstance(raw_adapters, list):
        return None
    adapters = [
        item
        for item in raw_adapters
        if isinstance(item, dict)
        and item.get("provider") == "lark"
        and item.get("desired") is True
    ]
    if not adapters:
        return None
    stale = sorted(
        str(item.get("name"))
        for item in adapters
        if item.get("streamHealth") == "stale" or item.get("status") == "stale"
    )
    unavailable = sorted(
        str(item.get("name"))
        for item in adapters
        if item.get("online") is not True
        and item.get("status") not in {"starting", "checking", "stale"}
    )
    unknown = sorted(
        str(item.get("name"))
        for item in adapters
        if item.get("online") is True
        and item.get("streamHealth") not in {"healthy", "stale"}
    )
    metrics: JsonObject = {
        "stale": stale,
        "unavailable": unavailable,
        "unknown": unknown,
        **lark_recovery_coverage(),
    }
    if stale:
        return {
            "name": "lark-inbound",
            "status": "fail",
            "detail": (
                "Lark worker process is alive but its inbound websocket is stale"
            ),
            "action": {
                "command": f"hyprial adapter status {stale[0]} --json",
                "description": (
                    "Inspect the active probe and automatic rebuild transition."
                ),
            },
            "metrics": metrics,
        }
    if unavailable:
        return {
            "name": "lark-inbound",
            "status": "fail",
            "detail": "one or more desired Lark inbound adapters are unavailable",
            "action": {
                "command": f"hyprial adapter status {unavailable[0]} --json",
                "description": "Inspect adapter startup or restart diagnostics.",
            },
            "metrics": metrics,
        }
    over_deadline = sorted(
        (
            str(item.get("name")),
            str(item.get("lifecycleCorrelationId") or "unknown"),
            int(item.get("lifecycleAgeSeconds") or 0),
        )
        for item in adapters
        if item.get("lifecycleCorrelationId")
        and int(item.get("lifecycleAgeSeconds") or 0)
        > lark_lifecycle.START_DEADLINE_SECONDS
    )
    if over_deadline:
        name, correlation, age = over_deadline[0]
        return {
            "name": "lark-inbound",
            "status": "fail",
            "detail": (
                f"Lark adapter {name} lifecycle transition {correlation} has "
                f"held the lifecycle lock for {age}s, past the "
                f"{lark_lifecycle.START_DEADLINE_SECONDS:.0f}s deadline"
            ),
            "action": {
                "command": f"grep '{correlation}' ~/.hyprial/state/logs/daemon.jsonl",
                "description": (
                    "The daemon fails loud at the deadline and releases the "
                    "lock; grep the locking correlation in daemon.jsonl to "
                    "see which command wrote it and where its completion "
                    "settled, then re-run start."
                ),
            },
            "metrics": metrics,
        }
    if any(item.get("status") in {"starting", "checking"} for item in adapters):
        return {
            "name": "lark-inbound",
            "status": "warn",
            "detail": (
                "one or more desired Lark inbound adapters are starting "
                "or verifying history"
            ),
            "metrics": metrics,
        }
    if unknown:
        return {
            "name": "lark-inbound",
            "status": "warn",
            "detail": "Lark worker is online but did not report inbound telemetry",
            "action": {
                "command": f"hyprial adapter status {unknown[0]} --json",
                "description": "Inspect the worker version and telemetry reader.",
            },
            "metrics": metrics,
        }
    return {
        "name": "lark-inbound",
        "status": "ok",
        "detail": (
            "desired Lark inbound event streams are live; history recovery "
            "covers known chats only, unknown first-chat recovery is unsupported, "
            "and chat enumeration is not implemented"
        ),
        "metrics": metrics,
    }


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

    from hyprial.org.store import OrgContextStore

    store = OrgContextStore(_hyprial_home())
    store.ensure_layout()
    if store.load_accepted() is not None:
        return None
    return {"code": "ORG_CONTEXT_ABSENT", "message": "org-context absent"}


def _org_init_warning(warning: JsonObject | None) -> JsonObject | None:
    """Ask neighbors once for an empty slot and summarize staged candidates."""

    if warning is None:
        return None
    try:
        result = _daemon_request(
            "org.fetch",
            {"requestMode": "neighbors", "timeoutSeconds": 2.0},
            restore_wait=0.0,
        )
    # PR #332 F4②: transient daemon-request failures are their own class
    # now; this best-effort branch keeps swallowing them alongside CliError.
    except (CliError, ipc_errors.TransientDaemonError):
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

    def already_running(status: Any) -> JsonObject:
        if not isinstance(status, dict):
            raise CliError("INVALID_RESPONSE", "daemon ps result must be an object")
        if listen is not None or connect is not None:
            raise CliError(
                "DAEMON_ALREADY_RUNNING",
                "stop the running daemon before changing zenoh endpoints",
            )
        existing: JsonObject = DaemonLaunchResult.existing(status).to_payload()
        _append_warning(existing, _org_init_warning(org_warning))
        _append_warning(existing, _zenoh_endpoint_warning(status))
        return existing

    try:
        status = _daemon_probe(timeout=0.5)
    except ipc_errors.DaemonUnavailableError:
        pass
    else:
        return already_running(status)

    login_result: JsonObject | None = None
    owner_override = os.environ.get("HYPRIAL_OWNER", "").strip()
    if not owner_override:
        from hyprial.daemon.identity import node_owner_or_none, resolve_node_owner

        if node_owner_or_none() is None and login_when_missing is not None:
            try:
                login_result = login_when_missing()
            except KeyboardInterrupt as error:
                raise CliError(
                    "INTERRUPTED",
                    "login was interrupted; run `hyprial login` or rerun `hyprial init`",
                    data={"nextSteps": ["hyprial login", "hyprial init"]},
                ) from error
            except CliError as error:
                data = dict(error.data) if isinstance(error.data, dict) else {}
                data["nextSteps"] = ["hyprial login", "hyprial init"]
                raise CliError(
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

    launched = _launch_daemon_process(
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
    if login_result is not None:
        for key in ("identity", "network", "verificationUri", "userCode", "sidecar"):
            if key in login_result:
                result[key] = login_result[key]
        network = login_result.get("network")
        if isinstance(network, dict) and network.get("status") == "failed":
            _append_warning(
                result,
                {
                    "code": "NETWORK_JOIN_FAILED",
                    "message": (
                        "login identity was kept and the daemon started; rerun "
                        "`hyprial login` to retry only the network join"
                    ),
                    "data": network,
                },
            )
    return result


def _append_warning(result: JsonObject, warning: JsonObject | None) -> None:
    if warning is None:
        return
    warnings = result.setdefault("warnings", [])
    if not isinstance(warnings, list):
        raise CliError("INVALID_RESPONSE", "warnings must be an array")
    warnings.append(warning)


def _plugin_skip_warnings(skipped: Sequence[Any]) -> list[JsonObject]:
    return [
        {
            "code": "PLUGIN_SKIPPED",
            "message": f"plugin {item.name!r} was not loaded: {item.reason}",
            "data": {"plugin": item.name, "reason": item.reason},
        }
        for item in skipped
    ]


def _announce_plugin_skips(warnings: Sequence[JsonObject]) -> None:
    for warning in warnings:
        print(f"hyprial: warning: {warning['message']}", file=sys.stderr)


def _with_plugin_warnings(
    result: JsonObject, warnings: Sequence[JsonObject]
) -> JsonObject:
    for warning in warnings:
        _append_warning(result, warning)
    return result


@app.command("help")
def help_command(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the top-level command inventory registered with Typer."""

    try:
        require_initialized_hyprial_home()
    except Exception as error:  # noqa: BLE001 - CLI error boundary
        _fail(error, json_output=json_output)
    usage = render_top_level_help(app)
    _emit(
        {"ok": True, "usage": usage} if json_output else usage,
        json_output=json_output,
    )


@app.command()
def version(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the installed version and update source."""

    _execute(
        _version_result, json_output=json_output, allow_missing_home=True
    )


@app.command()
def install(
    name: str = typer.Argument(..., help="Application name from the trusted catalog."),
    yes: bool = typer.Option(False, "--yes", help="Skip the installation prompt."),
    check: bool = typer.Option(
        False, "--check", help="Installed app only: report the catalog relation without changing it."
    ),
    force: bool = typer.Option(
        False, "--force", help="Installed app only: replace dirty, older, or diverged sources."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Install an application, or move an installed one to the catalog's current commit.

    A fresh install checks out the exact commit the catalog resolves to.  An
    app that already has an install receipt is *upgraded* instead -- the same
    plan / confirm / stop / apply / restart path as ``hyprial <app> upgrade`` --
    so the receipt and ``source/`` move together and a manifest that gained
    ``commands`` (hyprial.install/v2) is mounted on the next ``hyprial`` start.

    ⛔ The MVP's INSTALL_UPDATE_UNSUPPORTED refusal is gone on purpose.  It
    left every pre-v2 install with no command that could move it forward: on
    2026-09-04 a node on a v1-only binary could neither ``install gui`` nor
    ``gui upgrade`` once the manifest went v2, and ``hyprial <app> upgrade`` only
    exists *after* the mount that the upgrade was supposed to enable.  A
    generic ``hyprial upgrade <app>`` is not the answer either: ``hyprial upgrade`` is
    hyprial's own self-upgrade in the ruled command surface (#292).  See
    docs/design-app-manifest-commands.md §6.
    """

    def operation() -> JsonObject:
        hyprial_home = _hyprial_home()
        # A receipt is a file; a directory under this name is another
        # installer's ledger (kanban), not an installed app to upgrade.
        if (hyprial_home / "apps" / name / "install.json").is_file():
            if check and (force or yes):
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT, "--check cannot be combined with --force or --yes"
                )
            result = upgrade_mounted_app(
                name, hyprial_home, check_only=check, force=force, yes=yes, json_output=json_output,
            )
            return {**result, "mode": "upgrade"}
        if check or force:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"--check and --force apply to an installed app; {name!r} has no install receipt",
            )

        def confirm(plan: JsonObject) -> bool:
            if json_output and not yes:
                raise CliError(
                    "CONFIRMATION_REQUIRED",
                    "installation requires confirmation; rerun with --yes",
                    plan,
                )
            if not json_output:
                _emit(plan, json_output=False)
            return yes or typer.confirm(f"Install {name} from this exact commit?")

        result = install_application(
            name,
            hyprial_home=hyprial_home,
            confirm=confirm,
            json_output=json_output,
        )

        return result

    _execute(operation, json_output=json_output)


@app.command("ps")
def process_status(
    connector_kind: str | None = typer.Argument(None, help="Optional connector kind."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show daemon and connector status."""

    def operation() -> JsonObject:
        try:
            result = _daemon_request("ps", restore_wait=0.0)
        # PR #332 F4②: both projections branch on the registered transient
        # classes instead of comparing code strings.  Other transient
        # failures (disconnected / timeout) surface to the CLI error
        # boundary, as the old ``!= DAEMON_RESTORING``/``!=
        # DAEMON_UNAVAILABLE`` raises did.
        except ipc_errors.DaemonRestoringError:
            # The daemon is up and answering but restore has not
            # finished: answer from the readiness probe instead of
            # hanging until ps is served.  `phase`/"restorePending" are
            # the restoring signal; connectors stay empty because the
            # fleet table is exactly what is still being built.
            probe = _daemon_probe(timeout=2.0)
            return {
                "ok": True,
                "restoring": True,
                "daemon": {
                    key: probe[key]
                    for key in (
                        "running",
                        "pid",
                        "epoch",
                        "nodeId",
                        "owner",
                        "socket",
                        "phase",
                    )
                    if key in probe
                },
                "restorePending": probe.get("restorePending", True),
                "connectors": [],
            }
        except ipc_errors.DaemonUnavailableError:
            return {"ok": True, "daemon": {"running": False}, "connectors": []}
        if not isinstance(result, dict):
            raise CliError("INVALID_RESPONSE", "daemon ps result must be an object")
        connectors = result.get("connectors", [])
        if connector_kind is not None and isinstance(connectors, list):
            connectors = [
                item
                for item in connectors
                if isinstance(item, dict) and item.get("runtime") == connector_kind
            ]
        return {"ok": True, **result, "connectors": connectors}

    _execute(operation, json_output=json_output)


_TOP_SEVERITY = {
    "stuck": 0,
    "spin": 1,
    "stalled": 2,
    "backlog": 3,
    "ok": 4,
    "idle": 5,
    "online": 5,
    "offline": 6,
    "stopped": 7,
}


def _parse_ps_duration(value: str) -> float | None:
    """Parse ps ``time=``/``etime=`` values ([[dd-]hh:]mm:ss[.cc]) to seconds."""

    match = re.fullmatch(
        r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)(?:\.(\d+))?", value.strip()
    )
    if match is None:
        return None
    days, hours, minutes, seconds, fraction = match.groups()
    total = int(minutes) * 60 + int(seconds)
    total += int(hours) * 3600 if hours else 0
    total += int(days) * 86400 if days else 0
    if fraction:
        total += int(fraction) / (10 ** len(fraction))
    return float(total)


def _sample_process_table() -> dict[int, tuple[int, float, int, float, float]]:
    """One ``ps`` sweep: pid -> (ppid, cpu%, rss kb, cpu seconds, etime seconds).

    These harnesses are I/O-bound, so instantaneous %cpu is normally 0.0 and
    carries almost no diagnostic weight; it is sampled anyway (one fork total)
    and only rendered under ``--wide``.
    """

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,etime=,%cpu=,rss=,time="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    table: dict[int, tuple[int, float, int, float, float]] = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
            cpu_percent = float(fields[3])
            rss_kb = int(fields[4])
        except ValueError:
            continue
        etime = _parse_ps_duration(fields[2])
        cputime = _parse_ps_duration(fields[5])
        if etime is None or cputime is None:
            continue
        table[pid] = (ppid, cpu_percent, rss_kb, cputime, etime)
    return table


def _subtree_totals(
    table: dict[int, tuple[int, float, int, float, float]], pid: int
) -> tuple[float, int, float, float] | None:
    """Aggregate cpu%/rss/cputime over the pid's ppid subtree.

    pi workers share the daemon's process group, and codex grandchildren
    escape the worker's own group, so per-pgid aggregation is wrong in both
    directions; walking ppid children is correct for every runtime here.
    """

    if pid not in table:
        return None
    children: dict[int, list[int]] = {}
    for child_pid, entry in table.items():
        children.setdefault(entry[0], []).append(child_pid)
    cpu_percent = 0.0
    rss_kb = 0
    cputime = 0.0
    stack = [pid]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        entry = table.get(current)
        if entry is not None:
            cpu_percent += entry[1]
            rss_kb += entry[2]
            cputime += entry[3]
        stack.extend(children.get(current, ()))
    return cpu_percent, rss_kb, cputime, table[pid][4]


def _format_top_age(ms: int | None) -> str:
    if ms is None:
        return "-"
    seconds = max(0, int(ms / 1000))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h{minutes % 60:02d}m"
    return f"{hours // 24}d{hours % 24:02d}h"


def _format_top_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return _format_top_age(int(seconds * 1000))


def _format_top_rss(rss_kb: int | None) -> str:
    if rss_kb is None:
        return "-"
    if rss_kb >= 1024 * 1024:
        return f"{rss_kb / (1024 * 1024):.1f}G"
    if rss_kb >= 1024:
        return f"{rss_kb // 1024}M"
    return f"{rss_kb}K"


def _top_state_label(row: JsonObject, now_ms: int) -> str:
    state = str(row.get("state"))
    detail = row.get("stateDetail")
    detail = detail if isinstance(detail, dict) else {}
    if state == "stopped":
        return "\u00d7 stopped"
    if state == "online":
        return "online (proc n/a)"
    if state == "offline":
        return "offline (proc n/a)"
    if state == "stuck":
        return f"\u26a0 STUCK {_format_top_age(detail.get('openMs'))}"
    if state == "spin":
        mean_ms = detail.get("meanMs")
        return (
            f"\u26a0 SPIN {detail.get('failures', '?')}/{detail.get('window', '?')}"
            f" avg {_format_top_age(mean_ms)}"
        )
    if state == "stalled":
        return (
            f"\u26a0 STALLED {_format_top_age(detail.get('stalledForMs'))}"
            f" (turn {_format_top_age(detail.get('openMs'))})"
        )
    if state == "backlog":
        return (
            f"\u26a0 BACKLOG q={detail.get('pending', row.get('pendingCount', '?'))}"
            f" idle {_format_top_age(detail.get('idleMs'))}"
        )
    if state == "idle":
        return "idle"
    if detail.get("busy") is True and isinstance(row.get("openTurnStartedAtMs"), int):
        open_ms = now_ms - int(row["openTurnStartedAtMs"])
        return f"ok (turn {_format_top_age(open_ms)})"
    return "ok"


def _render_top(
    payload: JsonObject,
    *,
    wide: bool,
    sampler: Callable[[], dict[int, tuple[int, float, int, float, float]]]
    | None = None,
) -> str:
    daemon = payload.get("daemon")
    daemon = daemon if isinstance(daemon, dict) else {}
    now_ms = payload.get("nowMs")
    now_ms = now_ms if isinstance(now_ms, int) else int(time.time() * 1000)
    actors = [row for row in payload.get("actors", []) if isinstance(row, dict)]
    actors.sort(
        key=lambda row: (
            _TOP_SEVERITY.get(str(row.get("state")), 4),
            str(row.get("name")),
        )
    )
    sample = sampler if sampler is not None else _sample_process_table
    table = sample()

    headers = ["ACTOR", "RT", "PID"]
    if wide:
        headers.append("CPU%")
    headers += ["RSS", "CPUTIME", "UPTIME", "TURNS", "AVG5", "IDLE", "Q", "STATE"]
    rows: list[list[str]] = []
    for row in actors:
        pid = row.get("pid")
        totals = _subtree_totals(table, pid) if isinstance(pid, int) else None
        cpu_percent: float | None = None
        rss_kb: int | None = None
        cputime: float | None = None
        etime: float | None = None
        if totals is not None:
            cpu_percent, rss_kb, cputime, etime = totals
        started_at = row.get("processStartedAtMs")
        uptime_ms = now_ms - started_at if isinstance(started_at, int) else None
        durations = row.get("recentTurnDurationsMs")
        avg5 = (
            int(sum(durations) / len(durations))
            if isinstance(durations, list) and durations
            else None
        )
        last_ended = row.get("lastTurnEndedAtMs")
        idle_ms = now_ms - last_ended if isinstance(last_ended, int) else None
        turn_count = row.get("turnCount")
        cells = [
            str(row.get("name")),
            str(row.get("runtime")) if row.get("runtime") is not None else "-",
            str(pid) if isinstance(pid, int) else "-",
        ]
        if wide:
            cells.append(f"{cpu_percent:.1f}" if cpu_percent is not None else "-")
        cells += [
            _format_top_rss(rss_kb),
            _format_top_seconds(cputime),
            (
                _format_top_age(uptime_ms)
                if uptime_ms is not None
                else _format_top_seconds(etime)
            ),
            str(turn_count) if isinstance(turn_count, int) else "-",
            _format_top_age(avg5),
            _format_top_age(idle_ms),
            str(row.get("pendingCount", 0)),
            _top_state_label(row, now_ms),
        ]
        rows.append(cells)

    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rows)])
        for index in range(len(headers))
    ]
    lines = [
        f"hyprial top   daemon {daemon.get('pid', '?')}   node {daemon.get('nodeId', '?')}"
    ]
    bypass = daemon.get("dispatchWithoutPacCount")
    if isinstance(bypass, int):
        # A3 dispatch gate (design-dispatch-always-pac §三②): the numerator
        # of the PAC bypass rate -- dispatches that never went through a
        # workflow run.  Record-only for now; the durable events live in
        # state/logs/daemon.jsonl.
        lines.append(f"dispatch without PAC: {bypass}")
    conversations = daemon.get("dispatchConversationCount")
    if isinstance(conversations, int):
        # spec-dispatch-gate-classifier-2026-09-04: the contrast counter --
        # request-shape sends the narrowed gate classified as conversation
        # (adjudications, progress reports, replies), observed but never in
        # the numerator above.
        lines.append(f"dispatch conversations (not counted): {conversations}")
    lines.append(
        "  ".join(
            header.ljust(widths[index]) for index, header in enumerate(headers)
        ).rstrip()
    )
    for row in rows:
        lines.append(
            "  ".join(
                cell.ljust(widths[index]) for index, cell in enumerate(row)
            ).rstrip()
        )
    stranded = [item for item in payload.get("stranded", []) if isinstance(item, dict)]
    if stranded:
        lines.append("")
        lines.append("stranded inbox keys (other node; never claimed here):")
        for item in stranded:
            lines.append(
                f"  {item.get('recipient')}  pending={item.get('pending', '?')}"
                f"  (current: {item.get('currentRecipient', '?')})"
            )
    lines.extend(_render_top_quota(payload.get("quota"), now_ms))
    return "\n".join(lines)


def _format_top_window_seconds(seconds: int | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _format_top_reset(resets_at_ms: int | None) -> str | None:
    if not isinstance(resets_at_ms, int):
        return None
    when = datetime.fromtimestamp(resets_at_ms / 1000).astimezone()
    return when.strftime("%m-%d %H:%M")


def _format_quota_window(window: JsonObject, now_ms: int) -> str | None:
    used = window.get("used")
    limit = window.get("limit")
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
        return None
    if limit <= 0:
        return None
    label = str(window.get("label") or window.get("id"))
    scope = window.get("scope")
    if isinstance(scope, str) and scope:
        label = f"{label}:{scope}"
    span = _format_top_window_seconds(window.get("windowSeconds"))
    if span is not None:
        label = f"{label}({span})"
    percent = used / limit * 100
    # Percent-limited sources (claude/codex) show the percentage directly;
    # count-limited sources (kimi) show the raw ratio so a limit other than
    # 100 is never mistaken for a percentage.
    value = f"{percent:g}%" if limit == 100 else f"{used:g}/{limit:g} ({percent:g}%)"
    flags: list[str] = []
    severity = window.get("severity")
    if isinstance(severity, str) and severity not in ("", "normal"):
        flags.append(severity.upper())
    if window.get("isActive") is True:
        flags.append("ACTIVE")
    reset = _format_top_reset(window.get("resetsAtMs"))
    suffix = f" {' '.join(flags)}" if flags else ""
    if reset is not None:
        suffix += f", resets {reset}"
    return f"{label} {value}{suffix}"


def _render_top_quota(quota: object, now_ms: int) -> list[str]:
    if not isinstance(quota, dict):
        return []
    sources = [
        source for source in quota.get("sources", []) if isinstance(source, dict)
    ]
    if not sources:
        return []
    lines = ["", "quota"]
    for source in sources:
        name = str(source.get("source"))
        age_ms = source.get("ageMs")
        age = _format_top_age(age_ms if isinstance(age_ms, int) else None)
        if source.get("backingOff") is True:
            # A backoff serve is old-but-real data: name the state instead of
            # pretending the reading is fresh.
            marker = f"[{age} ago, backing off]"
        else:
            stale = " STALE" if source.get("stale") is True else ""
            marker = f"[{age} ago{stale}]"
        if source.get("ok") is not True:
            reason = source.get("reason")
            lines.append(
                f"  {name:<8} n/a ({reason if isinstance(reason, str) else 'unknown'}) {marker}"
            )
            continue
        parts = [
            rendered
            for window in source.get("windows", [])
            if isinstance(window, dict)
            and (rendered := _format_quota_window(window, now_ms)) is not None
        ]
        extra = source.get("extra")
        if isinstance(extra, dict):
            spend = extra.get("spend")
            if isinstance(spend, dict) and isinstance(
                spend.get("limitUsd"), (int, float)
            ):
                used_usd = spend.get("usedUsd")
                text = f"spend ${used_usd or 0:g}/${spend['limitUsd']:g}"
                if spend.get("enabled") is not True:
                    reason = spend.get("disabledReason")
                    text += f" ({reason if isinstance(reason, str) else 'disabled'})"
                parts.append(text)
            credits = extra.get("credits")
            if isinstance(credits, dict) and isinstance(credits.get("balance"), str):
                parts.append(f"credits {credits['balance']}")
        lines.append(f"  {name:<8} {' · '.join(parts) or 'n/a'} {marker}")
    return lines


@app.command("top")
def top_status(
    wide: bool = typer.Option(
        False,
        "--wide",
        help="Also show instantaneous CPU% (normally 0.0 for these I/O-bound harnesses).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show whether each agent is actually working: turns, queue, verdicts."""

    def operation() -> Any:
        result = _daemon_request("top.snapshot")
        if not isinstance(result, dict) or not isinstance(result.get("actors"), list):
            raise CliError(
                "INVALID_RESPONSE", "daemon top.snapshot result must contain actors"
            )
        payload: JsonObject = {"ok": True, **result}
        if json_output:
            return payload
        return _render_top(payload, wide=wide)

    _execute(operation, json_output=json_output)


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Run read-only health checks."""

    _execute(_doctor_result, json_output=json_output)


_TARGETS_KIND_OPTIONS = ("agent", "user", "channel_route")


def _render_targets(rows: list[JsonObject]) -> str:
    """Plain-text table in the ``hyprial top`` style: preamble, header, columns.

    targets lists delivery promises only (Allen's definition), so every row
    is deliverable by construction; the KIND column stays because user and
    channel_route rows join once their delivery paths are verified.
    """

    headers = ["TARGET", "KIND", "STATUS", "hosted", "hostedBy"]
    cells: list[list[str]] = [
        [
            str(row.get("targetUri", "?")),
            str(row.get("targetKind", "?")),
            str(row.get("status", "?")),
            str(row["hosted"]).lower() if "hosted" in row else "-",
            str(row.get("hostedBy") or "-"),
        ]
        for row in rows
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in cells)])
        for index in range(len(headers))
    ]
    lines = [f"hyprial targets   {len(rows)} targets"]
    lines.append(
        "  ".join(
            header.ljust(widths[index]) for index, header in enumerate(headers)
        ).rstrip()
    )
    for row in cells:
        lines.append(
            "  ".join(
                cell.ljust(widths[index]) for index, cell in enumerate(row)
            ).rstrip()
        )
    return "\n".join(lines)


def _render_hosts(rows: list[JsonObject]) -> str:
    """Node visibility table; hosts are not delivery targets."""

    headers = ["NODE", "STATUS"]
    cells: list[list[str]] = [
        [str(row.get("nodeId", "?")), str(row.get("status", "?"))] for row in rows
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in cells)])
        for index in range(len(headers))
    ]
    lines = [f"hyprial hosts   {len(rows)} nodes"]
    lines.append(
        "  ".join(
            header.ljust(widths[index]) for index, header in enumerate(headers)
        ).rstrip()
    )
    for row in cells:
        lines.append(
            "  ".join(
                cell.ljust(widths[index]) for index, cell in enumerate(row)
            ).rstrip()
        )
    return "\n".join(lines)


@app.command()
def targets(
    kind: str | None = typer.Option(
        None, "--kind", help="Filter: agent, user, or channel_route."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List live delivery targets.

    A target is a delivery promise (Allen): connectors (``agent``), owners
    with a completed squire profile (``user``), and configured outbound
    routes (``channel_route``).  Nodes are network peers, not targets —
    see ``hyprial hosts`` for node visibility.
    """

    def operation() -> Any:
        if kind is not None and kind not in _TARGETS_KIND_OPTIONS:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--kind must be agent, user, or channel_route",
            )
        params: JsonObject = {}
        if kind is not None:
            params["kind"] = kind
        result = _daemon_request("targets", params)
        if not isinstance(result, dict) or not isinstance(result.get("targets"), list):
            raise CliError(
                "INVALID_RESPONSE", "daemon targets result must contain an array"
            )
        if json_output:
            return {"ok": True, **result}
        return _render_targets(result["targets"])

    # --json is pretty-printed (indent 2): the default table is for humans,
    # the JSON is for both, and neither is a Python repr.
    _execute(operation, json_output=json_output, json_indent=2)


@app.command()
def hosts(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List nodes announced on the network (online status only).

    Hosts are not delivery targets: a node receives no actor messages.
    This view exists so operators keep node visibility now that targets
    lists delivery promises only.
    """

    def operation() -> Any:
        result = _daemon_request("hosts", {})
        if not isinstance(result, dict) or not isinstance(result.get("hosts"), list):
            raise CliError(
                "INVALID_RESPONSE", "daemon hosts result must contain an array"
            )
        if json_output:
            return {"ok": True, **result}
        return _render_hosts(result["hosts"])

    _execute(operation, json_output=json_output, json_indent=2)


def _validate_from_identity(
    ctx: typer.Context, param: typer.CallbackParam, value: str
) -> str:
    """Reject an unresolvable bare ``--from`` before anything is sent.

    The daemon is the single source of resolution truth: ``agent.resolve``
    runs the same pipeline ``message.send`` uses, so this check cannot drift
    from the delivery boundary.  Any uncertainty -- daemon unreachable,
    timeout, malformed answer -- fails open and lets the daemon judge at
    send time: a validator that blocks legitimate sends trains people to
    bypass it.  Shell completion runs callbacks under resilient parsing;
    doing IPC (or raising) there would break completion.
    """

    if ctx.resilient_parsing or not value or ":" in value:
        return value
    try:
        answer = _daemon_request("agent.resolve", {"name": value}, timeout=1.0)
    except (CliError, ipc_errors.TransientDaemonError) as error:
        if isinstance(error, CliError) and error.code == ipc_errors.AMBIGUOUS_TARGET:
            raise typer.BadParameter(str(error), ctx=ctx, param=param) from error
        return value
    if not isinstance(answer, dict) or "known" not in answer:
        return value
    if answer["known"] is True:
        return value
    raise typer.BadParameter(
        f"unknown local actor {value!r}; use a registered alias "
        "(see 'hyprial targets') or the canonical form "
        "agent:<owner>:<node>:<actor>",
        ctx=ctx,
        param=param,
    )


@app.command()
def send(
    message: list[str] = typer.Argument(..., help="Message text."),
    source: str = typer.Option(
        ...,
        "--from",
        help="Local sending actor.",
        callback=_validate_from_identity,
    ),
    to: list[str] = typer.Option(..., "--to", help="Target; repeat for fan-out."),
    topic: str | None = typer.Option(None, "--topic"),
    conversation: str | None = typer.Option(None, "--conversation"),
    reply_to: str | None = typer.Option(None, "--reply-to"),
    files: list[Path] | None = typer.Option(None, "--file"),
    images: list[Path] | None = typer.Option(None, "--image"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Send a message through the daemon."""

    def operation() -> Any:
        text = " ".join(message).strip()
        if not text:
            raise CliError(ipc_errors.INVALID_ARGUMENT, "message must not be empty")
        resources = []
        for kind, paths in (("file", files or []), ("image", images or [])):
            for path in paths:
                resources.append(
                    {
                        "path": str(path.expanduser().resolve()),
                        "kind": kind,
                        "mediaType": mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream",
                    }
                )
        params: JsonObject = {"from": source, "to": to, "message": text}
        if topic is not None:
            params["topic"] = topic
        if conversation is not None:
            params["conversationId"] = conversation
        if reply_to is not None:
            params["replyTo"] = reply_to
        if resources:
            params["resourcePaths"] = resources
        result = _daemon_request("message.send", params)
        if (
            not json_output
            and isinstance(result, dict)
            and result.get("replyPathUnavailable") is True
        ):
            # Facts only (#67): what is true about this sender's reply path.
            # No instructions.
            print(
                f"hyprial: sender {source!r} has no reply path: "
                "replies to it cannot be delivered",
                file=sys.stderr,
            )
        return result

    _execute(operation, json_output=json_output)


@app.command()
def ack(
    message_id: str = typer.Argument(..., help="Message ID to acknowledge."),
    source: str = typer.Option(..., "--from", help="Local acknowledging actor."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Acknowledge a message without replying."""

    _execute(
        lambda: _daemon_request(
            "message.ack", {"from": source, "messageId": message_id}
        ),
        json_output=json_output,
    )


@outbox_app.command("list")
def outbox_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List every queued outbox entry, flagging undeliverable schemes."""

    _execute(lambda: _daemon_request("outbox.list"), json_output=json_output)


@outbox_app.command("prune")
def outbox_prune(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be pruned without deleting."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Move dead outbox entries (undeliverable scheme or expired TTL) to the DLQ."""

    _execute(
        lambda: _daemon_request("outbox.prune", {"dryRun": dry_run}),
        json_output=json_output,
    )


@delivery_app.command("status")
def delivery_status(
    source: str = typer.Option(..., "--from", help="Local sending actor."),
    message_id: str | None = typer.Option(
        None, "--message-id", help="One message; omit for every recorded outcome."
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Seconds to wait on other holders (default 2)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the persisted terminal state of messages you sent.

    Every persisted outcome is ``fetched`` (the recipient took it) or
    ``expired`` (a holder evicted it).  The query reports ``unconfirmed``
    instead of ``expired`` when the recipient node did not answer, answered
    undecodably, cannot be resolved from the recipient URI, or is too old to
    send named empty replies.  Wait or query again; do not resend solely from
    ``unconfirmed``.  ``pending`` means a holder still has it and no verdict
    exists yet; ``unknown`` means no reachable holder has a record.
    """

    def operation() -> Any:
        params: JsonObject = {"from": source}
        if message_id is not None:
            params["messageId"] = message_id
        if timeout is not None:
            params["timeoutSeconds"] = timeout
        return _daemon_request("message.status", params)

    _execute(operation, json_output=json_output)


@app.command("query")
def query_command(
    actor: str = typer.Argument(..., help="Local actor name or agent URI."),
    view: str = typer.Argument(..., help="inbox or outbox."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read one local actor's inbox or outbox, changing nothing.

    ``inbox`` lists the messages and system notices still waiting for the
    actor, with their text; ``outbox`` lists what the actor sent that is still
    queued for delivery.  Read-only: nothing is fetched, acknowledged or
    drained, so the agent still receives exactly what it would have.
    """

    if view not in {"inbox", "outbox"}:
        raise CliError(ipc_errors.INVALID_ARGUMENT, "view must be inbox or outbox")

    def operation() -> Any:
        return _daemon_request("message.query", {"from": actor, "view": view})

    _execute(operation, json_output=json_output)


@app.command("log")
def log_command(
    component: str | None = typer.Option(
        None, "--component", help="Exact component scope (for example daemon)."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Exact daemon, adapter, or worker name scope."
    ),
    level: str | None = typer.Option(
        None, "--level", help="Exact level: debug, info, warn, or error."
    ),
    actor: str | None = typer.Option(None, "--actor"),
    conversation: str | None = typer.Option(None, "--conversation"),
    since: str | None = typer.Option(None, "--since"),
    until: str | None = typer.Option(None, "--until"),
    correlation_id: str | None = typer.Option(None, "--correlation-id"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Query all structured daemon, adapter, and worker JSONL history."""

    _execute(
        lambda: _log_result(
            component=component,
            name=name,
            level=level,
            actor=actor,
            conversation=conversation,
            since=since,
            until=until,
            correlation_id=correlation_id,
        ),
        json_output=json_output,
    )


@app.command("trajectory")
def trajectory_command(
    message_id: str = typer.Argument(..., help="Message ID to trace."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Merge local structured events with the pullable message status."""

    def operation() -> Any:
        result = _trajectory_result(message_id)
        return result if json_output else _format_trajectory(result)

    _execute(operation, json_output=json_output)


def _org_meta(document: Any) -> JsonObject:
    return {
        "version": document.meta.version,
        "issuedAt": document.meta.issued_at.isoformat(),
        "publisher": document.meta.publisher,
    }


@org_app.command("show")
def org_show(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the organization view this node's owner has adopted."""

    def operation() -> JsonObject:
        from hyprial.org.document import document_summary
        from hyprial.org.store import OrgContextStore

        store = OrgContextStore(_hyprial_home())
        document = store.load_accepted()
        if document is None:
            return {
                "ok": True,
                "status": "absent",
                "message": "org-context absent",
            }
        record = store.acceptance_record()
        if record is None:  # load_accepted above proved the slot exists
            raise CliError("ORG_CONTEXT_CORRUPT", "acceptance metadata is absent")
        return {
            "ok": True,
            "status": "accepted",
            "meta": _org_meta(document),
            "source": {
                "publisher": document.meta.publisher,
            },
            "adoptedAt": record.adopted_at.isoformat().replace("+00:00", "Z"),
            "summary": document_summary(document),
        }

    _execute(operation, json_output=json_output)


@org_app.command("status")
def org_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show accepted-slot and pending-candidate counts."""

    def operation() -> JsonObject:
        from hyprial.org.store import OrgContextStore

        return {"ok": True, **OrgContextStore(_hyprial_home()).status()}

    _execute(operation, json_output=json_output)


@org_app.command("fetch")
def org_fetch(
    source_target: str | None = typer.Option(
        None, "--from", help="Reachable node target from hyprial targets."
    ),
    timeout: float = typer.Option(2.0, "--timeout", help="Mesh reply timeout."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Fetch accepted org-context versions from reachable target nodes."""

    def operation() -> JsonObject:
        if timeout <= 0 or timeout > 30:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--timeout must be greater than 0 and at most 30",
            )
        params: JsonObject = {"timeoutSeconds": timeout}
        if source_target is not None:
            params["from"] = source_target
        result = _daemon_request("org.fetch", params, timeout=timeout + 1.0)
        if not isinstance(result, dict) or not isinstance(
            result.get("candidates"), list
        ):
            raise CliError(
                "INVALID_RESPONSE", "daemon org.fetch result must contain candidates"
            )
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


@org_app.command("import")
def org_import(
    source: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    force: bool = typer.Option(
        False, "--force", help="Skip the owner confirmation (validation still applies)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Preview and locally adopt one organization view."""

    def operation() -> JsonObject:
        from hyprial.org.document import parse_document, serialize_document
        from hyprial.org.store import OrgContextStore

        try:
            document = parse_document(source.read_text(encoding="utf-8"))
        except OSError as error:
            raise CliError(
                "ORG_CONTEXT_READ_FAILED", f"cannot read org-context {source}: {error}"
            ) from error
        store = OrgContextStore(_hyprial_home())
        accepted = store.load_accepted()
        previous = "" if accepted is None else serialize_document(accepted)
        incoming = serialize_document(document)
        diff = "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                incoming.splitlines(keepends=True),
                fromfile="accepted/org-context.md",
                tofile=f"incoming/{source.name}",
            )
        )
        preview: JsonObject = {
            "meta": _org_meta(document),
            "source": {
                "publisher": document.meta.publisher,
            },
            "diff": diff,
        }
        if not force:
            if json_output:
                raise CliError(
                    "CONFIRMATION_REQUIRED",
                    "org-context adoption requires owner confirmation; rerun with --force",
                    preview,
                )
            _emit(preview, json_output=False)
            if not typer.confirm("Adopt this org-context on this node?"):
                raise CliError("ADOPTION_DECLINED", "org-context was not adopted")
        record = store.adopt(document)
        published = False
        try:
            publish_result = _daemon_request(
                "org.publish", timeout=2.0, restore_wait=0.0
            )
        except (CliError, ipc_errors.TransientDaemonError):
            pass
        else:
            published = (
                isinstance(publish_result, dict)
                and publish_result.get("published") is True
            )
        return {
            "ok": True,
            "adopted": True,
            "published": published,
            **preview,
            "adoptedAt": record.adopted_at.isoformat().replace("+00:00", "Z"),
        }

    _execute(operation, json_output=json_output)


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
        # ① is a constant -- socket bound, accept running, ping answerable --
        # with restore deliberately outside it (restore runs on the daemon's
        # own thread and reports through ping's phase), so this budget only
        # has to cover bind + accept on a slow machine, and a timeout here is
        # a real startup failure, never a slow restore.  Same order of
        # magnitude as _daemon_request's flat 15s; the env var overrides it
        # for edge environments, which beats raising the default for all.
        15.0,
        "--ready-timeout",
        envvar=_INIT_READY_TIMEOUT_ENV,
        help=(
            "Seconds to wait for the daemon to reach its serving boundary "
            "(socket bound, ping answering) before failing the start "
            "(env: HYPRIAL_INIT_READY_TIMEOUT). Restore runs off this path and "
            "reports through ping's phase, so fleet size is not a factor "
            "and a timeout means the daemon never came up."
        ),
    ),
) -> None:
    """Initialize home and daemon, establishing identity first when owner is absent.

    Home creation, owner establishment, and daemon startup are one in-process
    onboarding flow.  A home with no owner asks where the identity comes
    from (U6): the Hyprial service — the existing login path, unchanged — or
    a self-hosted tailscale, whose owner is asserted by the host's own
    `tailscale whoami` (no Hyprial login, sidecar, or join; fails loudly
    when no tailscale is installed).  An existing owner still takes the
    pre-onboarding path unchanged; no login implementation is copied or
    launched through a shell.
    """

    def operation() -> JsonObject:
        initialize_hyprial_home()
        org_warning = _initialize_org_context()
        held_identity_transaction: list[IdentityTransactionLock] = []

        def login_when_missing() -> JsonObject:
            if (
                _choose_identity_source(json_output=json_output)
                == _IDENTITY_CHOICE_SERVICE
            ):
                return _run_login_cli_flow(
                    no_open=False,
                    json_output=json_output,
                    switch_account=False,
                    preauthkey_file=None,
                    join_timeout=300.0,
                    skip_join=False,
                    install_sidecar_flag=False,
                    no_daemon=True,
                    first_time_setup=True,
                    held_identity_transaction=held_identity_transaction,
                )
            return _run_selfhost_cli_flow(
                json_output=json_output,
                held_identity_transaction=held_identity_transaction,
            )

        try:
            return _complete_initialization(
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

    _execute(operation, json_output=json_output, allow_missing_home=True)


@app.command()
def service(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Manage the daemon service lifecycle."""

    def operation() -> Any:
        try:
            status = _daemon_probe(timeout=0.5)
        except ipc_errors.DaemonUnavailableError:
            return {
                "ok": True,
                "mode": "standalone",
                "installed": False,
                "running": False,
            }
        pid = status.get("pid")
        if not isinstance(pid, int):
            # Legacy-ps probe shape: the fields live under "daemon".
            daemon = status.get("daemon")
            pid = daemon.get("pid") if isinstance(daemon, dict) else None
        return {
            "ok": True,
            "mode": "standalone",
            "installed": False,
            "running": _probe_reports_running(status),
            **({"pid": pid} if isinstance(pid, int) else {}),
        }

    _execute(operation, json_output=json_output)


@daemon_app.command("run")
def daemon_run(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON after shutdown."),
) -> None:
    """Run the product daemon in the foreground until SIGINT or SIGTERM."""

    def operation() -> JsonObject:
        # A renamed daemon launched by the pre-rename upgrader is the first
        # new-code process guaranteed to run after the old daemon releases its
        # databases. Migrate before resolving state or socket paths.
        from hyprial.home_migration import migrate_legacy_default_home

        migrate_legacy_default_home()
        require_initialized_hyprial_home()
        from hyprial.daemon.application import DaemonApplication
        from hyprial.daemon.application import _lock_wait_timeout
        from hyprial.daemon.ownership import (
            DaemonOwnershipBusy,
            DaemonStateOwnershipFence,
        )

        from hyprial.identity_transaction import IdentityTransactionLock

        state_dir = _state_dir()
        try:
            transaction = IdentityTransactionLock.acquire_or_adopt(
                _hyprial_home(), os.environ, timeout=_lock_wait_timeout()
            )
        except IdentityTransactionBusy as error:
            raise CliError(error.code, str(error)) from error
        with transaction:
            try:
                fence = DaemonStateOwnershipFence.acquire(
                    state_dir, timeout=_lock_wait_timeout()
                )
            except DaemonOwnershipBusy as error:
                raise CliError(error.code, str(error)) from error
            with fence:
                runtime = DaemonApplication.from_environment(
                    state_dir=state_dir, socket_path=_socket_path()
                )
                # This interpreter exists to be the daemon, so the daemon is
                # allowed to end it if shutdown leaves a thread that will not join.
                runtime.owns_process_exit()
                runtime.run(
                    ownership_stream=fence.detach(),
                    identity_transaction_stream=transaction.detach(),
                )
        return {"ok": True, "stopped": True}

    _execute(operation, json_output=json_output, allow_missing_home=True)


def old_pid_from(error: CliError) -> int | None:
    """Pull the surviving pid out of the error's payload, never from its prose."""

    data = error.data
    if isinstance(data, dict) and isinstance(data.get("oldPid"), int):
        return data["oldPid"]
    return None


def _describe_survivor(pid: int | None) -> str:
    """Say what is still running. Best effort -- never raises, never blocks.

    ⚠️ When the count cannot be taken, it says so rather than printing 0. A
    zero that means "could not look" reads exactly like a zero that means
    "nothing left", and the whole point of this line is to tell someone whether
    there is a fleet to go and kill.
    """

    if pid is None:
        return "surviving pid: unknown (the stop reported no pid)"
    try:
        completed = subprocess.run(
            ["ps", "-axo", "ppid="],
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
    except Exception:  # noqa: BLE001 -- diagnostics must not raise here
        return f"surviving pid: {pid}; descendant count: could not be taken"
    children = sum(1 for line in completed.stdout.split() if line.strip() == str(pid))
    return f"surviving pid: {pid}; direct children still running: {children}"


def _stop_daemon_for_operator() -> JsonObject:
    """`hyprial daemon stop` -- answers as soon as teardown is done, and says what it saw.

    It asks "did it stop", not "is it gone so I may start another", so it does
    not spend the upgrade path's budget waiting for an exit nobody here will act
    on -- callers give this command about twenty seconds and teardown alone can
    use most of that.

    ⚠️ It does not therefore claim the process is gone. If the pid is still
    around when teardown finishes, that goes back as `survivingPid`: a fact for
    the caller, not a failure. Failing here was tried and measured wrong -- the
    process is normally still exiting at that moment, so failing would report a
    survivor on nearly every healthy stop.

    ⛔ Known gap, written down rather than papered over: nothing currently reads
    `survivingPid` after a bare `hyprial daemon stop`. The upgrade path does act on
    the equivalent signal, and that is the 2026-08-30 scenario; a plain operator
    stop that leaves a survivor is at present unwatched.
    """

    return _stop_daemon_gracefully(require_exit=False)


def _stop_daemon_gracefully(*, require_exit: bool = True) -> JsonObject:
    """Stop for operator and upgrade callers using historical compatibility."""

    return _stop_daemon_with_proof(require_exit=require_exit, require_known_pid=False)


def _stop_daemon_for_identity_switch() -> JsonObject:
    """Stop only when the old PID is known and its exit can be proven."""

    return _stop_daemon_with_proof(require_exit=True, require_known_pid=True)


def _stop_daemon_with_proof(
    *, require_exit: bool, require_known_pid: bool
) -> JsonObject:
    result = _daemon_request("shutdown", timeout=2.0)
    old_pid = (
        result.get("pid")
        if isinstance(result, dict)
        and isinstance(result.get("pid"), int)
        and result["pid"] > 0
        else None
    )
    if require_known_pid and old_pid is None:
        raise CliError(
            "DAEMON_STOP_IDENTITY_UNKNOWN",
            "daemon shutdown did not identify the old pid; refusing to commit "
            "an identity change without an exit fence",
            {"oldPid": None, "teardownCompleted": False},
        )
    old_identity: str | None = None
    if old_pid is not None:
        from hyprial.mcp.channel import _read_process_identity

        old_identity = _read_process_identity(old_pid)
    # ⚠️ Derived, not chosen. This was `10.0`, a number smaller than the
    # daemon's own exit backstop -- so a shutdown that needed the backstop
    # could not satisfy this wait however healthy it was, and a teardown that
    # takes its full budget could not satisfy it at all. On 2026-08-31 that
    # ended an upgrade before it launched a replacement: 56 minutes down.
    from hyprial.daemon.application import TEARDOWN_BUDGETED_SECONDS

    deadline = time.monotonic() + TEARDOWN_BUDGETED_SECONDS
    observed = _wait_daemon_teardown_receipt(
        result if isinstance(result, dict) else {},
        deadline,
        old_pid=old_pid,
        old_identity=old_identity,
        require_exit=require_exit,
    )
    return {"ok": True, **(result if isinstance(result, dict) else {}), **observed}


def _wait_daemon_teardown_receipt(
    shutdown: JsonObject,
    deadline: float,
    *,
    old_pid: int | None,
    old_identity: str | None,
    require_exit: bool = True,
) -> JsonObject:
    """Wait until the old daemon is really gone, not merely finished tearing down.

    The control socket is unlinked before adapters, actor runtimes and their log
    writers finish draining.  ``daemon.lock`` is deliberately released at the
    very end of ``DaemonApplication._close``, so acquiring it — or seeing
    ``daemon.json`` name a different pid — proves the old holder **completed its
    teardown**.

    ⚠️ It does not prove the old holder **exited**, and on 2026-08-30 that gap
    took production down for nine hours.  ``autoupdate`` stopped the daemon at
    03:17:09; all fifty-three registrations closed, the lock came back and
    ``daemon.json`` moved to the replacement — every receipt below said "done" —
    while the process stayed alive until 12:47 the next day, holding one hundred
    and six descendants that then blocked the replacement's bootstrap.  The
    caller of this function is about to start or upgrade a daemon, and for that
    the question is not "did teardown finish" but "is the old one gone".

    So the two facts are kept apart, and which one may end the wait depends on
    whether we know **who** to watch:

    * ``old_pid`` known — only the process disappearing ends the wait.  A
      released lock or a replacement's pid is evidence that teardown ran, and
      is recorded in the timeout message, but it cannot stand in for exit.
    * ``old_pid`` unknown — the process cannot be watched at all, so the lock
      and the marker are the only receipts available and are honoured as before.

    🔑 This is why a replacement daemon does not shorten the wait when the pid is
    known: its arrival says nothing whatever about the old process.  What used to
    make that look safe is that ``_stopped_process`` silently returns ``False``
    for an unknown pid, so without those exits an unknown-pid stop would always
    run to the deadline — the exits were a fallback for "we don't know who to
    watch", not a faster path to success.
    """

    raw_lock_path = shutdown.get("lockPath")
    raw_state_dir = shutdown.get("stateDir")
    lock_path = (
        Path(raw_lock_path).expanduser().resolve()
        if isinstance(raw_lock_path, str) and raw_lock_path
        else None
    )
    state_dir = (
        Path(raw_state_dir).expanduser().resolve()
        if isinstance(raw_state_dir, str) and raw_state_dir
        else (lock_path.parent if lock_path is not None else None)
    )
    stream: Any | None = None
    if lock_path is not None:
        try:
            # Never create a path while proving teardown.  The target daemon
            # minted this lock before accepting the shutdown request.
            stream = lock_path.open("r+", encoding="utf-8")
        except FileNotFoundError:
            stream = None
    # ⭐ `require_exit` is the caller's context, moved out of prose and into the
    # signature. The docstring above said "the caller of this function is about
    # to start or upgrade a daemon" -- true of the upgrade path, false of
    # `hyprial daemon stop`, which starts nothing and only wants to know whether the
    # daemon stopped. A precondition that lives only in a docstring cannot stop
    # itself from being applied where it does not hold, and on 2026-08-31 that
    # cost four CI failures: `daemon stop` inherited a 50s wait for an exit its
    # caller had no need to see, and every caller giving it 20s timed out.
    #
    # ⚠️ Default True: a caller that forgets the flag gets the strict wait. The
    # other default would silently drop the exit requirement for some future
    # "about to start a replacement" path, and that is the nine-hour outage.
    # ⭐ `require_exit` is the caller's context, moved out of prose into the
    # signature. The docstring above says "the caller of this function is about
    # to start or upgrade a daemon" -- true of the upgrade path, false of
    # `hyprial daemon stop`, which starts nothing. A precondition that lives only in
    # a docstring cannot stop itself being applied where it does not hold, and
    # on 2026-08-31 that cost four CI failures.
    #
    # ⚠️ Default True: a caller that forgets the flag gets the strict wait. The
    # other default would silently drop the exit requirement for some future
    # "about to start a replacement" path -- the nine-hour outage.
    receipts_are_sufficient = old_pid is None or not require_exit
    teardown_done = False
    # Report how long we actually waited, not the nominal budget. The previous
    # message hardcoded "10s" and kept saying it after the budget became
    # TEARDOWN_BUDGETED_SECONDS -- an error message that quietly went stale
    # because nothing compares prose against the code beside it.
    started = time.monotonic()
    try:
        while True:
            lock_busy = False
            if stream is not None:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock_busy = True
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    teardown_done = True
                    if receipts_are_sufficient:
                        # ⭐ Report a survivor as a **fact**, not as a verdict.
                        # Failing here was tried and measured wrong: a moment
                        # after the receipts land the process is almost always
                        # still there, because it is in the act of exiting. So
                        # a boolean must not decide -- "still running" 50ms
                        # after teardown and "still running" nine hours later
                        # are the same boolean and entirely different events.
                        if old_pid is None or _stopped_process(old_pid, old_identity):
                            # ⚠️ The field is ABSENT when the process is gone,
                            # not null and not 0: a null reads the same as "we
                            # did not look", and saying whether anyone is still
                            # there is this field's only job.
                            return {"teardownCompleted": True}
                        return {
                            "teardownCompleted": True,
                            "survivingPid": old_pid,
                        }

            # A new daemon can legitimately acquire the same persistent lock
            # before this waiter does.  Its different PID proves the old holder
            # completed teardown -- and nothing more, so with a pid in hand we
            # keep waiting for that pid rather than following the replacement.
            if state_dir is not None and old_pid is not None:
                try:
                    marker = json.loads(
                        (state_dir / "daemon.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    marker = None
                if (
                    isinstance(marker, dict)
                    and isinstance(marker.get("pid"), int)
                    and marker["pid"] != old_pid
                ):
                    teardown_done = True

            if _stopped_process(old_pid, old_identity):
                return {"teardownCompleted": teardown_done}

            if time.monotonic() >= deadline:
                waited = time.monotonic() - started
                if teardown_done:
                    # ⭐ A distinct code, not just distinct prose. The caller has
                    # to branch on this, and matching on a sentence is a coupling
                    # that breaks silently the first time someone rewords it.
                    #
                    # The two timeouts mean opposite things about what to do
                    # next: the one below says "we stopped watching" -- the
                    # process may well be exiting. This one says "we watched,
                    # and it is definitely still there". Waiting longer will not
                    # help; someone has to go find the survivor.
                    raise CliError(
                        "DAEMON_STOP_SURVIVOR",
                        "daemon teardown completed but process "
                        f"{old_pid} did not exit within {waited:.1f}s; it is "
                        "still alive and may still own harness children. Check "
                        "for descendants before starting a replacement.",
                        {"oldPid": old_pid, "teardownCompleted": True},
                    )
                detail = (
                    "lock is still held" if lock_busy else "old process is still alive"
                )
                raise CliError(
                    "DAEMON_STOP_TIMEOUT",
                    "daemon socket closed but teardown did not finish within "
                    f"{waited:.1f}s ({detail})",
                    {"oldPid": old_pid, "teardownCompleted": False},
                )
            time.sleep(0.05)
    finally:
        if stream is not None:
            stream.close()


def _stopped_process(pid: int | None, identity: str | None) -> bool:
    if pid is None:
        return False
    if identity is not None:
        from hyprial.mcp.channel import _owner_process_status

        return _owner_process_status(pid, identity).value in {
            "pid-missing",
            "identity-mismatch",
        }
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _observe_restart_process(error: BaseException) -> RestartProcessObservation:
    """Observe the exact child named by a launch failure at notification time."""

    pid = getattr(error, "_daemon_process_pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return RestartProcessObservation(None, RestartProcessState.UNKNOWN)
    if getattr(error, "_daemon_process_exited", False) is True:
        # Popen.wait/poll is an observation of this exact child, so later PID
        # reuse cannot turn its completed birth back into a running process.
        return RestartProcessObservation(pid, RestartProcessState.EXITED)
    identity = getattr(error, "_daemon_process_identity", None)
    if not isinstance(identity, str) or not identity:
        return RestartProcessObservation(pid, RestartProcessState.UNKNOWN)

    from hyprial.mcp.channel import _owner_process_status

    status = _owner_process_status(pid, identity).value
    if status == "alive":
        return RestartProcessObservation(pid, RestartProcessState.RUNNING)
    if status in {"pid-missing", "identity-mismatch"}:
        return RestartProcessObservation(pid, RestartProcessState.EXITED)
    return RestartProcessObservation(pid, RestartProcessState.UNKNOWN)


@daemon_app.command("stop")
def daemon_stop(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Ask the isolated daemon owning this socket to stop gracefully."""

    _execute(_stop_daemon_for_operator, json_output=json_output)


@mcp_app.command("claude-channel")
def mcp_claude_channel(
    actor: str = typer.Option(..., "--actor"),
    session_ref: str = typer.Option(..., "--session-ref"),
    cwd: Path | None = typer.Option(None, "--cwd"),
    command: list[str] | None = typer.Option(None, "--command"),
    poll_interval: float = typer.Option(0.5, "--poll-interval", hidden=True),
    recovery_signal: Path | None = typer.Option(None, "--recovery-signal", hidden=True),
    owner_pid: int | None = typer.Option(None, "--owner-pid", hidden=True),
    owner_identity: str | None = typer.Option(None, "--owner-identity", hidden=True),
    tmux_session: str | None = typer.Option(None, "--tmux-session", hidden=True),
) -> None:
    """Run one Claude Code-owned stdio Channel server."""

    require_initialized_hyprial_home()

    import anyio

    from hyprial.mcp import (
        StatelessDaemonProxy,
        UnixDaemonConnectionFactory,
        serve_channel_stdio,
    )

    async def run() -> None:
        proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(_socket_path()))
        await serve_channel_stdio(
            proxy,
            actor=actor,
            session_ref=session_ref,
            cwd=str(_resolved_agent_cwd(actor, cwd)),
            command=tuple(command or ("claude",)),
            logger=Logger.worker(_state_dir(), runtime="claude", name=actor),
            poll_interval=poll_interval,
            recovery_signal=recovery_signal,
            owner_pid=owner_pid,
            owner_identity=owner_identity,
            tmux_session=tmux_session,
        )

    # stdout belongs exclusively to the MCP stdio wire.
    anyio.run(run)


@mcp_app.command("claude-channel-recover", hidden=True)
def mcp_claude_channel_recover(
    signal_path: Path = typer.Option(..., "--signal-path"),
) -> None:
    """Pulse one running Claude Channel after a lifecycle/model failure."""

    require_initialized_hyprial_home()

    from hyprial.mcp.channel import signal_channel_recovery

    signal_channel_recovery(signal_path.expanduser().resolve())


@mcp_app.command("agent-channel")
def mcp_agent_channel(
    actor: str = typer.Option(..., "--actor"),
    session_ref: str = typer.Option(..., "--session-ref"),
) -> None:
    """Run one managed-worker stdio MCP server with a fixed daemon identity."""

    require_initialized_hyprial_home()

    import anyio

    from hyprial.mcp import (
        StatelessDaemonProxy,
        UnixDaemonConnectionFactory,
        serve_worker_stdio,
    )

    async def run() -> None:
        proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(_socket_path()))
        await serve_worker_stdio(proxy, actor=actor, session_ref=session_ref)

    # stdout belongs exclusively to the MCP stdio wire.
    anyio.run(run)


_CLAUDE_HARNESS_TOOLS = (
    "harness_ack",
    "harness_progress",
    "harness_read",
    "harness_reply",
    "harness_send",
    "harness_targets",
    "harness_whoami",
)


# Claude session-identity flags are owned by hyprial so that the Harness session
# ref and the Claude session id are decided in exactly one place. Resuming goes
# through the first-class `hyprial start claude --resume <session-id>` flag.
_CLAUDE_SESSION_IDENTITY_FLAGS = frozenset(
    {"--session-id", "--resume", "--continue", "-c", "--fork-session"}
)


def _reject_claude_session_identity_args(runtime_args: tuple[str, ...]) -> None:
    for arg in runtime_args:
        flag = arg.split("=", 1)[0]
        if flag in _CLAUDE_SESSION_IDENTITY_FLAGS:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"{flag} changes Claude session identity, which hyprial owns; "
                "resume a session with `hyprial start claude --resume <session-id>`",
            )


def _wait_foreground(process: subprocess.Popen[Any]) -> int:
    """Wait for the interactive TUI while leaving Ctrl-C to the child.

    The child shares this process's terminal and process group, so SIGINT
    already reaches the TUI directly. Ignoring SIGINT here keeps a turn
    interrupt inside Claude from tearing the whole session down through this
    supervisor's error path.
    """

    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return process.wait()
    finally:
        signal.signal(signal.SIGINT, previous)


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _interactive_actor(name: str, status: JsonObject) -> str:
    """Mint the canonical ``agent:<owner>:<machine>:<actor>`` for ``--name``.

    Pins, harnesses, and delivery addressing all speak the four-segment
    URI, and launch-time code that must *compare* against daemon truth
    (registration polling, tmux session names, result payloads) needs the
    minted spelling. The daemon's ``ps`` response is the source of truth for
    owner and node id, with the daemon's own fallbacks
    (``HYPRIAL_OWNER``/login, ``HYPRIAL_NODE_ID``/hostname) mirrored for older
    daemons that do not report them yet. A name that already is a
    four-segment URI passes through; any other colon form is rejected
    loudly instead of registering a third spelling.

    ⛔ Never persist this value: a minted address is derived state, and
    freezing it into an mcp-config or argv is what stranded still-running
    channels on a dead owner segment after the #352 owner switch. Anything
    that outlives the launch must carry the bare name
    (:func:`_interactive_actor_name`) and let the daemon mint per call.
    """

    if ":" not in name:
        from hyprial.daemon.identity import resolve_node_owner
        from hyprial.uri import canonical_agent_uri

        daemon = status.get("daemon") if isinstance(status, dict) else None
        node_id = ""
        owner = ""
        if isinstance(daemon, dict):
            node_id = str(daemon.get("nodeId") or "").strip()
            owner = str(daemon.get("owner") or "").strip()
        if not node_id:
            node_id = os.environ.get("HYPRIAL_NODE_ID", "").strip() or socket.gethostname()
        if not owner:
            owner = resolve_node_owner()
        return canonical_agent_uri(owner, node_id, name)
    from hyprial.uri import agent_uri_actor

    if agent_uri_actor(name) is not None:
        return name
    raise CliError(
        ipc_errors.INVALID_ARGUMENT,
        f"--name {name!r} is neither a bare actor name nor a canonical "
        "agent:<owner>:<machine>:<actor> URI",
    )


def _interactive_actor_name(name: str) -> str:
    """Reduce ``--name`` to the bare actor name; validation only, never mint.

    The value that outlives a launch -- the mcp-config JSON and the channel
    argv -- is the operator's input, not a derived address: the daemon's
    boundary (``_canonical_interactive_actor``) mints the canonical
    four-segment URI per call from its own current owner/node, so an owner
    change never strands a still-running channel on a stale spelling. A
    four-segment URI still passes validation and reduces to its short name.
    """

    if ":" not in name:
        return name
    from hyprial.uri import agent_uri_actor

    short = agent_uri_actor(name)
    if short is not None:
        return short
    raise CliError(
        ipc_errors.INVALID_ARGUMENT,
        f"--name {name!r} is neither a bare actor name nor a canonical "
        "agent:<owner>:<machine>:<actor> URI",
    )


@agent_app.command("create")
def agent_create(
    name: str = typer.Option(..., "--name", help="Actor name, unique on this machine."),
    cwd: Path | None = typer.Option(None, "--cwd", help="Default working directory."),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Single explicit personality config directory (C).",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Model vendor preference (the squire harness/provider/model vocabulary).",
    ),
    model: str | None = typer.Option(None, "--model", help="Model id preference."),
    preferred_harness: str | None = typer.Option(
        None,
        "--preferred-harness",
        help=(
            "Default harness for 'hyprial start'. Only a default: the agent is not "
            "bound to it and may be started on any harness."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Register a new agent on this machine.

    The name is the agent's identity within agent:<owner>:<machine>:, and only
    one agent may hold it -- creating a second agent under a name already in
    use fails, whether or not anything is currently running under it. The name
    is freed only by 'hyprial agent destroy'.

    A name says nothing about a harness: an agent called 'pi-ds4' is just an
    agent called 'pi-ds4', and it can run on claude. To move an existing agent
    to another harness, start it there; that rebinds the same agent rather
    than creating a new one, so it is not a name conflict.
    """

    def operation() -> Any:
        params: JsonObject = {"name": name}
        if cwd is not None:
            params["cwd"] = str(cwd.expanduser().resolve())
        if config is not None:
            params["config"] = {
                "sources": [
                    {"path": str(config.expanduser().resolve()), "required": True}
                ],
                "discovery": "explicit-only",
            }
        if provider is not None:
            # Wire key for the model vendor, as squire spells it.
            params["provider"] = provider
        if model is not None:
            params["model"] = model
        if preferred_harness is not None:
            params["preferredHarness"] = preferred_harness
        return _daemon_request("agent.create", params)

    _execute(operation, json_output=json_output)


@agent_app.command("host-invite")
def agent_host_invite(
    name: str = typer.Argument(..., help="Actor name, unique on this host."),
    owner: str = typer.Option(..., "--owner", help="Visitor's owner identity, asserted by this host."),
    cwd: Path | None = typer.Option(None, "--cwd", help="Default working directory."),
    preferred_harness: str | None = typer.Option(None, "--preferred-harness", help="Preferred harness for later starts."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Register a trusted visitor's agent; does not launch or isolate a worker."""

    def operation() -> Any:
        params: JsonObject = {"name": name, "owner": owner}
        if cwd is not None:
            params["cwd"] = str(cwd.expanduser().resolve())
        if preferred_harness is not None:
            params["preferredHarness"] = preferred_harness
        return _daemon_request("agent.host-invite", params)

    _execute(operation, json_output=json_output)


@agent_app.command("grant")
def agent_grant(
    actor: str = typer.Argument(..., help="Agent instance name."),
    capability: str = typer.Option(..., "--capability", help="Capability name from the grant schema."),
    scope: str = typer.Option(..., "--scope", help="Single-line capability scope; arrays/paths use JSON."),
    grant_id: str | None = typer.Option(None, "--grant-id", help="Stable id; defaults to a new UUID."),
    revision: int = typer.Option(1, "--revision", help="Must increase when updating an existing id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Record a capability grant. Recording does not enforce runtime permissions."""
    _execute(
        lambda: _daemon_request("agent.grant", {
            "actor": actor, "capability": capability, "scope": scope,
            "grantId": grant_id if grant_id is not None else str(uuid4()), "revision": revision,
        }), json_output=json_output,
    )


@agent_app.command("revoke")
def agent_revoke(
    actor: str = typer.Argument(..., help="Agent instance name."),
    grant_id: str = typer.Argument(..., help="Grant id returned by agent grant."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove an active capability record and retain its audit history."""
    _execute(
        lambda: _daemon_request("agent.revoke", {"actor": actor, "grantId": grant_id}),
        json_output=json_output,
    )


@agent_app.command("grants")
def agent_grants(
    actor: str | None = typer.Argument(None, help="Agent name; required with --audit."),
    audit: bool = typer.Option(False, "--audit", help="Show history, including destroyed incarnations."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List capability records, or an agent's append-only audit history."""
    _execute(
        lambda: _daemon_request("agent.grants", {
            **({"actor": actor} if actor is not None else {}), "audit": audit,
        }), json_output=json_output,
    )


@agent_app.command("list")
def agent_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List this machine's agents with their real online/offline status."""

    _execute(lambda: _daemon_request("agent.list"), json_output=json_output)


@agent_app.command("destroy")
def agent_destroy(
    name: str = typer.Argument(..., help="Actor name of the agent to destroy."),
    yes: bool = typer.Option(
        False, "--yes", help="Confirm this irreversible deletion."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Permanently delete an agent. THIS CANNOT BE UNDONE.

    Destroying an agent stops its connectors, deletes its record, and discards
    its undelivered messages. There is no tombstone and no revival: messages
    already sent to this agent lose a resolvable recipient, and recreating the
    same name later gives you a new, empty agent -- not this one back.
    Interactive terminals may confirm after seeing the workspace inventory;
    non-interactive callers must pass --yes.
    """

    def operation() -> Any:
        if not yes:
            if json_output or not _stdin_isatty():
                raise CliError(
                    "CONFIRMATION_REQUIRED",
                    f"destroying agent {name!r} from a non-interactive command "
                    "requires --yes",
                )
            preview = _daemon_request("agent.destroy.preview", {"name": name})
            workspace = preview.get("workspace")
            if not isinstance(workspace, dict):
                raise CliError(
                    "INVALID_RESPONSE",
                    "agent.destroy.preview must return a workspace inventory",
                )
            files = workspace.get("files")
            size = workspace.get("bytes")
            if not isinstance(files, int) or not isinstance(size, int):
                raise CliError(
                    "INVALID_RESPONSE",
                    "agent.destroy.preview returned an invalid workspace inventory",
                )
            prompt = (
                f"将删除 workspace({files} 个文件、{size} 字节)，以及 agent "
                f"{name!r} 的记录、连接器和未送达消息。继续？"
            )
            if not typer.confirm(prompt):
                raise CliError("CANCELLED", "agent destroy cancelled")
        return _daemon_request("agent.destroy", {"name": name})

    _execute(operation, json_output=json_output)


@secret_app.command("provider-" + "write")
def agent_secret_entry_write(
    entry_id: str = typer.Argument(..., help="Secret entry id."),
    field_name: str = typer.Option(..., "--field", help="JSON field name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Write one model-vendor secret from a non-TTY stdin stream."""

    def operation() -> Any:
        if sys.stdin.isatty():
            raise CliError(
                "SECRET_REQUIRED",
                "secret write refuses a TTY; pipe exactly one secret value on stdin",
            )
        value = sys.stdin.read()
        if value.endswith("\n"):
            value = value[:-1]
        if not value or "\n" in value or "\r" in value:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "secret write requires one non-empty line on stdin",
            )
        return _daemon_request(
            "agent.secret." + "provider-write",
            {"entryId": entry_id, "fieldName": field_name, "value": value},
        )

    _execute(operation, json_output=json_output)


@secret_app.command("grant")
def agent_secret_grant(
    actor: str = typer.Argument(..., help="Agent instance name."),
    grant_id: str = typer.Option(..., "--grant-id", help="Stable grant id."),
    source: str = typer.Option(..., "--source", help="user-" + "provider or agent-private"),
    entry_id: str = typer.Option(..., "--entry-id", help="Secret entry id."),
    field_name: str = typer.Option(..., "--field-name", help="Model-vendor field name."),
    environment_name: list[str] = typer.Option(
        ..., "--environment-name", help="Approved environment variable name; repeatable."
    ),
    revision: int = typer.Option(..., "--revision", help="Positive grant revision."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Grant one named secret entry to one current agent incarnation."""

    _execute(
        lambda: _daemon_request(
            "agent.secret.grant",
            {
                "actor": actor,
                "grantId": grant_id,
                "source": source,
                "entryId": entry_id,
                "fieldName": field_name,
                "environmentNames": environment_name,
                "revision": revision,
            },
        ),
        json_output=json_output,
    )


@secret_app.command("list")
def agent_secret_list(
    actor: str | None = typer.Option(None, "--actor", help="Filter by agent instance."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List non-secret grant metadata."""

    _execute(
        lambda: _daemon_request(
            "agent.secret.list", {**({"actor": actor} if actor else {})}
        ),
        json_output=json_output,
    )


@secret_app.command("revoke")
def agent_secret_revoke(
    actor: str = typer.Argument(..., help="Agent instance name."),
    grant_id: str = typer.Argument(..., help="Grant id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Revoke one grant; the underlying secret entry remains intact."""

    _execute(
        lambda: _daemon_request(
            "agent.secret.revoke", {"actor": actor, "grantId": grant_id}
        ),
        json_output=json_output,
    )


def _create_agent_for_start(
    *,
    name: str,
    harness: str,
    runtime: str,
    cwd: Path,
    provider: str | None = None,
    model: str | None = None,
) -> JsonObject:
    """Route ``hyprial start`` through ``agent create`` before launching anything.

    Decision A5 keeps ``hyprial start`` working exactly as before but makes it go
    through agent creation, so a connector can never come up without an
    identity behind it. ``existing: reuse`` is what separates the two entry
    points: an explicit ``hyprial agent create`` on a taken name is an error, while
    ``start`` on an existing agent is the normal case -- it is starting *that*
    agent, possibly on a different harness than last time.

    Failing here also means a duplicate is refused before a TUI is spawned.
    """

    params: JsonObject = {
        "name": name,
        "existing": "reuse",
        "harness": harness,
        "runtime": runtime,
        "cwd": str(cwd),
    }
    if provider is not None:
        params["provider"] = provider
    if model is not None:
        params["model"] = model
    return _daemon_request("agent.create", params)


def _handover_prompt(result: JsonObject) -> str | None:
    """The A9 notice to put in front of the agent's first turn, if any."""

    handover = result.get("harnessHandover")
    if not isinstance(handover, dict):
        return None
    notice = handover.get("notice")
    return notice if isinstance(notice, str) and notice else None


def _runtime_context_environment(
    *, name: str, harness: str, cwd: Path, include_projection: bool = False
) -> JsonObject | None:
    projection = _runtime_context_projection(name=name, harness=harness, cwd=cwd)
    if projection is None:
        return None
    return (
        dict(projection)
        if include_projection
        else dict(projection["environment"])
    )


def _runtime_context_projection(
    *, name: str, harness: str, cwd: Path
) -> JsonObject | None:
    """Ask the daemon for one non-secret P2 root/profile projection.

    ``None`` is the explicit legacy mode.  The response is intentionally a
    string map: entity tokens, grants, credential values, and receipts never
    cross this CLI IPC seam.
    """

    result = _daemon_request(
        "agent.runtime-context",
        {"name": name, "harness": harness, "cwd": str(cwd)},
    )
    if result.get("mode") == "legacy":
        return None
    if result.get("mode") != "agent-home-p2":
        raise CliError(
            ipc_errors.INVALID_ARGUMENT,
            "daemon returned an unsupported agent runtime context mode",
        )
    environment = result.get("environment")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise CliError(
            ipc_errors.INVALID_ARGUMENT,
            "daemon returned an invalid agent runtime environment",
        )
    for field in ("projectionRoot", "nativeRoot", "sessionRoot"):
        value = result.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"daemon returned an invalid agent runtime {field}",
            )
    return result


def _start_interactive_claude(
    *,
    name: str,
    nickname: str | None,
    cwd: Path,
    resume: str | None,
    runtime_args: tuple[str, ...],
    model_provider: str | None,
    model: str | None,
    json_output: bool,
    tmux: bool = False,
) -> JsonObject:
    """Launch a real CC TUI with one session-owned Harness Channel server."""

    # Argument validation stays ahead of every daemon call: a rejected launch
    # must not have touched the daemon at all.
    _reject_claude_session_identity_args(runtime_args)
    # The persisted identity is the operator's input (the bare name); the
    # daemon mints the canonical URI per call. Never freeze a minted
    # address into the config or argv that outlives this launch (#352).
    actor_name = _interactive_actor_name(name)
    # A5: the agent exists before the TUI does. This is also where a duplicate
    # name is refused -- before a Claude process has been spawned.
    from hyprial.daemon.desired_state import HarnessLaunchSpec
    from hyprial.harnesses.model_provider import claude_provider_environment

    provider_spec = HarnessLaunchSpec(
        "claude",
        name,
        False,
        args=runtime_args,
        model_provider=model_provider,
        model=model,
    )
    native_model_args = (
        ("--model", model)
        if model is not None and model_provider in {None, "anthropic"}
        else ()
    )
    handover = _handover_prompt(
        _create_agent_for_start(
            name=name,
            harness="claude",
            runtime="interactive",
            cwd=cwd,
            provider=model_provider,
            model=model,
        )
    )
    runtime_environment = _runtime_context_environment(
        name=name, harness="claude", cwd=cwd
    )
    from hyprial.agents.environment import apply_runtime_environment_profile
    from hyprial.harnesses.claude_runtime import (
        CLAUDE_RUNTIME_ENVIRONMENT,
        ClaudeRuntimeError,
        validate_claude_auth_environment,
    )

    profile_base = apply_runtime_environment_profile(
        os.environ, runtime_environment
    )
    provider_environment = claude_provider_environment(
        provider_spec,
        os.environ if runtime_environment is None else profile_base,
        allow_legacy_home_fallback=runtime_environment is None,
    )
    launch_environment = apply_runtime_environment_profile(
        os.environ,
        runtime_environment,
        provider_environment,
        CLAUDE_RUNTIME_ENVIRONMENT if runtime_environment is not None else {},
    )
    if runtime_environment is not None:
        native_root = runtime_environment.get("CLAUDE_CONFIG_DIR")
        if not native_root:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "Claude P2 runtime context is missing CLAUDE_CONFIG_DIR",
            )
        try:
            validate_claude_auth_environment(Path(native_root), launch_environment)
        except ClaudeRuntimeError as error:
            raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    status = _daemon_request("ps")
    actor = _interactive_actor(name, status)
    from hyprial.uri import agent_uri_actor

    display_name = nickname or agent_uri_actor(actor) or actor
    # Single identity decision: a fresh session gets a new ref; a resume reuses
    # the target session as the ref. The Harness ref and the Claude session id
    # are therefore always the same value and can never collide.
    session_ref = resume if resume is not None else str(uuid4())
    # M2: the attached TUI (and every shell it runs) carries the carrier's
    # daemon-bound identity, so its workflow commands writes ride the fenced
    # path instead of falling to the human identity.
    launch_environment = {
        **launch_environment,
        "HYPRIAL_WORKER_ACTOR": actor,
        "HYPRIAL_WORKER_SESSION_REF": session_ref,
        "HYPRIAL_MANAGED_WORKER": "1",
    }
    identity_args = (
        ["--resume", session_ref]
        if resume is not None
        else ["--session-id", session_ref]
    )
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The config file belongs to this launch, not to the session: a resumed
    # session may still have a stale file from a crashed earlier launch.
    config_path = state_dir / f"claude-channel-{session_ref}-{uuid4().hex[:8]}.json"
    recovery_path = config_path.with_suffix(".recover")
    server_args = [
        "-m",
        "hyprial.cli",
        "mcp",
        "claude-channel",
        "--actor",
        actor_name,
        "--session-ref",
        session_ref,
        "--cwd",
        str(cwd),
        "--command",
        "claude",
        "--recovery-signal",
        str(recovery_path),
    ]
    tmux_session_name: str | None = None
    if tmux:
        from hyprial.harnesses.tmux import session_name_for_actor

        # Detached mode: this launcher exits right after spawn, so it can
        # never be the channel's owner fence. The carrier receives the tmux
        # session name instead -- the daemon records it on the registration
        # and the carrier fences on the pane's top process, which lives
        # exactly as long as the tmux session.
        tmux_session_name = session_name_for_actor(actor)
        server_args.extend(["--tmux-session", tmux_session_name])
    else:
        # The launcher lives exactly as long as the Claude process it waits
        # for. Passing both PID and birth marker lets a slowly starting
        # channel detect owner death even after reparenting, without
        # mistaking PID reuse for life.
        from hyprial.mcp.channel import _read_process_identity

        owner_pid = os.getpid()
        owner_identity = _read_process_identity(owner_pid)
        if owner_identity is not None:
            server_args.extend(
                [
                    "--owner-pid",
                    str(owner_pid),
                    "--owner-identity",
                    owner_identity,
                ]
            )
    config = {
        "mcpServers": {
            "harness-bridge": {
                "type": "stdio",
                "command": sys.executable,
                "args": server_args,
                # Pin the stdio child to the same daemon even when the parent
                # shell carries an ambient production socket override.
                "env": child_state_environment(_hyprial_home(), state_dir),
            }
        }
    }
    from hyprial.plugins import (
        PluginManifestError,
        claude_plan,
        load_manifest,
        materialize_claude_skill_plugin,
    )

    try:
        plugin_plan = claude_plan(load_manifest(_hyprial_home()))
    except PluginManifestError as error:
        raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    plugin_warnings = _plugin_skip_warnings(plugin_plan.skipped)
    _announce_plugin_skips(plugin_warnings)
    # HYPRIAL_HOME-declared MCP servers ride the same per-launch --mcp-config file
    # as the channel server, so a session never depends on the launch
    # directory's per-project registration in ~/.claude.json.
    config["mcpServers"].update(plugin_plan.mcp_servers)
    # Named after this launch's config file so a crashed earlier launch can
    # never hand its stale skill payload to the next one.
    skill_plugin_dir = materialize_claude_skill_plugin(
        plugin_plan.skill_dirs,
        state_dir / f"{config_path.stem}-skills",
    )
    plugin_dir_args: list[str] = []
    for plugin_dir in (skill_plugin_dir, *plugin_plan.plugin_dirs):
        plugin_dir_args.extend(["--plugin-dir", str(plugin_dir)])
    settings = {
        "permissions": {
            "allow": [f"mcp__harness-bridge__{tool}" for tool in _CLAUDE_HARNESS_TOOLS]
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "hyprial.cli",
                                "mcp",
                                "claude-channel-recover",
                                "--signal-path",
                                str(recovery_path),
                            ],
                        }
                    ],
                }
            ],
            "StopFailure": [
                {
                    "matcher": (
                        "rate_limit|overloaded|authentication_failed|"
                        "oauth_org_not_allowed|billing_error|invalid_request|"
                        "model_not_found|server_error|max_output_tokens|unknown"
                    ),
                    "hooks": [
                        {
                            "type": "command",
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "hyprial.cli",
                                "mcp",
                                "claude-channel-recover",
                                "--signal-path",
                                str(recovery_path),
                            ],
                        }
                    ],
                }
            ],
        },
    }
    argv = [
        os.environ.get("HARNESS_CLAUDE_BIN", "claude"),
        *native_model_args,
        *runtime_args,
        *identity_args,
        "--name",
        display_name,
        "--mcp-config",
        str(config_path),
        *(
            ("--setting-sources", "user,project,local")
            if runtime_environment is not None
            else ()
        ),
        # Project MCP files remain discoverable inputs, but cannot add or
        # replace servers for this managed session.  The per-launch config
        # contains the daemon-bound Harness server plus explicitly approved
        # plugin-manifest servers.
        "--strict-mcp-config",
        *plugin_dir_args,
        "--settings",
        json.dumps(settings, separators=(",", ":")),
        # Channels are opt-in even when an MCP server declares the preview
        # capability. The local-development confirmation remains CC-owned.
        "--dangerously-load-development-channels",
        "server:harness-bridge",
        "--append-system-prompt",
        (
            "Harness Network is connected through the harness-bridge MCP server. "
            "When a Harness channel notification arrives, immediately call "
            "harness_read and handle every pending message in FIFO order. Reply to "
            "requests with harness_reply; use harness_ack only when no reply is "
            "required. Never treat the channel notification itself as message body."
            # A9: a harness swap starts the conversation from zero. Say so up
            # front, before the first turn, together with the previous harness
            # and session id -- the handover is allowed to cost context, it is
            # not allowed to happen silently.
            + (f"\n\n{handover}" if handover else "")
        ),
    ]
    process: subprocess.Popen[Any] | None = None
    if tmux_session_name is not None:
        return _launch_detached_tui(
            argv=argv,
            env=launch_environment,
            cwd=cwd,
            actor=actor,
            session_ref=session_ref,
            session_name=tmux_session_name,
            harness="claude",
            registration_deadline_seconds=60.0,
            is_session_registered=lambda status: _channel_registration_confirmed(
                actor, session_ref, status
            ),
            registration_failure=(
                f"Claude exited or timed out before Channel registration for {actor}"
            ),
            config=config,
            config_path=config_path,
            recovery_path=recovery_path,
            warnings=plugin_warnings,
        )
    try:
        _write_launch_config(config_path, config)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=launch_environment,
            stdout=sys.stderr if json_output else None,
        )
        registered = False
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            status = _daemon_request("ps")
            sessions = status.get("interactiveSessions", [])
            registered = isinstance(sessions, list) and any(
                isinstance(item, dict)
                and item.get("actor") == actor
                and item.get("sessionRef") == session_ref
                and item.get("channelConfirmed") is True
                for item in sessions
            )
            if registered or process.poll() is not None:
                break
            time.sleep(0.1)
        if not registered:
            if process.poll() is None:
                _terminate_process(process)
            else:
                process.wait()
            raise CliError(
                "CHANNEL_REGISTRATION_FAILED",
                f"Claude exited or timed out before Channel registration for {actor}",
            )
        returncode = _wait_foreground(process)
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)
        from hyprial.harnesses._launch_cleanup import cleanup_launch_resources

        cleanup_launch_resources(config_path, recovery_path)
    return _with_plugin_warnings(
        {
            "ok": returncode == 0,
            "actor": actor,
            "provider": "claude",
            "runtime": "interactive",
            "sessionRef": session_ref,
            "runtimeExitCode": returncode,
        },
        plugin_warnings,
    )


def _channel_registration_confirmed(
    actor: str, session_ref: str, status: JsonObject
) -> bool:
    """Readiness for the Claude Channel carrier: registered AND confirmed."""

    sessions = status.get("interactiveSessions", [])
    return isinstance(sessions, list) and any(
        isinstance(item, dict)
        and item.get("actor") == actor
        and item.get("sessionRef") == session_ref
        and item.get("channelConfirmed") is True
        for item in sessions
    )


def _pi_attach_registration(actor: str, session_ref: str, status: JsonObject) -> bool:
    """Readiness for the pi attach carrier: its session.register has landed."""

    sessions = status.get("interactiveSessions", [])
    return isinstance(sessions, list) and any(
        isinstance(item, dict)
        and item.get("actor") == actor
        and item.get("sessionRef") == session_ref
        and item.get("source") == "pi-extension"
        for item in sessions
    )


def _write_launch_config(config_path: Path, config: JsonObject) -> None:
    """Write the launch-scoped mcp config, mode 0600, refusing overwrites."""

    descriptor = os.open(
        config_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _launch_detached_tui(
    *,
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    actor: str,
    session_ref: str,
    session_name: str,
    harness: str,
    registration_deadline_seconds: float,
    is_session_registered: Callable[[JsonObject], bool],
    registration_failure: str,
    config: JsonObject | None = None,
    config_path: Path | None = None,
    recovery_path: Path | None = None,
    warnings: Sequence[JsonObject] = (),
) -> JsonObject:
    """Spawn an interactive TUI inside a detached tmux session, then return.

    Unlike the foreground path this launcher does NOT wait for the TUI: the
    tmux server owns the pane, attach/detach never signals it, and an
    optional launch-scoped mcp config stays on disk so an in-session MCP
    reconnect can still read it after this process exits. Registration is
    still the readiness gate: a session that never registers is killed again,
    exactly like the foreground spawn-failure path. ``harness`` decides the
    registration predicate (Claude: channelConfirmed; pi: the extension's
    session.register) and the deadline (pi's trust prompt stalls longer).
    """

    from hyprial.harnesses import tmux as tmux_mod

    tmux_bin = tmux_mod.find_tmux()
    if tmux_bin is None:
        if config_path is not None:
            from hyprial.harnesses._launch_cleanup import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
        raise CliError(
            "TMUX_UNAVAILABLE",
            "--tmux requires the tmux binary on PATH",
        )
    if tmux_mod.has_session(tmux_bin, session_name):
        if config_path is not None:
            from hyprial.harnesses._launch_cleanup import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
        hints = tmux_mod.attach_hints(session_name)
        raise CliError(
            "TMUX_SESSION_EXISTS",
            f"tmux session {session_name} already exists; attach to it with "
            f"`{hints['wsl-linux-terminal']}` (WSL/Linux) or "
            f"`{hints['macOS-iTerm2']}` (macOS iTerm2), or kill it first",
        )
    if config is not None:
        if config_path is None:
            raise ValueError("config_path is required when config is given")
        try:
            _write_launch_config(config_path, config)
        except BaseException:
            from hyprial.harnesses._launch_cleanup import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
            raise
    try:
        cleanup = (
            tmux_mod.LaunchCleanupResources(config_path, recovery_path)
            if config_path is not None
            else None
        )
        tmux_mod.new_detached_session(
            tmux_bin,
            session_name,
            argv,
            cwd=cwd,
            env=env,
            cleanup=cleanup,
        )
    except subprocess.CalledProcessError as error:
        if config_path is not None:
            from hyprial.harnesses._launch_cleanup import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
        detail = (error.stderr or "").strip()
        raise CliError(
            "TMUX_SPAWN_FAILED",
            f"tmux failed to start detached session {session_name}"
            + (f": {detail}" if detail else ""),
        ) from error
    registered = False
    deadline = time.monotonic() + registration_deadline_seconds
    next_pane_read = time.monotonic()
    while time.monotonic() < deadline:
        registered = is_session_registered(_daemon_request("ps"))
        # A dead tmux session is this path's process.poll(): the TUI exited
        # before its carrier ever registered.
        if registered or not tmux_mod.has_session(tmux_bin, session_name):
            break
        if time.monotonic() >= next_pane_read:
            next_pane_read = time.monotonic() + 1.0
            prompt = tmux_mod.claude_confirmation_prompt(
                tmux_mod.pane_text(tmux_bin, session_name)
            )
            if prompt is not None:
                # A Claude Code start-up confirmation (folder trust, or the
                # development-channels warning).  Both are CC-owned on purpose
                # and cannot be pre-accepted, so nobody in a detached pane will
                # ever answer.  Waiting out the deadline and killing the pane
                # (the old path) reported the wrong failure and destroyed the
                # one pane a person could confirm in.  Keep it (and its launch
                # config) and say exactly what to do.
                hints = tmux_mod.attach_hints(session_name)
                question = {
                    "folder-trust": "whether to trust the working folder",
                    "development-channels": "its 'Loading development channels' warning",
                }[prompt]
                raise CliError(
                    "CLAUDE_CONFIRMATION_REQUIRED",
                    f"{harness} in tmux session {session_name} is waiting for a "
                    f"person to confirm {question}; attach with "
                    f"`{hints['wsl-linux-terminal']}` (iTerm2: "
                    f"`{hints['macOS-iTerm2']}`), confirm, then detach -- the "
                    "session registers with the daemon on its own",
                    {
                        "prompt": prompt,
                        "actor": actor,
                        "sessionRef": session_ref,
                        "tmuxSession": session_name,
                        "attach": hints,
                    },
                )
        time.sleep(0.1)
    if not registered:
        tmux_mod.kill_session(tmux_bin, session_name)
        if config_path is not None:
            from hyprial.harnesses._launch_cleanup import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
        raise CliError("CHANNEL_REGISTRATION_FAILED", registration_failure)
    return _with_plugin_warnings(
        {
            "ok": True,
            "actor": actor,
            # The IPC key stays "provider" so this CLI can talk to a daemon (and
            # reports) running an older build.
            "provider": harness,
            "runtime": "interactive",
            "sessionRef": session_ref,
            "detached": True,
            "tmuxSession": session_name,
            "attach": tmux_mod.attach_hints(session_name),
        },
        warnings,
    )


# Pi session-identity and mode flags are owned by hyprial on an attached
# launch: the Harness session ref and the pi session id are decided in
# exactly one place, and the TUI must stay in its default interactive mode.
_PI_SESSION_IDENTITY_FLAGS = frozenset(
    {
        "--session-id",
        "--session",
        "--fork",
        "--continue",
        "-c",
        "--resume",
        "-r",
        "--mode",
        "--print",
        "-p",
        "--no-session",
    }
)


def _reject_pi_session_identity_args(runtime_args: tuple[str, ...]) -> None:
    for arg in runtime_args:
        flag = arg.split("=", 1)[0]
        if flag in _PI_SESSION_IDENTITY_FLAGS:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"{flag} changes pi session identity or mode, which hyprial owns "
                "on an attached launch",
            )


def _start_interactive_pi(
    *,
    name: str,
    nickname: str | None,
    cwd: Path,
    runtime_args: tuple[str, ...],
    model_provider: str | None,
    model: str | None,
    json_output: bool,
    tmux: bool = False,
) -> JsonObject:
    """Launch a real pi TUI with the hyprial attach carrier extension loaded.

    Same five phases as the Claude interactive launcher (design
    notes/design-tui-launcher.md): prepare (validate + agent.create +
    identity), spawn_tui (foreground, terminal inherited; or a detached
    tmux session with --tmux), attach_and_register (poll ps until the
    carrier's session.register lands), wait_foreground, teardown (terminate
    stragglers; the carrier's own session_shutdown unregister is the normal
    exit path and the daemon TTL reclaims anything else).

    Detached mode needs no claude-style owner-fence rewiring: the pi carrier
    is an IN-PROCESS extension, so its lifetime already equals the TUI
    process's -- which under tmux is the pane's. kill-session SIGHUPs the
    pane, pi dies, the carrier dies with it (session_shutdown unregisters
    best-effort, the daemon TTL reclaims the rest). The launcher only
    teaches the carrier the session name (HYPRIAL_WORKER_TMUX_SESSION) so the
    daemon can record it on the registration.
    """

    from hyprial.daemon.desired_state import HarnessLaunchSpec
    from hyprial.harnesses.model_provider import pi_model_args
    from hyprial.uri import agent_uri_actor
    from hyprial.harnesses.pi import PI_HARNESS_ATTACH_EXTENSION
    from hyprial.harnesses.pi_session import pi_session_id

    # Argument validation stays ahead of every daemon call: a rejected launch
    # must not have touched the daemon at all.
    _reject_pi_session_identity_args(runtime_args)
    provider_spec = HarnessLaunchSpec(
        "pi",
        name,
        False,
        args=runtime_args,
        model_provider=model_provider,
        model=model,
    )
    selected_args = pi_model_args(provider_spec)
    handover = _handover_prompt(
        _create_agent_for_start(
            name=name,
            harness="pi",
            runtime="interactive",
            cwd=cwd,
            provider=model_provider,
            model=model,
        )
    )
    runtime_projection = _runtime_context_environment(
        name=name, harness="pi", cwd=cwd, include_projection=True
    )
    runtime_environment = (
        None
        if runtime_projection is None
        else {
            key: value
            for key, value in runtime_projection["environment"].items()
            if isinstance(key, str) and isinstance(value, str)
        }
    )
    status = _daemon_request("ps")
    actor = _interactive_actor(name, status)
    display_name = nickname or agent_uri_actor(actor) or actor
    # Single identity decision: the Harness ref and the pi session id are
    # the same value, translated once at pi's --session-id charset boundary
    # (#192; the raw ref remains the daemon identity key).
    session_ref = str(uuid4())
    state_dir = _state_dir()
    append_system_prompt = (
        "Harness Network is connected through the hyprial pi harness-bridge "
        "extension. Harness messages arrive as user messages prefixed "
        "with [Harness Network ...]; answer them in the transcript and "
        "the bridge sends your final reply back when the turn settles. "
        "Use the harness_send/harness_read/harness_progress/harness_reply/harness_ack/"
        "harness_targets/harness_whoami tools for proactive Harness "
        "Network access." + (f"\n\n{handover}" if handover else "")
        # A9: a harness swap starts the conversation from zero. Say so up
        # front, before the first turn.
    )
    argv = [
        os.environ.get("HARNESS_PI_BIN", "pi"),
        *selected_args,
        *runtime_args,
        "--session-id",
        pi_session_id(session_ref),
        "--name",
        display_name,
        "--extension",
        str(PI_HARNESS_ATTACH_EXTENSION),
        "--append-system-prompt",
        append_system_prompt,
    ]
    from hyprial.plugins import PluginManifestError, load_manifest, pi_plan

    try:
        plugin_plan = pi_plan(load_manifest(_hyprial_home()))
    except PluginManifestError as error:
        raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    plugin_warnings = _plugin_skip_warnings(plugin_plan.skipped)
    _announce_plugin_skips(plugin_warnings)
    # HYPRIAL_HOME-declared payloads ride pi's native session-scoped flags, so a
    # session never depends on launch-directory-local discovery.
    if runtime_projection is None:
        for skill_dir in plugin_plan.skill_dirs:
            argv.extend(["--skill", str(skill_dir)])
        for extension in plugin_plan.extensions:
            argv.extend(["--extension", str(extension)])
    from hyprial.agents.environment import apply_runtime_environment_profile

    environment = apply_runtime_environment_profile(
        os.environ,
        runtime_environment,
        {
        # The carrier's own canonical identity, pinned to THIS daemon's
        # socket (never an ambient production daemon).
        "HYPRIAL_WORKER_ACTOR": actor,
        "HYPRIAL_WORKER_SESSION_REF": session_ref,
        "HYPRIAL_MANAGED_WORKER": "1",
        **child_state_environment(_hyprial_home(), state_dir),
        },
    )
    if runtime_projection is not None:
        from hyprial.harnesses.pi_loader import (
            find_pi_package_root,
            pi_sdk_launch_from_public_projection,
            resolve_approved_pi_project,
        )

        pi_command = (os.environ.get("HARNESS_PI_BIN", "pi"),)
        try:
            trust = resolve_approved_pi_project(
                cwd=str(cwd), runtime_args=runtime_args
            )
            sdk_launch = pi_sdk_launch_from_public_projection(
                runtime_projection,
                trust=trust,
                mode="tui",
                session_id=pi_session_id(session_ref),
                pi_package_root=find_pi_package_root(pi_command, environment),
                model_provider=model_provider,
                model=model,
                additional_extension_paths=(
                    PI_HARNESS_ATTACH_EXTENSION,
                    *plugin_plan.extensions,
                ),
                additional_skill_paths=plugin_plan.skill_dirs,
                append_system_prompt=(append_system_prompt,),
                session_name=display_name,
            )
        except ValueError as error:
            raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        argv = list(sdk_launch.argv)
    if tmux:
        from hyprial.harnesses.tmux import session_name_for_actor

        session_name = session_name_for_actor(actor)
        environment["HYPRIAL_WORKER_TMUX_SESSION"] = session_name
        return _launch_detached_tui(
            argv=argv,
            env=environment,
            cwd=cwd,
            actor=actor,
            session_ref=session_ref,
            session_name=session_name,
            harness="pi",
            # An attended launch may stall on pi's project-trust prompt
            # before the extension ever runs: the deadline is generous, and
            # the error names that exact cause (research finding E8).
            registration_deadline_seconds=180.0,
            is_session_registered=lambda status: _pi_attach_registration(
                actor, session_ref, status
            ),
            registration_failure=(
                "pi exited or timed out before attach registration for "
                f"{actor}; if the project-trust prompt was showing, trust "
                "the project and retry"
            ),
            warnings=plugin_warnings,
        )
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdout=sys.stderr if json_output else None,
        )
        registered = False
        # An attended launch may stall on pi's project-trust prompt before
        # the extension ever runs: the deadline is generous, and the error
        # names that exact cause (research finding E8).
        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline:
            status = _daemon_request("ps")
            sessions = status.get("interactiveSessions", [])
            registered = isinstance(sessions, list) and any(
                isinstance(item, dict)
                and item.get("actor") == actor
                and item.get("sessionRef") == session_ref
                and item.get("source") == "pi-extension"
                for item in sessions
            )
            if registered or process.poll() is not None:
                break
            time.sleep(0.1)
        if not registered:
            if process.poll() is None:
                _terminate_process(process)
            else:
                process.wait()
            raise CliError(
                "CHANNEL_REGISTRATION_FAILED",
                f"pi exited or timed out before attach registration for {actor}; "
                "if the project-trust prompt was showing, trust the project "
                "and retry",
            )
        returncode = _wait_foreground(process)
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)
    return _with_plugin_warnings(
        {
            "ok": returncode == 0,
            "actor": actor,
            "provider": "pi",
            "runtime": "interactive",
            "sessionRef": session_ref,
            "runtimeExitCode": returncode,
        },
        plugin_warnings,
    )


_CODEX_SESSION_FLAGS = frozenset({"--remote", "--remote-auth-token-env"})


def _reject_codex_session_args(runtime_args: tuple[str, ...]) -> None:
    for arg in runtime_args:
        flag = arg.split("=", 1)[0]
        if flag in _CODEX_SESSION_FLAGS:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"{flag} changes the Codex app-server session transport, which "
                "hyprial owns on an attached launch",
            )


def _start_interactive_codex(
    *,
    name: str,
    nickname: str | None,
    cwd: Path,
    runtime_args: tuple[str, ...],
    model_provider: str | None,
    model: str | None,
    json_output: bool,
) -> JsonObject:
    """Launch Codex with a hyprial-owned app-server and remote TUI.

    PR1 owns launch-time discovery, registration, foreground TUI lifetime, and
    clean detach.  The socket carrier's turn/inbox/FIFO behavior lands in PR2.
    """

    from hyprial.daemon.desired_state import HarnessLaunchSpec
    from hyprial.harnesses.codex import (
        CodexAppServerRpcError,
        CodexInteractiveAppServer,
        CodexInteractiveCarrier,
    )
    from hyprial.harnesses.model_provider import codex_provider_configuration

    from hyprial.plugins import PluginManifestError, codex_plan, load_manifest

    _reject_codex_session_args(runtime_args)
    provider_spec = HarnessLaunchSpec(
        "codex",
        name,
        False,
        args=runtime_args,
        model_provider=model_provider,
        model=model,
    )
    try:
        plugin_plan = codex_plan(load_manifest(_hyprial_home()))
    except PluginManifestError as error:
        raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    plugin_warnings = _plugin_skip_warnings(plugin_plan.skipped)
    _announce_plugin_skips(plugin_warnings)
    _create_agent_for_start(
        name=name,
        harness="codex",
        runtime="interactive",
        cwd=cwd,
        provider=model_provider,
        model=model,
    )
    runtime_projection = _runtime_context_projection(
        name=name, harness="codex", cwd=cwd
    )
    runtime_environment = (
        None
        if runtime_projection is None
        else dict(runtime_projection["environment"])
    )
    from hyprial.agents.environment import apply_runtime_environment_profile

    profile_base = apply_runtime_environment_profile(
        os.environ, runtime_environment
    )
    provider_args, provider_environment = codex_provider_configuration(
        provider_spec,
        os.environ if runtime_environment is None else profile_base,
        allow_legacy_home_fallback=runtime_projection is None,
    )
    status = _daemon_request("ps")
    actor = _interactive_actor(name, status)
    codex_bin = os.environ.get("HARNESS_CODEX_BIN", "codex")
    root = Path(tempfile.mkdtemp(prefix="hyprial-codex-attach-", dir="/tmp"))
    socket_path = root / "app.sock"
    session_ref: str | None = None
    registered = False
    server = CodexInteractiveAppServer(
        socket_path,
        cwd=cwd,
        command=(codex_bin, *provider_args),
        env=apply_runtime_environment_profile(
            os.environ,
            runtime_environment,
            child_state_environment(_hyprial_home(), _state_dir()),
            provider_environment,
        ),
        # HYPRIAL_HOME plugin MCP servers ride the app-server's ``-c`` override
        # surface: the server executes tools, the remote TUI does not.
        config_args=plugin_plan.config_args,
        model_provider=model_provider,
        projection_root=(
            None
            if runtime_projection is None
            else Path(str(runtime_projection["projectionRoot"]))
        ),
        native_root=(
            None
            if runtime_projection is None
            else Path(str(runtime_projection["nativeRoot"]))
        ),
        session_root=(
            None
            if runtime_projection is None
            else Path(str(runtime_projection["sessionRoot"]))
        ),
    )
    process: subprocess.Popen[Any] | None = None
    carrier: CodexInteractiveCarrier | None = None
    returncode = 1
    model_args = ("--model", model) if model is not None else ()
    argv = [
        codex_bin,
        *provider_args,
        "--no-alt-screen",
        *model_args,
        *runtime_args,
        "--remote",
        f"unix://{socket_path}",
    ]
    environment = apply_runtime_environment_profile(
        os.environ,
        runtime_environment,
        child_state_environment(_hyprial_home(), _state_dir()),
        provider_environment,
    )
    try:
        server.start()
        process = subprocess.Popen(argv, cwd=cwd, env=environment)
        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise CliError(
                    "CODEX_ATTACH_REGISTRATION_FAILED",
                    f"Codex exited before interactive thread discovery for {actor}",
                )
            try:
                discovered, discovery_mode = server.discover_thread()
            except (ConnectionError, CodexAppServerRpcError):
                time.sleep(0.2)
                continue
            session_ref = discovered
            from hyprial.mcp.channel import _read_process_identity

            process_pid = server.pid
            process_identity = (
                _read_process_identity(process_pid)
                if process_pid is not None
                else None
            )
            process_fence = (
                {
                    "processPid": process_pid,
                    "processIdentity": process_identity,
                }
                if process_pid is not None and process_identity is not None
                else {}
            )
            _daemon_request(
                "session.register",
                {
                    "actor": actor,
                    "cwd": str(cwd),
                    "command": argv,
                    "source": "codex-app-server",
                    "runtime": "codex_interactive",
                    "sessionRef": session_ref,
                    **process_fence,
                },
            )
            registered = True
            carrier = CodexInteractiveCarrier(
                server,
                actor=actor,
                session_ref=session_ref,
                cwd=cwd,
                command=argv,
                daemon_request=lambda method, params: _daemon_request(method, params),
                logger=Logger.worker(_state_dir(), runtime="codex", name=actor),
                state_path=_state_dir() / "codex-interactive-carrier.sqlite3",
                process_pid=process_pid if process_identity is not None else None,
                process_identity=process_identity,
            )
            carrier.start()
            if not json_output:
                print(
                    f"Codex attach registered actor={actor} sessionRef={session_ref} "
                    f"discovery={discovery_mode}",
                    file=sys.stderr,
                )
            break
        else:
            raise CliError(
                "CODEX_ATTACH_REGISTRATION_FAILED",
                f"timed out discovering a Codex TUI thread for {actor}",
            )
        returncode = _wait_foreground(process)
    finally:
        if carrier is not None:
            carrier.stop()
        if registered and session_ref is not None:
            try:
                _daemon_request(
                    "session.unregister",
                    {"actor": actor, "sessionRef": session_ref},
                    restore_wait=0.0,
                )
            except Exception:  # noqa: BLE001, S110 - cleanup must not mask the TUI exit
                pass
        if process is not None and process.poll() is None:
            _terminate_process(process)
        server.stop()
        shutil.rmtree(root, ignore_errors=True)
    return _with_plugin_warnings(
        {
            "ok": returncode == 0,
            "actor": actor,
            "provider": "codex",
            "runtime": "interactive",
            "sessionRef": session_ref,
            "runtimeExitCode": returncode,
        },
        plugin_warnings,
    )


@dispatch_app.command("matrix")
def dispatch_matrix(
    tier: str | None = typer.Option(
        None, "--tier", help="fast, strong, or super."
    ),
    probe: bool = typer.Option(
        False, "--probe", help="Diagnose each candidate; never selects."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Inspect choices, optionally diagnosing; never launch or update a profile."""

    def operation() -> JsonObject:
        from hyprial.dispatch.matrix import (
            TIERS,
            candidate_json,
            diagnose,
            resolve,
        )

        if tier is not None and tier not in TIERS:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "tier must be fast, strong, or super",
            )
        tiers = (tier,) if tier is not None else tuple(TIERS)
        document: JsonObject = {
            "ok": True,
            "tiers": {
                name: [candidate_json(candidate) for candidate in TIERS[name]]
                for name in tiers
            },
            # The static selection, reported so a caller reading this command
            # sees the same answer dispatch would use.  It is not derived from
            # the diagnostics below and carries no readings.
            "selection": {
                name: candidate_json(resolve(name).selected) for name in tiers
            },
        }
        if probe:
            # Explicit human diagnosis only.  Read-only by construction: the
            # readings are reported here and consumed nowhere else.
            document["diagnostics"] = {
                name: [reading.to_json() for reading in diagnose(name)]
                for name in tiers
            }
        return document

    _execute(operation, json_output=json_output)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def start(
    ctx: typer.Context,
    harness_kind: str | None = typer.Argument(None, help="claude, pi, codex, dsh, jev, or user-proxy; optional with --tier."),
    name: str = typer.Option(..., "--name"),
    tier: str | None = typer.Option(None, "--tier", help="Select fast, strong, or super; it chooses harness/provider/model, so it cannot be combined with any of them."),
    nickname: str | None = typer.Option(None, "--nickname"),
    cwd: Path | None = typer.Option(None, "--cwd"),
    headless: bool = typer.Option(False, "--headless"),
    tmux: bool = typer.Option(
        False,
        "--tmux",
        help=(
            "Run the interactive TUI inside a detached tmux session and "
            "return attach commands (interactive claude and pi only)."
        ),
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help=(
            "Resume an existing session by id: interactive claude, or headless "
            "claude, pi, and codex (refused, never a fresh session, when the "
            "session cannot be found or does not hold)."
        ),
    ),
    model_provider: str | None = typer.Option(
        None,
        "--provider",
        help="Model vendor (for example deepseek); separate from the harness kind.",
    ),
    model: str | None = typer.Option(None, "--model", help="Model id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Start a harness connector through the daemon."""

    def operation() -> Any:
        nonlocal harness_kind, model_provider, model, headless

        from hyprial.harnesses.model_provider import (
            ModelProviderError,
            validate_model_selection,
        )

        from hyprial.dispatch.matrix import TIERS

        runtime_args = tuple(ctx.args)
        if tier is not None and tier not in TIERS:
            raise CliError(ipc_errors.INVALID_ARGUMENT, "tier must be fast, strong, or super")
        explicit_selection = (
            harness_kind is not None or model_provider is not None or model is not None
            or any(arg in {"--provider", "--model"} or arg.startswith(("--provider=", "--model="))
                   for arg in runtime_args)
        )
        if tier is not None and explicit_selection:
            # --tier CHOOSES harness/provider/model.  Combined with an explicit
            # one it used to be dropped without a word, starting e.g. a pi with
            # no model vendor or model, silently running the harness's global default
            # (2026-09-21: `start pi --tier super` -> kimi-coding/k3 -> 403).
            # Refuse here, before any daemon call: no agent, no desired state.
            given = [
                label
                for label, present in (
                    (f"harness {harness_kind!r}", harness_kind is not None),
                    ("--provider", model_provider is not None),
                    ("--model", model is not None),
                    (
                        "--provider/--model after '--'",
                        any(
                            arg in {"--provider", "--model"}
                            or arg.startswith(("--provider=", "--model="))
                            for arg in runtime_args
                        ),
                    ),
                )
                if present
            ]
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"--tier {tier} chooses harness/provider/model itself; it "
                f"cannot be combined with {', '.join(given)}. Use either "
                f"`hyprial start --tier {tier} --name ...` or an explicit harness "
                "with --provider/--model",
            )
        if tier is not None and not explicit_selection:
            # Resolve inside the daemon so the audit uses its own runtime
            # profile, not caller-supplied evidence.  Selection is static
            # configuration, so this is one ordinary IPC round-trip: the
            # request no longer waits on any liveness probe (2026-09-21).
            # The read-only `dispatch matrix --probe` never enters this path.
            resolved = _daemon_request(
                "dispatch.matrix.resolve", {"tier": tier, "name": name},
                timeout=_DAEMON_IPC_ROUNDTRIP_SECONDS,
            )
            choice = resolved["selected"]
            harness_kind, model_provider, model = choice["harness"], choice["provider"], choice["model"]
        if harness_kind not in {"claude", "pi", "codex", "dsh", "jev", "user-proxy"}:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "harness kind must be claude, pi, codex, dsh, jev, or user-proxy (or use --tier without explicit model selection)",
            )
        proxy_route: str | None = None
        if harness_kind == "user-proxy":
            # One person's relay (docs/design-user-proxy-harness.md): the only
            # runtime argument is the person's DM route, which is where
            # everyone else's messages are forwarded to.
            if len(runtime_args) != 2 or runtime_args[0] != "--route":
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "user-proxy needs exactly: -- --route route:<adapter>:<route> "
                    "(the person's DM route)",
                )
            from hyprial.uri import parse_route_uri

            proxy_route = runtime_args[1]
            if parse_route_uri(proxy_route) is None:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"--route must be route:<adapter>:<route>, got {proxy_route!r}",
                )
            if tier is not None or model_provider is not None or model is not None:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "user-proxy relays and has no model; --tier, --provider, and --model are not accepted",
                )
            if resume is not None or tmux:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "user-proxy does not support --resume or --tmux",
                )
            runtime_args = ()
            headless = True
        if harness_kind == "jev":
            if runtime_args:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "jev does not accept positional runtime arguments after '--'",
                )
            if tier is not None or model_provider is not None or model is not None:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "jev model selection belongs to each request; --tier, --provider, and --model are not accepted",
                )
            if resume is not None or tmux:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "jev does not support --resume or --tmux",
                )
            headless = True
        try:
            validate_model_selection(
                harness_kind, model_provider, model,
                context=f"hyprial start --name {name}",
            )
        except ModelProviderError as error:
            raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        for option, explicit in (("--provider", model_provider), ("--model", model)):
            if explicit is not None and any(
                value == option or value.startswith(f"{option}=")
                for value in runtime_args
            ):
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"{option} was supplied both as a hyprial option and after '--'",
                )
        resolved_cwd = _resolved_agent_cwd(name, cwd)
        if resume is not None:
            if not (
                (harness_kind == "claude" and not headless)
                or (headless and harness_kind in {"claude", "pi", "codex"})
            ):
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "--resume is supported for interactive claude and for headless "
                    "claude, pi, and codex",
                )
            if not resume.strip():
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT, "--resume requires a session id"
                )
        if tmux and headless:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT, "--tmux cannot be combined with --headless"
            )
        if tmux and harness_kind not in {"claude", "pi"}:
            raise CliError(
                "UNSUPPORTED_CAPABILITY",
                "--tmux is currently only supported for interactive claude "
                "and pi sessions",
            )
        if not headless:
            from hyprial.harnesses.capabilities import (
                Capability,
                SupportLevel,
                declare,
            )

            attach = declare(harness_kind, headless=False).get(
                Capability.INTERACTIVE_ATTACH
            )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "channel"
            ):
                return _start_interactive_claude(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    resume=resume,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                    tmux=tmux,
                )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "extension"
                and harness_kind == "pi"
            ):
                return _start_interactive_pi(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                    tmux=tmux,
                )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "app_server"
                and harness_kind == "codex"
            ):
                return _start_interactive_codex(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                )
            raise CliError(
                "UNSUPPORTED_CAPABILITY",
                f"{harness_kind} does not support interactive_attach; "
                "use --headless or start an interactive claude session instead",
            )
        if harness_kind not in {"dsh", "jev", "user-proxy"}:
            # Pin the harness binary by absolute path at registration: the
            # daemon that later spawns it may run under a launchd/cron PATH
            # that lacks user bin dirs.  Older daemons ignore the unknown
            # key (tolerant desired-state reader), so this stays skew-safe.
            # abspath keeps vendor symlinks (e.g. ~/.local/bin/codex) intact
            # -- they are the upgrade-stable entry points.  Resolved BEFORE
            # agent creation so a refused start leaves no trace at all.
            pinned_binary = shutil.which(harness_kind)
            if pinned_binary is None:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"{harness_kind} binary not found on PATH; install it "
                    f"(or fix PATH) before starting a {harness_kind} connector",
                )
        # A5: the agent exists before the connector does, on this path too.
        _create_agent_for_start(
            name=name,
            harness=harness_kind,
            runtime="headless",
            cwd=resolved_cwd,
            provider=None if harness_kind in {"jev", "user-proxy"} else model_provider,
            model=None if harness_kind in {"jev", "user-proxy"} else model,
        )
        params: JsonObject = {
            # The IPC key stays "provider" so this CLI can talk to a daemon
            # running an older build (and vice versa).
            "provider": harness_kind,
            "name": name,
            "headless": True if harness_kind in {"jev", "user-proxy"} else headless,
            "args": list(runtime_args),
            "cwd": str(resolved_cwd),
        }
        if harness_kind == "user-proxy":
            assert proxy_route is not None
            params["command"] = [
                os.path.abspath(sys.executable),
                "-m",
                "hyprial.harnesses._user_proxy_worker",
                "--kind",
                "user-proxy",
                "--route",
                proxy_route,
            ]
        elif harness_kind == "jev":
            params["command"] = [
                os.path.abspath(sys.executable),
                "-m",
                "hyprial.harnesses._python_worker",
                "--kind",
                "jev",
            ]
        elif harness_kind != "dsh":
            params["command"] = [os.path.abspath(pinned_binary)]
        if nickname is not None:
            params["nickname"] = nickname
        if model_provider is not None and harness_kind not in {"jev", "user-proxy"}:
            params["modelProvider"] = model_provider
        if model is not None and harness_kind not in {"jev", "user-proxy"}:
            params["model"] = model
        if resume is not None:
            # Headless resume: the daemon refuses a session it cannot find
            # (RESUME_SESSION_NOT_FOUND, before anything starts) and one that
            # does not hold (STRICT_RESUME_FAILED, worker stopped).  Without
            # this key the daemon keeps today's default: a fresh session.
            params["sessionRef"] = resume.strip()
        # Harness readiness is bounded by the lifecycle manager's operation
        # deadline, and this wait must OUTLAST the daemon-side wait
        # (deadline + wait margin): a shorter budget abandoned a healthy
        # start with IPC_TIMEOUT while the daemon still owned and settled it
        # (2026-09-14 production).  Same derivation as `down`; the three
        # waits are composed from one deadline, never chosen independently.
        return _daemon_request(
            "lifecycle.start",
            params,
            timeout=(
                LIFECYCLE_OPERATION_DEADLINE_SECONDS
                + LIFECYCLE_WAIT_MARGIN_SECONDS
                + LIFECYCLE_IPC_MARGIN_SECONDS
                # A resume adds the daemon's readiness check (one margin) and,
                # when it fails, one undo operation (a full operation wait).
                + (
                    LIFECYCLE_WAIT_MARGIN_SECONDS
                    + LIFECYCLE_OPERATION_DEADLINE_SECONDS
                    + LIFECYCLE_WAIT_MARGIN_SECONDS
                    if "sessionRef" in params
                    else 0.0
                )
            ),
        )

    _execute(operation, json_output=json_output)


@app.command()
def down(
    targets: list[str] | None = typer.Argument(
        None, help="Connector ID or kind and target."
    ),
    all_connectors: bool = typer.Option(
        False,
        "--all",
        help=(
            "Stop and permanently deregister every daemon-managed harness "
            "connector (from 'hyprial start --headless') and every Lark adapter. "
            "Does NOT touch interactive Claude sessions ('hyprial start claude "
            "--name ...' without --headless) -- those keep running."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Stop one connector, or stop and deregister every daemon-managed
    harness connector and Lark adapter with --all.

    This is permanent removal from desired state, not a pause: a stopped
    connector or adapter will not come back on daemon restart until it is
    started again (adapters need 'hyprial adapter start <name>' per adapter).

    --all does not reach interactive Claude sessions started via
    'hyprial start claude --name ...' (no --headless); those are tracked
    separately and are never stopped by this command. Use
    'hyprial ps' to see connectors, adapters, and interactive sessions as
    three distinct categories.
    """

    def operation() -> Any:
        values = targets or []
        if all_connectors and values:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--all cannot be combined with a connector target",
            )
        if not all_connectors and len(values) not in {1, 2}:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "down requires a connector ID, kind and target, or --all",
            )
        params: JsonObject = {"all": all_connectors}
        if len(values) == 1:
            params["target"] = values[0]
        elif len(values) == 2:
            params.update({"provider": values[0], "target": values[1]})
        # Outlast the daemon-side lifecycle wait (deadline + wait margin) so a
        # stuck operation returns its coded failure here instead of an
        # IPC_TIMEOUT (card 104164aa (c)).
        return _daemon_request(
            "down",
            params,
            timeout=(
                LIFECYCLE_OPERATION_DEADLINE_SECONDS
                + LIFECYCLE_WAIT_MARGIN_SECONDS
                + LIFECYCLE_IPC_MARGIN_SECONDS
            ),
        )

    _execute(operation, json_output=json_output)


@app.command()
def transfer(
    name: str = typer.Argument(..., help="Managed headless worker name."),
    to: str = typer.Option(
        ..., "--to", help="SSH destination: [user@]<exact nodeId from this home's hyprial hosts>."
    ),
    harness: str | None = typer.Option(
        None, "--harness", help="Disambiguate when several harnesses share the name."
    ),
    cwd: str | None = typer.Option(
        None, "--cwd",
        help="Target directory; required across SSH login users (same-path default only for the same user)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and print the plan; change nothing (requires --yes)."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Confirm the reported remote command, daemon.socket and actor."
    ),
    strict_timeout: float = typer.Option(
        90.0,
        "--strict-timeout",
        help="Seconds the target waits for the resumed worker to come ready.",
    ),
    remote_hyprial: str = typer.Option(
        "hyprial",
        "--remote-hyprial",
        help=(
            "Remote command (default: hyprial). Non-default values are rehearsal-only "
            "and require a non-default source HYPRIAL_HOME override, e.g. "
            "'HYPRIAL_HOME=/tmp/hyprial-tgt /home/me/transfer-p0/.venv/bin/hyprial'."
        ),
    ),
    containerized: bool = typer.Option(
        False,
        "--containerized",
        help=(
            "Land the worker inside a docker container carrying the source "
            "owner's credentials (design docs/design-transfer-container.md)."
        ),
    ),
    image: str | None = typer.Option(
        None,
        "--image",
        help="Worker image reference (default: hyprial-worker:<this hyprial version>).",
    ),
    with_credentials: bool = typer.Option(
        True,
        "--with-credentials/--no-credentials",
        help=(
            "Ship the source owner's credential bundle (containerized mode "
            "only; --no-credentials exists for mutation testing)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Move a managed headless worker to another machine (P0 cold migration).

    stop -> ship (worktree + harness session file + identity) -> resume on
    the target with the transferred sessionRef.  The resume is STRICT: if
    the session does not come back on the target, the transfer fails and
    the source is rolled back rather than silently cold-starting.
    """

    def operation() -> Any:
        from hyprial.transfer.orchestrator import TransferError, run_transfer
        from hyprial.transfer.ssh import SshRunner

        if remote_hyprial != "hyprial" and (
            not os.environ.get("HYPRIAL_HOME", "").strip()
            or configured_hyprial_home()[0] == default_hyprial_home()[0]
        ):
            raise CliError(
                "TRANSFER_REMOTE_OVERRIDE",
                "non-default --remote-hyprial is rehearsal-only; select an isolated "
                "source HYPRIAL_HOME override (not the default ~/.hyprial). "
                "Production transfers use the default remote command hyprial",
            )

        def emit(message: str) -> None:
            # Facts must be visible BEFORE execution, including --json;
            # keep stdout as one JSON result by sending progress to stderr.
            print(message, file=sys.stderr if json_output else sys.stdout, flush=True)

        try:
            return run_transfer(
                name=name,
                harness=harness,
                host=to,
                target_cwd=cwd,
                dry_run=dry_run,
                strict_timeout=strict_timeout,
                local_request=_daemon_request,
                remote=SshRunner(to, remote_hyprial=remote_hyprial),
                emit=emit,
                yes=yes,
                containerized=containerized,
                container_image=image,
                with_credentials=with_credentials,
            )
        except TransferError as error:
            raise CliError(error.code, str(error), error.data) from error

    _execute(operation, json_output=json_output)


# -- transfer receive side (hidden; the orchestrator drives these over SSH) --


@app.command("transfer-precheck", hidden=True)
def transfer_precheck(
    harness: str = typer.Option(..., "--harness"),
    name: str = typer.Option(..., "--name"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Target-side admission check (driven by 'hyprial transfer' over SSH)."""

    _execute(
        lambda: _daemon_request(
            # The IPC key stays "provider" (schemaVersion=1 dual-read rule).
            "transfer.precheck",
            {"provider": harness, "name": name},
        ),
        json_output=json_output,
    )


@app.command("transfer-receive", hidden=True)
def transfer_receive(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Adopt a transferred worker; the payload JSON arrives on stdin."""

    def operation() -> Any:
        raw = sys.stdin.buffer.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"transfer-receive payload is not JSON: {error}",
            ) from error
        if not isinstance(payload, dict):
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "transfer-receive payload must be an object",
            )
        # Strict-resume verification blocks for up to strictTimeoutSeconds;
        # the IPC budget must outlive it (the orchestrator's SSH timeout
        # leaves the same margin).
        timeout = float(payload.get("strictTimeoutSeconds", 90.0)) + 60.0
        return _daemon_request("transfer.receive", payload, timeout=timeout)

    _execute(operation, json_output=json_output)


@app.command("transfer-cred-stage", hidden=True)
def transfer_cred_stage(
    name: str = typer.Option(..., "--name"),
    harness: str = typer.Option(..., "--harness"),
    finalize: bool = typer.Option(False, "--finalize"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Prepare (or permission-seal) the credential staging dir on the target.

    The orchestrator scp's the credential bundle and the image tar into the
    returned directory.  ``--finalize`` runs AFTER the uploads: staging
    dirs become 0700 and every staged file 0600 (scp does not preserve
    modes).  Nothing here reads file contents.
    """

    def operation() -> Any:
        from hyprial.transfer import container as xfer_container

        staging = xfer_container.staging_dir(_state_dir(), name)
        try:
            manifest = xfer_container.CREDENTIAL_FILES[harness]
        except KeyError as error:
            raise CliError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{harness} has no credential bundle manifest",
            ) from error
        staging.mkdir(parents=True, exist_ok=True)
        os.chmod(staging, 0o700)
        for arcname, _required_flag in manifest:
            parent = (staging / arcname).parent
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        staged_files: list[str] = []
        if finalize:
            for path in sorted(staging.rglob("*")):
                if path.is_dir():
                    os.chmod(path, 0o700)
                else:
                    os.chmod(path, 0o600)
                    staged_files.append(str(path.relative_to(staging)))
        return {
            "ok": True,
            "path": str(staging),
            **({"files": staged_files} if finalize else {}),
        }

    _execute(operation, json_output=json_output)


@app.command("transfer-container-home", hidden=True)
def transfer_container_home(
    name: str = typer.Option(..., "--name"),
    harness: str = typer.Option(..., "--harness"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create and return the per-worker container home on the target."""

    def operation() -> Any:
        from hyprial.transfer import container as xfer_container

        home = xfer_container.worker_home(_state_dir(), harness, name)
        session_host, _container_path = xfer_container.session_mount(harness, home)
        session_host.mkdir(parents=True, exist_ok=True)
        os.chmod(home.parent, 0o700)
        os.chmod(home, 0o700)
        return {"ok": True, "home": str(home)}

    _execute(operation, json_output=json_output)


@app.command("transfer-session-path", hidden=True)
def transfer_session_path(
    harness: str = typer.Option(..., "--harness"),
    cwd: str = typer.Option(..., "--cwd"),
    ref: str = typer.Option(..., "--ref"),
    filename: str = typer.Option(..., "--filename"),
    sessions_rel: str | None = typer.Option(None, "--sessions-rel"),
    home: str | None = typer.Option(
        None,
        "--home",
        help="Resolve under this home instead of the user's (container mode).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Resolve (and create the parent of) a session file's target path."""

    def operation() -> Any:
        from hyprial.transfer.session_files import (
            SessionFileError,
            claude_session_target,
            pi_session_target,
        )

        home_path = Path(home).expanduser() if home else Path.home()
        try:
            if harness == "claude":
                target = claude_session_target(home_path / ".claude", cwd, ref)
            elif harness == "pi":
                target = pi_session_target(home_path / ".pi" / "agent", cwd, filename)
            elif harness == "codex":
                if not sessions_rel:
                    raise CliError(
                        ipc_errors.INVALID_ARGUMENT,
                        "codex targets require --sessions-rel (the rollout's "
                        "path relative to the source sessions/ root)",
                    )
                target = home_path / ".codex" / "sessions" / sessions_rel
            else:
                raise CliError(
                    ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                    f"{harness} has no transferable session files",
                )
        except SessionFileError as error:
            raise CliError("TRANSFER_SESSION_FILE", str(error)) from error
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CliError(
                "TRANSFER_SESSION_FILE",
                f"cannot create {target.parent}: {error}",
            ) from error
        return {"ok": True, "path": str(target)}

    _execute(operation, json_output=json_output)


def _running_daemon_before_upgrade(version: str) -> JsonObject | None:
    try:
        # The pre-restart snapshot needs ONE number, the pid, and must not
        # hang on the actor projection: on 2026-09-05 03:17 this asked ``ps``
        # (77 actors x N desired-state loads, card 259) and the 15 s budget
        # expired, so autoupdate self-locked on exactly the build that fixed
        # the storm (card b9872e94).  ``_daemon_probe`` asks ping -- light,
        # answered mid-restore, no actor snapshot -- and falls back to ``ps``
        # only for a daemon older than the ping contract.  The normal IPC
        # budget stays: a loaded daemon still gets its 15 s to answer ping.
        status = _daemon_probe(timeout=15.0)
    except ipc_errors.DaemonUnavailableError:
        return None
    if not _probe_reports_running(status):
        return None
    daemon = status.get("daemon") if isinstance(status.get("daemon"), dict) else status
    pid = daemon.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise CliError("INVALID_RESPONSE", "running daemon did not report a valid pid")
    return {"pid": pid, "version": version}


def _prime_post_install_restart_imports() -> None:
    """Load restart-only modules before ``uv tool install`` replaces us.

    A uv tool upgrade atomically removes the distribution backing this still
    running CLI process.  Any first import after that boundary resolves
    against a path that no longer exists, even though the newly installed
    command is complete.  The restart path intentionally remains in this
    process so it can preserve the pre-install daemon snapshot and return one
    atomic upgrade result; load its lazy dependencies before crossing the
    destructive install boundary.
    """

    import importlib

    for name in (
        "hyprial.autoupdate",
        "hyprial.daemon.desired_state",
        # 3c116ad2: the post-restart confirmation reads ping's phase and
        # derives its poll budget here (pure arithmetic over the desired
        # state's rows) -- lazily in cli.py, so it must cross the install
        # boundary pre-loaded like every other restart-path import.
        "hyprial.daemon.readiness_budget",
        # The owner alert resolves channels through this lazy leaf import.
        # It fires after the install boundary, so prime the module explicitly.
        "hyprial.uri",
        "hyprial.mcp.channel",
    ):
        importlib.import_module(name)


def _perform_upgrade_and_report(*args: object, **kwargs: object) -> JsonObject:
    """Upgrade, then check what got installed, then tell the owner -- every time.

    ⭐ Sending only on failure is why this channel had never delivered anything:
    the one path that exercises it is the path where the machine is already in
    trouble. On 2026-08-31 it was needed twice and arrived zero times -- once
    the process died before reaching it, once the owner lookup was wrong and had
    never been run. Both invisible for the same reason: nothing used this code
    while things were fine.

    ⚠️ The report can never change the upgrade's outcome. It is written outside
    `_perform_upgrade` precisely so it cannot: whatever that raised is re-raised
    untouched, and nothing here raises anything of its own. A self-check that
    could fail an upgrade would turn "we could not verify" into "it did not
    work", and someone would go re-install something that had installed fine.
    """

    host = socket.gethostname()

    def report(*, action: str, detail: str, key: str) -> JsonObject:
        check = run_self_check()
        alert = notify_upgrade_outcome(
            hyprial_home=_hyprial_home(),
            state_dir=_state_dir(),
            host=host,
            action=action,
            upgrade_detail=detail,
            check=check,
            idempotency_key=key,
        )
        return {"selfCheck": check.to_json(), "ownerAlert": alert.to_json()}

    def _action_for(result: JsonObject) -> str:
        """What the run actually did -- read off the result, never assumed.

        ⚠️ This used to be a hardcoded `True` passed as `upgraded`. Reaching
        the success path and installing something are different facts, and
        `hyprial upgrade` runs on a timer where the second is usually false.
        """

        restart = result.get("restart")
        if isinstance(restart, dict) and restart.get("awaitingConfirmation") is True:
            # Installed on purpose without a restart; the owner confirms.
            return UPGRADE_AWAITING_RESTART
        if isinstance(restart, dict) and restart.get("confirmed") is False:
            # ⭐ Installed, daemon not yet confirmed ready. Checked before
            # `upgraded`, because this run *did* install -- so asking "did
            # anything install?" first would answer `✅ 升级完成` and state
            # something nobody verified. Trading a false alarm for a false
            # all-clear is not an improvement.
            return UPGRADE_UNCONFIRMED
        if result.get("upgraded"):
            return UPGRADE_INSTALLED
        if result.get("declinedDowngrade"):
            return UPGRADE_DECLINED_DOWNGRADE
        return UPGRADE_ALREADY_CURRENT

    try:
        result = _perform_upgrade(*args, **kwargs)  # type: ignore[arg-type]
    except (CliError, ipc_errors.TransientDaemonError) as error:
        try:
            observed = report(
                action=UPGRADE_FAILED,
                detail=f"{error.code}: {error}",
                key=f"upgrade-outcome:failed:{error.code}:{_utc_now()}",
            )
            if isinstance(error.data, dict):
                error.data.update(observed)
        except Exception:  # noqa: BLE001 -- the upgrade error is the message that matters
            pass
        raise
    try:
        action = _action_for(result)
        result.update(
            report(
                action=action,
                detail=str(result.get("resolvedTag") or "upgrade completed"),
                key=f"upgrade-outcome:ok:{result.get('resolvedCommit')}:{action}",
            )
        )
        if action == UPGRADE_UNCONFIRMED:
            # 3c116ad2 (P2): the notice above promised a follow-up; deliver
            # it from this same process -- poll ping's phase to reconciliation
            # or the fleet-derived budget, then one closing message.  Inside
            # the guard on purpose: reporting must never undo a good upgrade.
            _follow_up_restore_confirmation(result)
    except Exception:  # noqa: BLE001 -- reporting must not undo a good upgrade
        pass
    return result


def _perform_upgrade(
    force: bool,
    tag: str | None = None,
    *,
    restart: bool = True,
    before_restart: Callable[[JsonObject, str, str, str], JsonObject] | None = None,
    awaiting_confirmation: bool = False,
) -> JsonObject:
    """Install one exact tag and restart an existing daemon when it changes.

    ``awaiting_confirmation`` (the timer's mode): install, never restart, and
    record a pending restart that ``hyprial autoupdate restart`` applies.
    """

    from hyprial import updates
    from hyprial.home_migration import migrate_legacy_default_home

    # Self-upgrade is the other product installation boundary. Migrate before
    # update settings or state paths are resolved; explicit HYPRIAL_HOME is a
    # no-op in the migration helper and remains an isolation boundary.
    migrate_legacy_default_home()
    require_initialized_hyprial_home()

    installation = updates.read_installation()
    if updates.installation_is_local(installation):
        # Guard 2 (spec autoupdate-isolated-home-2026-09-15): this process
        # was installed from a local path (file://, bare path, git+file,
        # editable), so it runs from a working tree, and "upgrading" it
        # would overwrite the USER'S GLOBAL uv tool directory with that
        # tree's resolution.  Refuse before any remote probe.  Display-
        # through code per proto.md's registry border ruling: it lands in
        # the --json error surface and the autoupdate last-run record, and
        # no process branches on the string.
        raise CliError(
            "UPGRADE_LOCAL_SOURCE",
            "refusing to upgrade: this hyprial was installed from a local "
            f"source ({installation.url}); a uv tool install would overwrite "
            "the user's global tool directory",
            {
                "guard": "local-install-source",
                "installationUrl": installation.url,
            },
        )
    url = updates.installation_git_url(installation)
    try:
        # An explicit --tag is the operator's escape hatch and always wins;
        # otherwise an explicit settings.json updateTrack selects its movable
        # track tag, and no track keeps the legacy latest-version-tag probe.
        track = updates.read_update_track(_hyprial_home()) if tag is None else None
        resolution = updates.resolve_remote(url, tag=tag or track)
    except updates.UpdateProbeError as error:
        raise CliError("UPGRADE_CHECK_FAILED", str(error)) from error
    warning = updates.retired_track_warning(_hyprial_home())
    resolved: JsonObject = {
        "resolvedTag": resolution.tag,
        "resolvedCommit": resolution.commit,
    }
    if track is not None:
        resolved["track"] = track
    if warning is not None:
        resolved["warning"] = warning
    if not force and not updates.upgrade_available(
        installation, resolution, operator_chose_the_tag=tag is not None
    ):
        # ⚠️ Two different reasons land here and they must not share a sentence.
        # "already at the resolved tag's commit" is simply false when the track
        # moved backwards -- we are NOT at that commit, we declined to go to
        # it. And a guard that leaves no trace is unobservable: "the protection
        # fired" and "no rollback ever happened" would read identically, and we
        # want to know whether it has ever actually caught anything.
        declined_downgrade = tag is None and updates.resolution_is_a_downgrade(
            installation, resolution
        )
        return {
            "ok": True,
            "upgraded": False,
            "reason": (
                f"resolved tag names an older version than the installed "
                f"{installation.version}; refusing to move backwards"
                if declined_downgrade
                else "already at the resolved tag's commit"
            ),
            **({"declinedDowngrade": True} if declined_downgrade else {}),
            **resolved,
            "restartRequired": False,
            "restart": {
                "attempted": False,
                "restarted": False,
                "reason": "upgrade was a no-op; restart not needed",
            },
        }
    # Track installs pin the resolved commit, never the movable tag name: the
    # tag can advance between this resolution and uv's fetch, and uv records
    # the requirement's ref as PEP 610 requested_revision — pinning the tag
    # name on a track would make every later upgrade see a changed ref and
    # restart the daemon on every timer tick.  The legacy and explicit-tag
    # paths keep their exact @tag requirement.
    requested = resolution.commit if track is not None else resolution.tag
    ref_changed = installation.requested_revision != requested
    before = (
        _running_daemon_before_upgrade(installation.version or "unknown")
        if ref_changed
        else None
    )
    if before is not None and restart:
        _prime_post_install_restart_imports()
    requirement = f"git+{url}@{requested}"
    resolved_suffix = f"resolvedTag={resolution.tag} resolvedCommit={resolution.commit}"
    tool_guard = updates.uv_tool_dir_guard()
    if not tool_guard["allowed"]:
        # Guard 3 (spec autoupdate-isolated-home-2026-09-15), the backstop:
        # ``uv tool install`` may run only when this process itself lives in
        # the tool directory uv is about to write.  Placement is the single
        # install call site, so the manual ``hyprial upgrade``, the
        # autoupdate child, and the legacy launchd/systemd timer (which
        # shells out to the same child) all share it.  Fail-closed on an
        # unresolvable tool directory: we cannot prove where uv would
        # write, so we cannot prove the write is ours.
        raise CliError(
            "UPGRADE_TOOL_DIR_MISMATCH",
            "refusing to run uv tool install: uv would write "
            f"{tool_guard['toolDir']} but this process runs from "
            f"{tool_guard['sysPrefix']}; only the installation this process "
            "is part of may be upgraded in place",
            {**resolved, **tool_guard},
        )
    try:
        completed = subprocess.run(
            # --compile-bytecode: pay the ~11.5k-file compile here, inside
            # UV_INSTALL_TIMEOUT, not in the restarted daemon's first import.
            # Without it the new daemon spent ~15 s compiling before it could
            # serve, the fixed 15 s readiness wait below expired, and a
            # healthy upgrade was reported UPGRADE_RESTART_FAILED with a false
            # owner alert (hyprial-hq, 2026-09-24 07:08Z: 11,447 .pyc written
            # 07:08:00-07:08:19, ipc-server up at 07:08:18.859).
            ["uv", "tool", "install", "--force", "--compile-bytecode", requirement],
            text=True,
            capture_output=True,
            check=False,
            # The timer (and any headless caller) must never hang on a git
            # credential prompt inside uv's own fetch.
            env=updates.git_env(),
            timeout=updates.UV_INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise CliError(
            "UPGRADE_FAILED",
            f"uv tool install timed out after {updates.UV_INSTALL_TIMEOUT:g}s; "
            f"{resolved_suffix}",
            resolved,
        ) from error
    except OSError as error:
        # e.g. uv was uninstalled after the timer was installed.
        raise CliError(
            "UPGRADE_FAILED",
            f"cannot run uv: {error}; {resolved_suffix}",
            resolved,
        ) from error
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "uv tool install failed"
        )
        raise CliError("UPGRADE_FAILED", f"{detail}; {resolved_suffix}", resolved)
    installed = updates.read_installation()
    installed_version = installed.version or resolution.version or resolution.tag
    result: JsonObject = {
        "ok": True,
        "upgraded": True,
        **resolved,
        "output": completed.stdout.strip(),
    }
    if not ref_changed:
        result.update(
            {
                "restartRequired": False,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    # The legacy string is byte-preserved; only track mode
                    # (which pins a commit, not a tag) uses the new wording.
                    "reason": (
                        "resolved ref unchanged; restart not needed"
                        if track is not None
                        else "resolved tag unchanged; restart not needed"
                    ),
                },
            }
        )
        return result
    if before is None:
        result.update(
            {
                "restartRequired": False,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    "reason": "daemon was not running; restart skipped",
                },
            }
        )
        return result
    if awaiting_confirmation:
        # Allen 2026-09-23: the timer no longer restarts on its own.  The old
        # gate (restart only after a 3s squire receipt) failed on a slow
        # receipt while the notice itself arrived, and a blocked restart was
        # then forgotten: the next run saw "already current" and never
        # restarted.  Now the install is recorded as a pending restart that
        # survives until someone runs `hyprial autoupdate restart`.
        pending = {
            "tag": resolution.tag,
            "commit": resolution.commit,
            "version": installed_version,
            "before": before,
            "resolved": resolved,
            "installedAt": _utc_now(),
        }
        _write_pending_restart(pending)
        result.update(
            {
                "restartRequired": True,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    "awaitingConfirmation": True,
                    "reason": (
                        "installed; restart waits for confirmation: "
                        "hyprial autoupdate restart"
                    ),
                    "before": before,
                },
                "pendingRestart": pending,
            }
        )
        return result
    if not restart:
        result.update(
            {
                "restartRequired": True,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    "reason": "restart disabled by --no-restart",
                    "before": before,
                },
            }
        )
        return result

    notification: JsonObject | None = None
    if before_restart is not None:
        try:
            notification = before_restart(
                before,
                installed_version,
                resolution.tag,
                resolution.commit,
            )
            if (
                notification.get("delivered") is not True
                or notification.get("deliveryConfirmed") is not True
            ):
                raise CliError(
                    "AUTOUPDATE_NOTIFICATION_UNDELIVERED",
                    "restart notification did not receive delivery confirmation",
                )
        except Exception as error:  # noqa: BLE001 - restart must fail closed
            failure: JsonObject = {
                "upgradeCompleted": True,
                "upgraded": True,
                **resolved,
                "restartRequired": True,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    "reason": (
                        "restart blocked because the pre-restart notification "
                        "was not confirmed"
                    ),
                    "before": before,
                },
            }
            if notification is not None:
                failure["notification"] = notification
            raise CliError(
                "UPGRADE_NOTIFICATION_FAILED",
                f"upgrade completed at {resolution.tag} ({installed_version}), "
                f"but restart notification was not confirmed: {error}",
                failure,
            ) from error
        result["notification"] = notification

    return _restart_daemon_onto_install(
        result,
        before=before,
        installed_version=installed_version,
        resolution=resolution,
        resolved=resolved,
    )


def _restart_daemon_onto_install(
    result: JsonObject,
    *,
    before: JsonObject,
    installed_version: str,
    resolution: Any,
    resolved: JsonObject,
) -> JsonObject:
    """Restart the running daemon onto the version already installed.

    Shared by the upgrade path and ``hyprial autoupdate restart`` (the
    person-confirmed restart of an upgrade that was installed without one).
    ``resolution`` needs only ``tag`` and ``commit``.
    """

    restart_result: JsonObject = {
        "attempted": True,
        "restarted": False,
        "reason": "daemon restart did not complete",
        "before": before,
    }
    restart_started_at: datetime | None = None
    try:
        try:
            _stop_daemon_gracefully()
        except (CliError, ipc_errors.TransientDaemonError) as stop_error:
            # ⚠️ A stop that times out says we stopped watching. It does not
            # say the daemon will not exit -- it has its own guarantee, and on
            # 2026-08-31 the outgoing process did exit, seconds after this
            # branch had already abandoned the upgrade. Nothing launched a
            # replacement, and production was down 56 minutes.
            #
            # 🔑 The launcher below usually adjudicates: the replacement takes
            # `daemon.lock`, and fails loudly if the old daemon still holds it.
            # Giving up here traded a *possible* failure for a *certain*
            # absence.
            #
            # ⛔ But that adjudication is on the **lock**, and there is one
            # shape where the lock is free while the old process is not gone:
            # 2026-08-30, teardown finished, the lock came back, `daemon.json`
            # moved -- and the process lived on for nine hours holding 106
            # descendants that blocked the replacement's bootstrap. In that
            # shape the replacement acquires the lock happily and nothing about
            # the launch looks wrong. So the launcher cannot be relied on here;
            # that is what DAEMON_STOP_SURVIVOR exists to say, and why it does
            # not share this path's silence.
            restart_result["stopWarning"] = str(stop_error)
            if stop_error.code == "DAEMON_STOP_SURVIVOR":
                # Still launch -- refusing would trade a possible failure for a
                # certain absence again, and the claim that the launch is futile
                # is not one anyone has verified. What changes is that a person
                # hears about it in minutes rather than in nine hours, which was
                # the actual cost that day.
                restart_result["survivingOldProcess"] = True
                survivor = _describe_survivor(old_pid_from(stop_error))
                marker_path: Path | None
                try:
                    # ⛔ No `start_failure` argument, on purpose: a survivor is
                    # not a start failure, so the question does not apply. The
                    # key is left out; passing `None` would assert "we looked
                    # for this launch's daemon.start.failed and found none", a
                    # search this branch never runs. The omission is the honest
                    # record, and it keeps `null` meaning only "looked and there
                    # was none" on the restart-failure path below.
                    marker_path = write_failure_marker(
                        state_dir=_state_dir(),
                        host=socket.gethostname(),
                        summary="旧 daemon 拆解完成但进程没有退出",
                        detail=f"{stop_error}\n{survivor}",
                    )
                except OSError:
                    marker_path = None
                survivor_alert = notify_upgrade_failure(
                    hyprial_home=_hyprial_home(),
                    state_dir=_state_dir(),
                    host=socket.gethostname(),
                    # ⭐ Deliberately not the same sentence as a stop timeout.
                    # The two ask for different actions: a timeout means "wait,
                    # it is probably fine"; this means "go find the survivor, it
                    # will not leave on its own".
                    summary="⚠️ 旧 daemon 拆解完成但【进程没有退出】—— 新 daemon 已启动",
                    detail=f"{stop_error}\n{survivor}",
                    idempotency_key=(
                        f"daemon-stop-survivor:{resolution.commit}:{stop_error.data}"
                    ),
                )
                restart_result["survivorAlert"] = survivor_alert.to_json()
                if marker_path is not None:
                    record_alert_outcome(marker_path, survivor_alert)
        # The restart wait covers only the serving boundary, so it is the same
        # small budget as init's -- not a fleet-scaled one.  The 2026-08-31
        # measurement that justified 90s here (a six-adapter machine needing
        # 161s) was adapter restore time, and restore is no longer inside this
        # wait: it runs on the daemon's own thread and reports through ping's
        # phase.  A timeout therefore means the new daemon never came up, which
        # `_launch_daemon_process` raises and the restart-failed path below
        # reports -- the "still starting" third verdict this replaces existed
        # only because restore used to sit inside the readiness budget.
        # Parent-side wall clock is captured immediately before the launch
        # primitive. Every event from the child generation must be at or after
        # this lower bound; prior daemon generations are therefore ineligible
        # for the failure diagnosis even when they are the last matching line.
        now = datetime.now(UTC)
        # DaemonLogger serializes milliseconds. Floor the parent boundary to
        # that same precision so an event emitted later in this millisecond is
        # not made to look earlier merely by JSON timestamp truncation.
        restart_started_at = now.replace(microsecond=now.microsecond // 1000 * 1000)
        launched = _launch_daemon_process(ready_timeout=15.0)
        # F2 (2026-09-04): this used to dig for the nested ps shape
        # ``launched["daemon"]`` against the flat launch answer, so every
        # upgrade ended in UPGRADE_RESTART_FAILED with a false alert, and a
        # hand-written stub returning the nested shape kept the tests green.
        # The launch answer is the typed ``DaemonLaunchResult`` now; anything
        # that is not the type is a contract break and takes the loud path
        # below, never a silent misread.
        if not isinstance(launched, DaemonLaunchResult):
            raise CliError(
                "INVALID_RESPONSE",
                "daemon launch returned an unexpected result shape",
            )
        if launched.running is not True:
            raise CliError(
                "INVALID_RESPONSE", "restarted daemon did not report running"
            )
        after_pid = launched.pid
        if not isinstance(after_pid, int) or after_pid <= 0:
            raise CliError(
                "INVALID_RESPONSE", "restarted daemon did not report a valid pid"
            )
        if after_pid == before["pid"]:
            raise CliError(
                "INVALID_RESPONSE", "daemon restart did not yield a fresh pid"
            )
    except (CliError, ipc_errors.TransientDaemonError, OSError) as error:
        restart_result["reason"] = f"daemon restart failed: {error}"
        failure: JsonObject = {
            "upgradeCompleted": True,
            "upgraded": True,
            **resolved,
            "restart": restart_result,
        }
        if result.get("notification") is not None:
            failure["notification"] = result["notification"]
        # ⭐ Write the fact down, *then* try to tell someone -- in that order,
        # never the reverse. The send can fail with nobody left to notice: there
        # is no daemon at this point, and the network is one of the things that
        # may be broken. On 2026-08-31 this exact failure sat in the autoupdate
        # status for 56 minutes; what was missing was not the record, it was the
        # notice. Both halves, and this order.
        summary = (
            f"升级到 {resolution.tag} ({installed_version}) 已完成,但 daemon 重启失败"
        )
        start_failure = (
            latest_start_failure(
                _state_dir() / "logs" / "daemon.jsonl",
                started_at=restart_started_at,
            )
            if restart_started_at is not None
            else None
        )
        host = socket.gethostname()
        # ⭐ The marker is what an unattended failure leaves behind, and it is
        # read by someone who does not already know to open daemon.jsonl. So it
        # gets the same cause line the alert gets, rendered by the same
        # function (⛔ never a second copy of the format).
        #
        # ⛔ The process state stays out on purpose: it is observed *below*, and
        # the write must stay ahead of that observation. Adding it here would
        # mean moving this write after `_observe_restart_process`, which is the
        # ordering this block exists to preserve.
        marker_detail = (
            f"daemon restart failed: {error}\n{start_failure_line(start_failure)}"
        )
        marker: Path | None
        try:
            marker = write_failure_marker(
                state_dir=_state_dir(),
                host=host,
                summary=summary,
                detail=marker_detail,
                start_failure=start_failure,
            )
        except OSError as marker_error:
            # Said out loud rather than swallowed: "the marker is missing" and
            # "there was nothing to mark" must not look the same afterwards.
            marker = None
            failure["failureMarker"] = {
                "written": False,
                "reason": str(marker_error),
            }
        else:
            failure["failureMarker"] = {"written": True, "path": str(marker)}
        # The durable failure fact is now on disk. Re-observe this exact child
        # immediately before sending so the owner sees its current state, not
        # the state at the end of the 15-second readiness wait.
        process_observation = _observe_restart_process(error)
        detail = restart_failure_detail(
            error=str(error),
            start_failure=start_failure,
            process=process_observation,
        )
        alert = notify_upgrade_failure(
            hyprial_home=_hyprial_home(),
            state_dir=_state_dir(),
            host=host,
            summary=summary,
            detail=detail,
            idempotency_key=(
                f"upgrade-restart-failed:{resolution.commit}:{installed_version}"
            ),
        )
        failure["alert"] = alert.to_json()
        if marker is not None:
            record_alert_outcome(marker, alert)
        raise CliError(
            "UPGRADE_RESTART_FAILED",
            f"upgrade completed at {resolution.tag} ({installed_version}), "
            f"but daemon restart failed: {error}",
            failure,
        ) from error

    restart_result.update(
        {
            "restarted": True,
            "reason": "resolved tag changed; running daemon restarted",
            "after": {"pid": after_pid, "version": installed_version},
        }
    )
    # 3c116ad2 (P1): the launch answer proves only the serving boundary.
    # Restore runs on the daemon's own thread and reports through ping's
    # phase, so confirmation reads that phase -- once, without waiting here:
    # ``reconciled`` is the daemon's own "restore settled" verdict, and
    # anything else (or no answer) leaves the restart honestly unconfirmed
    # for the reporter to act on.  The serving wait above stays as small as
    # it is -- restore time does not go back into the launch budget.
    phase = _read_restore_phase()
    if phase == "reconciled":
        restart_result["confirmed"] = True
    else:
        pending = _pending_connector_count()
        restart_result["confirmed"] = False
        restart_result["reason"] = (
            f"restore in progress, {pending} connectors pending"
            if pending is not None
            else "restore in progress"
        )
    result.update({"restartRequired": False, "restart": restart_result})
    return result


def _read_restore_phase() -> str | None:
    """One light ping; the answer's ``phase`` is the only readiness truth.

    The three phases are the daemon's contract (``restoring`` ->
    ``serving`` -> ``reconciled``); this function reads and returns one,
    interpreting nothing.  No phase in the answer is returned as ``None`` --
    only an explicit ``reconciled`` may ever count as confirmed.
    """

    answer = _daemon_request("ping", timeout=2.0)
    if isinstance(answer, dict):
        phase = answer.get("phase")
        if isinstance(phase, str):
            return phase
    return None


def _poll_restore_phase() -> str | None:
    """`_read_restore_phase` for the poll loop: no answer is not reconciled.

    A daemon that cannot answer ping while its budget elapses is simply not
    reconciled -- that is the whole of the interpretation, and the poll
    keeps its promise of deciding on ping's phase rather than on an error.
    Both error families count as "no answer": CliError (malformed reply)
    and the transient transport classes (#332 F4② -- a restoring or
    reconnecting daemon is mid-flight, not gone).
    """

    try:
        return _read_restore_phase()
    except (CliError, ipc_errors.TransientDaemonError):
        return None


def _pending_connector_count() -> int | None:
    """Connectors not back yet, counted off ps's existing ``connectors`` list.

    ``None`` when ps cannot answer (mid-restore it is gated behind the
    restore wall) or carries no connector list -- then the message omits
    the count rather than guess at one.  Best-effort by design:
    ``restore_wait=0.0`` never sits out the restoring refusal.
    """

    try:
        answer = _daemon_request("ps", timeout=2.0, restore_wait=0.0)
    except (CliError, ipc_errors.TransientDaemonError):
        # Mid-restore ps is refused as a transient class (#332 F4②); either
        # way there is no count to read, and the message omits N.
        return None
    if not isinstance(answer, dict):
        return None
    connectors = answer.get("connectors")
    if not isinstance(connectors, list):
        return None
    return sum(
        1
        for row in connectors
        if not (isinstance(row, dict) and row.get("running") is True)
    )


def _desired_connector_rows() -> int:
    """Connector rows in this machine's desired state -- the restore's fleet.

    The row count is what the poll budget scales with, exactly as the
    daemon-side settlement bound scales with its target count.  A fresh
    home has no rows and derives an empty budget; that is the honest bound
    for a machine with nothing to restore.
    """

    from hyprial.daemon.desired_state import DesiredStateStore

    document = _state_dir() / "desired-state.json"
    return len(DesiredStateStore(document).load().harnesses)


def _expected_interruption_seconds() -> int:
    """The notice budget, derived from this machine's fleet -- not a literal.

    Same arithmetic as the follow-up poll's backstop (F1's admission-round
    shape with margin), rounded up to whole seconds.  The daemon's notice
    text no longer speaks a duration (3c116ad2 P3), but the field stays a
    positive-int contract; a machine with an empty desired state derives
    zero seconds and is floored to 1 -- the smallest value the contract
    accepts, not a new promise.
    """

    from hyprial.daemon.readiness_budget import restore_followup_budget_seconds

    seconds = restore_followup_budget_seconds(_desired_connector_rows())
    return max(1, math.ceil(seconds))


def _follow_up_restore_confirmation(
    result: JsonObject,
    *,
    poll_interval_seconds: float = _RESTORE_FOLLOWUP_POLL_INTERVAL_SECONDS,
    admission_width: int | None = None,
    start_timeout_seconds: float | None = None,
) -> None:
    """Close the unconfirmed state: poll ping's phase, then follow up once.

    The first alert said "unconfirmed"; card 3c116ad2 P2 requires the same
    CLI process to settle it.  Poll until ping reports ``reconciled`` or the
    fleet-derived budget runs out -- never longer -- then send exactly one
    follow-up over the owner channel: 「已恢复」 flips ``confirmed`` to true,
    「仍在启动:N」 leaves it false.  The budget is the F1 shape
    (``ceil(rows/width)`` rounds of one start timeout, with margin) over
    this machine's own desired-state rows, never a wall-clock guess; the
    width/timeout parameters are the production defaults and exist so
    tests can shrink them instead of sleeping minutes.
    """

    from hyprial.daemon.readiness_budget import (
        START_ADMISSION_WIDTH_DEFAULT,
        START_TIMEOUT_SECONDS_DEFAULT,
        restore_followup_budget_seconds,
    )

    restart = result.get("restart")
    if not isinstance(restart, dict):
        return
    budget = restore_followup_budget_seconds(
        _desired_connector_rows(),
        admission_width=(
            START_ADMISSION_WIDTH_DEFAULT
            if admission_width is None
            else admission_width
        ),
        start_timeout_seconds=(
            START_TIMEOUT_SECONDS_DEFAULT
            if start_timeout_seconds is None
            else start_timeout_seconds
        ),
    )
    deadline = time.monotonic() + budget
    phase = _poll_restore_phase()
    while phase != "reconciled" and time.monotonic() < deadline:
        time.sleep(poll_interval_seconds)
        phase = _poll_restore_phase()
    host = socket.gethostname()
    upgrade_detail = str(result.get("resolvedTag") or "upgrade completed")
    commit = str(result.get("resolvedCommit") or "unknown")
    follow_up: JsonObject = {"phase": phase}
    if phase == "reconciled":
        restart["confirmed"] = True
        restart["reason"] = "restore settled after poll (phase reconciled)"
        follow_up["outcome"] = "recovered"
        alert = notify_restore_followup(
            hyprial_home=_hyprial_home(),
            state_dir=_state_dir(),
            host=host,
            recovered=True,
            pending_connectors=None,
            upgrade_detail=upgrade_detail,
            idempotency_key=f"upgrade-restore-followup:recovered:{commit}",
        )
    else:
        pending = _pending_connector_count()
        follow_up["outcome"] = "still-starting"
        if pending is not None:
            follow_up["pendingConnectors"] = pending
        alert = notify_restore_followup(
            hyprial_home=_hyprial_home(),
            state_dir=_state_dir(),
            host=host,
            recovered=False,
            pending_connectors=pending,
            upgrade_detail=upgrade_detail,
            idempotency_key=f"upgrade-restore-followup:still-starting:{commit}",
        )
    follow_up["alert"] = alert.to_json()
    restart["followUp"] = follow_up


@app.command()
def upgrade(
    tag: str | None = typer.Option(
        None,
        "--tag",
        help="Install this exact remote tag instead of the latest version tag.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Reinstall the resolved tag even when its commit is current.",
    ),
    no_restart: bool = typer.Option(
        False,
        "--no-restart",
        help="Install the tag but leave an existing daemon on its current code.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Upgrade the uv-managed hyprial tool to an exact Forgejo tag."""

    _execute(
        lambda: _perform_upgrade_and_report(force, tag, restart=not no_restart),
        json_output=json_output,
        allow_missing_home=True,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _timer_config(*, require_executables: bool) -> Any:
    """Build the timer unit config, resolving the executable paths at install
    time so the rendered units stay valid even if the PATH changes later."""

    from hyprial.autoupdate import TimerConfig

    executable = shutil.which("hyprial")
    uv = shutil.which("uv")
    if require_executables:
        if executable is None:
            raise CliError(
                "INVALID_CONFIGURATION",
                "hyprial executable not on PATH; install with 'uv tool install' first",
            )
        if uv is None:
            raise CliError(
                "INVALID_CONFIGURATION",
                "uv not on PATH; 'hyprial upgrade' cannot reinstall without it",
            )
    path_entries: list[str] = []
    for candidate in (uv, executable):
        if candidate is None:
            continue
        parent = str(Path(candidate).resolve().parent)
        if parent not in path_entries:
            path_entries.append(parent)
    # User-level installs (uv tools, pipx, the codex standalone shim) live in
    # ~/.local/bin on both macOS and Linux; units rendered without it cannot
    # find those binaries (2026-08-23 post-upgrade outage).
    user_bin = str(Path.home() / ".local" / "bin")
    if user_bin not in path_entries:
        path_entries.append(user_bin)
    path_entries.append(
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    return TimerConfig(
        executable=(
            Path(executable).resolve()
            if executable is not None
            else Path(sys.argv[0]).resolve()
        ),
        home=Path.home(),
        hyprial_home=_hyprial_home(),
        state_dir=_state_dir(),
        path_env=":".join(path_entries),
    )


def _autoupdate_result(status: Any) -> JsonObject:
    from hyprial.autoupdate import SCHEDULE

    result: JsonObject = {
        "ok": True,
        "platform": status.platform,
        "unit": status.unit,
        "installed": status.installed,
        "loaded": status.loaded,
        "schedule": [{"hour": hour, "minute": minute} for hour, minute in SCHEDULE],
        "lastRun": status.last_run,
    }
    if status.boot_persistent is not None:
        result["bootPersistent"] = status.boot_persistent
    if status.enabled is not None:
        result["enabled"] = status.enabled
    if status.changed is not None:
        result["changed"] = status.changed
    return result


@autoupdate_app.command("install")
def autoupdate_install(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Install the legacy cutover timer (the daemon now owns scheduling)."""

    def operation() -> JsonObject:
        from hyprial.autoupdate import AutoUpdateManager, detect_platform

        platform = detect_platform()
        if platform is None:
            raise CliError(
                "PLATFORM_UNSUPPORTED",
                f"no update timer for platform {sys.platform!r}; "
                "supported: macOS (launchd), Linux (systemd)",
            )
        manager = AutoUpdateManager(
            _timer_config(require_executables=True), platform=platform
        )
        try:
            return _autoupdate_result(manager.install())
        except (RuntimeError, OSError) as error:
            raise CliError("AUTOUPDATE_INSTALL_FAILED", str(error)) from error

    _execute(operation, json_output=json_output)


@autoupdate_app.command("uninstall")
def autoupdate_uninstall(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove the legacy timer after daemon-owned upgrade proof."""

    def operation() -> JsonObject:
        from hyprial.autoupdate import AutoUpdateManager, detect_platform

        platform = detect_platform()
        if platform is None:
            raise CliError(
                "PLATFORM_UNSUPPORTED",
                f"no update timer for platform {sys.platform!r}",
            )
        manager = AutoUpdateManager(
            _timer_config(require_executables=False), platform=platform
        )
        try:
            return _autoupdate_result(manager.uninstall())
        except (RuntimeError, OSError) as error:
            raise CliError("AUTOUPDATE_UNINSTALL_FAILED", str(error)) from error

    _execute(operation, json_output=json_output)


@autoupdate_app.command("status")
def autoupdate_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the update timer state and the most recent run."""

    def operation() -> JsonObject:
        from hyprial import updates
        from hyprial.autoupdate import (
            SCHEDULE,
            AutoUpdateManager,
            detect_platform,
            read_last_run,
        )

        scheduler: JsonObject
        try:
            scheduler_result = _daemon_request(
                "autoupdate.status", timeout=1.0, restore_wait=0.0
            )
        # PR #332 F4②: the best-effort transport-transient set, spelled as
        # the registered classes.  DAEMON_RESTORING stays out (its budget is
        # the caller's restore_wait=0.0 fail-fast choice).
        except (
            ipc_errors.DaemonUnavailableError,
            ipc_errors.DaemonDisconnectedError,
            ipc_errors.IpcTimeoutError,
        ):
            scheduler = {
                "running": False,
                "active": False,
                "pending": False,
                "nextRunAt": None,
                "lastRun": read_last_run(_state_dir()),
                # Installed but not yet running; `hyprial autoupdate restart` applies it.
                "pendingRestart": _read_pending_restart(),
            }
        else:
            if not isinstance(scheduler_result, dict):
                raise CliError(
                    "INVALID_RESPONSE", "daemon autoupdate status must be an object"
                )
            scheduler = scheduler_result

        # §6a: the per-node migration pre-check reads off the same command an
        # operator already runs; it is also the P3 gate's evidence surface.
        # ``unreadable`` is reported separately so a corrupt receipt is never
        # mistaken for "nothing to migrate" (RELP2A2 追加1).
        scan = _safe_scan_app_migrations()
        platform = detect_platform()
        auto_upgrade_enabled = updates.auto_upgrade_enabled(_hyprial_home())
        if platform is None:
            return {
                "ok": True,
                "trigger": "daemon",
                "autoUpgradeEnabled": auto_upgrade_enabled,
                "platform": sys.platform,
                "unit": None,
                "installed": False,
                "loaded": False,
                "schedule": [
                    {"hour": hour, "minute": minute} for hour, minute in SCHEDULE
                ],
                "lastRun": read_last_run(_state_dir()),
                # Installed but not yet running; `hyprial autoupdate restart` applies it.
                "pendingRestart": _read_pending_restart(),
                "scheduler": scheduler,
                "pendingAppMigrations": list(scan.pending),
                "pendingAppMigrationsUnreadable": list(scan.unreadable),
                "pendingAppMigrationsBrokenLinks": list(scan.broken_links),
                "pendingAppMigrationsScanError": scan.scan_error,
            }
        manager = AutoUpdateManager(
            _timer_config(require_executables=False), platform=platform
        )
        result = _autoupdate_result(manager.status())
        result["trigger"] = "daemon"
        result["autoUpgradeEnabled"] = auto_upgrade_enabled
        result["scheduler"] = scheduler
        result["pendingAppMigrations"] = list(scan.pending)
        result["pendingAppMigrationsUnreadable"] = list(scan.unreadable)
        result["pendingAppMigrationsBrokenLinks"] = list(scan.broken_links)
        result["pendingAppMigrationsScanError"] = scan.scan_error
        result["legacyUnit"] = {
            "unit": result["unit"],
            "installed": result["installed"],
            "loaded": result["loaded"],
        }
        return result

    _execute(operation, json_output=json_output)


def _safe_scan_app_migrations() -> MigrationScan:
    """Scan installed receipts for v1→release migration (§6a).  A pre-check must
    never fail the caller, so an unexpected error becomes a scan whose
    ``scan_error`` says the scan could not run -- ⛔ never a silent empty
    "nothing pending" (owner audit B2).

    A *read failure of one receipt* is carried in ``MigrationScan.unreadable``,
    and a failure to enumerate ``apps/`` in ``MigrationScan.scan_error`` (both by
    ``scan_app_migrations`` itself); this wrapper only guards the truly
    unexpected (e.g. the home cannot be resolved)."""

    try:
        return scan_app_migrations(_hyprial_home())
    except Exception as error:  # noqa: BLE001 - a pre-check must not fail the run or status
        return MigrationScan((), (), scan_error=f"{type(error).__name__}: {error}")


def _alert_pending_app_migrations(pending: list[str]) -> None:
    """Write-then-send the migration notice after a self-upgrade (§6b).

    Marker on disk first (its own file, never the restart-failed one), then the
    owner DM through the module's never-raises path, then fold the send outcome
    back into the marker.  Nothing here may fail the upgrade that just ran.

    ⚠️ A marker-write failure does **not** cancel the DM (fable R3, aligning with
    the restart-failed precedent at ``cli.py`` ~6628 and the alert module's "a
    write failure is worth surfacing"): the marker is set to ``None``, a line is
    printed to stderr, and the owner is still notified; only the write-back of the
    send outcome is skipped when there is no marker to fold it into."""

    host = socket.gethostname()
    marker: Path | None
    try:
        marker = write_app_migration_marker(
            state_dir=_state_dir(), host=host, apps=pending
        )
    except Exception as error:  # noqa: BLE001 - surface it, but still notify the owner
        marker = None
        print(
            f"hyprial: could not write the app-migration marker: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
    try:
        outcome = notify_app_migration_required(
            hyprial_home=_hyprial_home(),
            state_dir=_state_dir(),
            host=host,
            apps=pending,
            # ⚠️ Stable idempotency key -- no timestamp (owner audit #6): the
            # daemon-less send path dedups on this key, so a per-run ``_utc_now()``
            # would DM the owner afresh on every timer fire until the app migrates.
            idempotency_key=f"app-migration:{','.join(pending)}",
        )
        if marker is not None:
            record_alert_outcome(marker, outcome)
    except Exception:  # noqa: BLE001 - see the alert module docstring
        pass


@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ..., help="Config key (supported: autoUpgrade, forwarding.mode)."
    ),
    value: str = typer.Argument(
        ..., help="New value (autoUpgrade: true|false; forwarding.mode: off|auto|on)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Set an operator switch in settings.json.

    ``autoUpgrade`` (spec autoupdate-isolated-home r1) gates every automatic
    self-upgrade: the daemon scheduler and the ``autoupdate run`` timer entry
    point skip until this is explicitly ``true``.  The manual
    ``hyprial upgrade`` is an operator's explicit act and stays ungated.

    ``forwarding.mode`` is the durable forwarding switch: ``auto`` (the
    default) forwards through the sidecar on a sidecar-joined home, ``off``
    disables it -- including any explicit or generated forwarding variables
    -- and ``on`` refuses to start the daemon without it.
    ``HYPRIAL_FORWARDING`` in the daemon's environment still wins.  It takes
    effect on the next daemon start.
    """

    def operation() -> JsonObject:
        from hyprial import updates
        from hyprial.forwarding_config import (
            FORWARDING_SETTINGS_KEY,
            write_forwarding_mode,
        )

        forwarding_key = f"{FORWARDING_SETTINGS_KEY}.mode"
        if key == forwarding_key:
            try:
                path = write_forwarding_mode(value, _hyprial_home())
            except (ValueError, ForwardingConfigurationError) as error:
                raise CliError("INVALID_CONFIGURATION", str(error)) from error
            return {
                "ok": True,
                "key": forwarding_key,
                "value": value.strip().lower(),
                "path": str(path),
                "appliesOn": "next daemon start",
            }
        if key not in updates.AUTOUPGRADE_KEY_ALIASES:
            raise CliError(
                "INVALID_CONFIGURATION",
                f"unknown config key {key!r}; supported: "
                + ", ".join((*updates.AUTOUPGRADE_KEY_ALIASES, forwarding_key)),
            )
        normalized = value.strip().lower()
        if normalized not in ("true", "false"):
            raise CliError(
                "INVALID_CONFIGURATION",
                f"{updates.AUTOUPGRADE_SETTINGS_KEY} accepts true|false; "
                f"got {value!r}",
            )
        enabled = normalized == "true"
        try:
            path = updates.write_auto_upgrade(enabled, _hyprial_home())
        except ValueError as error:
            raise CliError("INVALID_CONFIGURATION", str(error)) from error
        return {
            "ok": True,
            "key": updates.AUTOUPGRADE_SETTINGS_KEY,
            "value": enabled,
            "path": str(path),
        }

    _execute(operation, json_output=json_output)


PENDING_RESTART_FILE = "autoupdate-pending-restart.json"


def _pending_restart_path() -> Path:
    return _state_dir() / PENDING_RESTART_FILE


def _read_pending_restart() -> JsonObject | None:
    try:
        value = json.loads(_pending_restart_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_pending_restart(record: JsonObject) -> None:
    path = _pending_restart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _clear_pending_restart() -> None:
    try:
        _pending_restart_path().unlink()
    except FileNotFoundError:
        pass


@autoupdate_app.command("restart")
def autoupdate_restart(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Restart the daemon onto the version the timer installed (your confirmation).

    The timer installs new versions but never restarts on its own; this is the
    confirmation.  Run it yourself or have any agent run it.  With nothing
    pending it does nothing.
    """

    def operation() -> JsonObject:
        from types import SimpleNamespace

        from hyprial import updates

        pending = _read_pending_restart()
        if pending is None:
            return {"ok": True, "restarted": False, "reason": "no pending restart"}
        recorded = pending.get("before")
        before = _running_daemon_before_upgrade(
            str(recorded.get("version") if isinstance(recorded, dict) else "unknown")
        )
        if before is None:
            # No daemon: the next start runs the installed code anyway.
            _clear_pending_restart()
            return {
                "ok": True,
                "restarted": False,
                "reason": "daemon is not running; the next start uses the installed version",
                "pendingRestart": pending,
            }
        if isinstance(recorded, dict) and recorded.get("pid") != before.get("pid"):
            # Something already restarted it after the install; that daemon
            # runs the installed code.
            _clear_pending_restart()
            return {
                "ok": True,
                "restarted": False,
                "reason": "daemon was already restarted after the install",
                "before": before,
                "pendingRestart": pending,
            }
        installed = updates.read_installation()
        installed_version = str(installed.version or pending.get("version") or "unknown")
        resolved = pending.get("resolved") if isinstance(pending.get("resolved"), dict) else {}
        result = _restart_daemon_onto_install(
            {"ok": True, "confirmedRestart": True, "pendingRestart": pending},
            before=before,
            installed_version=installed_version,
            resolution=SimpleNamespace(
                tag=str(pending.get("tag") or ""), commit=str(pending.get("commit") or "")
            ),
            resolved=resolved,
        )
        _clear_pending_restart()
        return result

    _execute(operation, json_output=json_output)


@autoupdate_app.command("run", hidden=True)
def autoupdate_run(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Timer entry point: upgrade to the latest tag and record the last run."""

    def operation() -> JsonObject:
        from hyprial import updates
        from hyprial.autoupdate import AUTOUPDATE_CHILD_ENV, write_last_run
        from hyprial.home_migration import migrate_legacy_default_home

        migrate_legacy_default_home()
        require_initialized_hyprial_home()

        # Guard 1 (spec autoupdate-isolated-home r1, Allen 2026-09-15):
        # automatic upgrades are OFF unless settings.json explicitly opts in
        # (``hyprial config set autoUpgrade true``).  The check sits at THIS
        # entry point, not only in the daemon scheduler, because the legacy
        # launchd/systemd timer runs ``autoupdate run`` directly and the
        # daemon-trigger delegation below silently falls through to a local
        # upgrade when the daemon is unreachable.  The manual
        # ``hyprial upgrade`` is an operator's explicit act and is NOT gated
        # by this switch.
        if not updates.auto_upgrade_enabled(_hyprial_home()):
            skip_record: JsonObject = {
                "ok": True,
                "at": _utc_now(),
                "skipped": True,
                "skipReason": updates.AUTOUPGRADE_DISABLED_REASON,
            }
            write_last_run(_state_dir(), skip_record)
            return skip_record

        # During the staged migration the old launchd/systemd unit remains in
        # place until a daemon-owned update has succeeded.  Its invocation is
        # converted into a scheduler signal, so the actual child always
        # inherits the daemon environment and duplicate calendar fires dedup.
        if os.environ.get(AUTOUPDATE_CHILD_ENV) != "1":
            try:
                scheduled = _daemon_request(
                    "autoupdate.trigger", timeout=2.0, restore_wait=0.0
                )
            except (
                ipc_errors.DaemonUnavailableError,
                ipc_errors.DaemonDisconnectedError,
                ipc_errors.IpcTimeoutError,
            ):
                pass
            else:
                if not isinstance(scheduled, dict):
                    raise CliError(
                        "INVALID_RESPONSE",
                        "daemon autoupdate trigger result must be an object",
                    )
                return scheduled

        # The twice-daily timer is the already-happening heartbeat the
        # lark-cli user-credential watchdog hangs on (#142: a mechanism
        # nothing triggers is dead code).  Best-effort: runs before the
        # upgrade (the daemon is up at timer fire time; the upgrade may
        # restart it) and never fails the run.
        lark_auth = _lark_auth_timer_check()

        def attach(record: JsonObject) -> JsonObject:
            if lark_auth is not None:
                record["larkAuth"] = lark_auth
            return record

        try:
            result = _perform_upgrade_and_report(
                force=False,
                restart=False,
                awaiting_confirmation=True,
            )
        except (CliError, ipc_errors.TransientDaemonError) as error:
            failure_record: JsonObject = attach({
                "ok": False,
                "at": _utc_now(),
                "code": error.code,
                "error": str(error),
            })
            if error.data is not None:
                failure_record["data"] = error.data
            write_last_run(
                _state_dir(),
                failure_record,
            )
            raise
        except Exception as error:  # noqa: BLE001 - the timer must never fail silently
            write_last_run(
                _state_dir(),
                attach(
                    {
                        "ok": False,
                        "at": _utc_now(),
                        "code": "UNEXPECTED",
                        "error": str(error),
                    }
                ),
            )
            raise
        record: JsonObject = attach(
            {
                "ok": True,
                "at": _utc_now(),
                "resolvedTag": result.get("resolvedTag"),
                "resolvedCommit": result.get("resolvedCommit"),
                "upgraded": result.get("upgraded", False),
                "restartRequired": result.get("restartRequired", False),
            }
        )
        if "warning" in result:
            record["warning"] = result["warning"]
        if "restart" in result:
            record["restart"] = result["restart"]
        if "notification" in result:
            record["notification"] = result["notification"]
        # §6a: record which installed apps still need migrating, and which
        # receipts could not be read (kept distinct -- RELP2A2 追加1).  Adding
        # these fields must never turn a good upgrade's ``ok: True`` into a
        # failure: they are pure additions, and the alert below never raises.
        scan = _safe_scan_app_migrations()
        record["pendingAppMigrations"] = list(scan.pending)
        record["pendingAppMigrationsUnreadable"] = list(scan.unreadable)
        record["pendingAppMigrationsBrokenLinks"] = list(scan.broken_links)
        record["pendingAppMigrationsScanError"] = scan.scan_error
        # §6b: tell the owner **only after a self-upgrade actually installed
        # something** (design §6b "自升级后告警"; owner audit #6 -- a no-op run
        # must not DM).  ⛔ Negative control: no v1 receipts ⇒ no marker, no DM
        # (row 22); a run that upgraded nothing likewise sends nothing.
        if scan.pending and record.get("upgraded"):
            _alert_pending_app_migrations(list(scan.pending))
        write_last_run(_state_dir(), record)
        return record

    _execute(operation, json_output=json_output, allow_missing_home=True)


def _lark_auth_state_path() -> Path:
    from hyprial.adapters.lark import reauth

    return _state_dir() / reauth.STATE_FILENAME


def _canonical_lark_auth_state_path() -> Path:
    """The machine-global anchor: the real home, ignoring HYPRIAL_HOME /
    HARNESS_STATE_DIR overrides.  The pending device flow belongs to
    lark-cli's machine-global identity, and lark-cli honors neither variable.
    """

    from hyprial.adapters.lark import reauth

    return default_hyprial_home()[0] / "state" / reauth.STATE_FILENAME


def _lark_auth_guard_notify_anchor(
    state_path: Path, notify_route: str | None, sender: str | None
) -> None:
    from hyprial.adapters.lark import reauth

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

    _daemon_request("message.send", {"from": sender, "to": [route], "message": text})


def _lark_auth_timer_check() -> JsonObject | None:
    """Best-effort lark-cli user-credential check for the autoupdate timer.

    The timer is the already-happening heartbeat this watchdog hangs on (#142:
    a mechanism nothing triggers is dead code).  This must never fail the
    upgrade it rides with: every error collapses into the returned record.
    """

    try:
        from hyprial.adapters.lark import reauth

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

    def operation() -> JsonObject:
        from hyprial.adapters.lark import reauth

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
            raise CliError(error.code, str(error)) from error
        if not json_output and result.status == "awaiting_user":
            where = (
                f"pushed to {result.notify_route}"
                if result.notified
                else "NOT pushed (no route/sender configured or delivery failed)"
            )
            print(
                f"lark-cli 用户凭据需要重新授权,链接已生成({where}):\n"
                f"{result.verification_url}\n"
                "人点完后运行 `hyprial lark-auth complete` 收尾。",
                file=sys.stderr,
            )
        return result.to_json()

    _execute(operation, json_output=json_output)


@lark_auth_app.command("complete")
def lark_auth_complete(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Finish a pending device flow after the human clicked the link.

    Polls the pending device code (from the 0600 state file) until the
    platform confirms the authorization or the code expires; on success the
    remembered route gets a single all-clear notice.
    """

    def operation() -> JsonObject:
        from hyprial.adapters.lark import reauth

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
            raise CliError(error.code, str(error)) from error
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

    _execute(operation, json_output=json_output)


def _parse_adapter_routes(raw_routes: list[str]) -> tuple[Any, ...]:
    from hyprial.adapter_registration import RouteInput

    if not raw_routes:
        raise CliError(
            ipc_errors.INVALID_ARGUMENT,
            "at least one --route name=native_id is required",
        )
    parsed: list[RouteInput] = []
    for item in raw_routes:
        route_name, separator, native_id = item.partition("=")
        if not separator or not route_name or not native_id:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"--route {item!r} must be name=native_id",
            )
        parsed.append(RouteInput(name=route_name, native_id=native_id))
    return tuple(parsed)


def _read_adapter_secret(*, secret_file: Path | None, json_output: bool) -> str:
    """Obtain the Lark app secret without ever accepting it on argv.

    Precedence: ``--secret-file`` > piped stdin > interactive hidden prompt.
    A hidden prompt is only used on a TTY without ``--json`` (whose stdout must
    stay a single JSON value); otherwise an explicit source is required.
    """

    if secret_file is not None:
        try:
            secret = secret_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            reason = error.strerror or str(error)
            raise CliError(
                "SECRET_UNAVAILABLE",
                f"cannot read secret file {secret_file}: {reason}",
            ) from error
        if not secret:
            raise CliError("SECRET_REQUIRED", "secret file is empty")
        return secret
    if not sys.stdin.isatty():
        secret = sys.stdin.read().strip()
        if not secret:
            raise CliError("SECRET_REQUIRED", "no app secret provided on stdin")
        return secret
    if json_output:
        raise CliError(
            "SECRET_REQUIRED",
            "provide the app secret via --secret-file or stdin "
            "(an interactive prompt is unavailable with --json)",
        )
    import getpass

    secret = getpass.getpass("Lark app secret (hidden): ").strip()
    if not secret:
        raise CliError("SECRET_REQUIRED", "no app secret entered")
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

    def operation() -> JsonObject:
        from hyprial.adapter_registration import add_lark_gateway
        from hyprial.persistent_config import PersistentConfigError

        routes = _parse_adapter_routes(route)
        secret = _read_adapter_secret(secret_file=secret_file, json_output=json_output)
        try:
            result = add_lark_gateway(
                hyprial_home=_hyprial_home(),
                name=name,
                app_id=app_id,
                app_secret=secret,
                routes=routes,
                default_route=default_route,
                force=force,
            )
        except PersistentConfigError as error:
            from hyprial.adapter_registration import AdapterExistsError

            code = (
                "ADAPTER_EXISTS"
                if isinstance(error, AdapterExistsError)
                else ipc_errors.INVALID_ARGUMENT
            )
            raise CliError(code, str(error)) from error
        # Best-effort hot reload: a reachable daemon picks the new adapter up
        # now; an unreachable one reads the same config at its next start.
        try:
            result["daemonReload"] = _daemon_request(
                "adapter.reload", {}, timeout=5.0, restore_wait=0.0
            )
        except (CliError, ipc_errors.TransientDaemonError):
            result["daemonReload"] = None
        return result

    _execute(operation, json_output=json_output)


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
    sys.stderr.write(
        "\n"
        "  Lark App onboarding needs you to authorize in a browser.\n"
        "  Open this URL, or scan it as a QR code, with the Feishu/Lark app\n"
        f"  of the tenant that should own this App{expiry}:\n\n"
        f"    {prompt.url}\n\n"
        "  Waiting for authorization... (Ctrl-C to abort)\n\n"
    )
    sys.stderr.flush()


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

    def operation() -> JsonObject:
        import lark_oapi

        from hyprial.adapter_registration import (
            AdapterExistsError,
            add_lark_gateway,
            ensure_lark_gateway_addable,
        )
        from hyprial.adapters.lark.onboarding import (
            UserActionRequiredError,
            resolve_lark_app_credential,
        )
        from hyprial.persistent_config import PersistentConfigError

        if new and app_id is not None:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "adapter onboard accepts either --new or --app-id, not both",
            )
        routes = _parse_adapter_routes(route) if route else ()
        if default_route is not None and not routes:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--default-route requires at least one --route",
            )
        # Fail on a bad or taken name *before* asking a human to approve the
        # creation of a real App: that approval cannot be taken back, and a
        # credential we then refuse to store is an App stranded on the tenant.
        try:
            ensure_lark_gateway_addable(hyprial_home=_hyprial_home(), name=name, force=force)
        except PersistentConfigError as error:
            code = (
                "ADAPTER_EXISTS"
                if isinstance(error, AdapterExistsError)
                else ipc_errors.INVALID_ARGUMENT
            )
            raise CliError(code, str(error)) from error
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
            raise CliError("USER_ACTION_REQUIRED", str(error), error.action) from error

        try:
            registered = add_lark_gateway(
                hyprial_home=_hyprial_home(),
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
            raise CliError(code, str(error)) from error

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

    _execute(operation, json_output=json_output)


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

    try:
        status = _daemon_request("adapter.status", {"name": name})
    # PR #332 F4②: the transient half of the old mixed set is the registered
    # class; ADAPTER_NOT_FOUND (permanent) keeps its code comparison.
    except ipc_errors.DaemonUnavailableError:
        return
    except CliError as error:
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
        raise CliError(
            "ADAPTER_RUNNING",
            f"adapter {name!r} is still running; run 'hyprial adapter stop {name}' "
            "first, or pass --force to stop it as part of the removal",
        )
    stopped = _daemon_request("adapter.stop", {"name": name})
    after = stopped.get("adapter", {}) if isinstance(stopped, dict) else {}
    if (
        "pid" in after
        or after.get("online") is True
        or after.get("status") in {"online", "starting"}
    ):
        raise CliError(
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

    def operation() -> JsonObject:
        from hyprial.adapter_registration import (
            AdapterConfigConflictError,
            AdapterNotFoundError,
            remove_lark_gateway,
        )
        from hyprial.daemon.desired_state import DesiredStateError
        from hyprial.daemon.ownership import DaemonOwnershipBusy
        from hyprial.persistent_config import PersistentConfigError

        _confirm_adapter_stopped(name, force=force)
        try:
            return _daemon_request("management.adapter.remove", {"name": name})
        except ipc_errors.DaemonUnavailableError:
            pass
        try:
            return remove_lark_gateway(
                hyprial_home=_hyprial_home(), state_dir=_state_dir(), name=name
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
            raise CliError(code, str(error)) from error
        except DesiredStateError as error:
            # Fail closed: never rewrite config against a desired-state file
            # we cannot parse.
            raise CliError("DESIRED_STATE_ERROR", str(error)) from error
        except DaemonOwnershipBusy as error:
            raise CliError(error.code, str(error)) from error

    _execute(operation, json_output=json_output)


@adapter_app.command("start")
def adapter_start(
    name: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Start an external-platform adapter."""

    _execute(
        lambda: _daemon_request("adapter.start", {"name": name}),
        json_output=json_output,
    )


@adapter_app.command("stop")
def adapter_stop(
    name: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Stop an external-platform adapter."""

    _execute(
        lambda: _daemon_request("adapter.stop", {"name": name}), json_output=json_output
    )


@adapter_app.command("status")
def adapter_status(
    name: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show one external-platform adapter."""

    _execute(
        lambda: _daemon_request("adapter.status", {"name": name}),
        json_output=json_output,
    )


@adapter_app.command("reload")
def adapter_reload(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Reload adapter configs into the running daemon (incremental)."""

    _execute(lambda: _daemon_request("adapter.reload", {}), json_output=json_output)


@adapter_app.command("list")
def adapter_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List external-platform adapters."""

    _execute(lambda: _daemon_request("adapter.list", {}), json_output=json_output)


route_app = typer.Typer(
    help="Manage one adapter's outbound routes (route:<adapter>:<route>)."
)
adapter_app.add_typer(route_app, name="route")


def _parse_one_route(value: str) -> Any:
    """Parse a single ``name=native_id`` pair, reusing the add-time rules."""

    return _parse_adapter_routes([value])[0]


def _route_error_code(error: Exception) -> str:
    """Map a config error to the same code ``adapter remove`` reports."""

    from hyprial.adapter_registration import AdapterNotFoundError

    if isinstance(error, AdapterNotFoundError):
        return ipc_errors.ADAPTER_NOT_FOUND
    return getattr(error, "code", None) or ipc_errors.INVALID_ARGUMENT


def _route_operation(call: Any) -> JsonObject:
    """Run one route mutation, map its error code, then hot-reload.

    A route that exists only in the file is a route the running daemon cannot
    deliver to, so the reload belongs to the change -- but an unreachable
    daemon is not a failure: it reads the same config at its next start. This
    is the same contract as ``adapter add``.
    """

    from hyprial.persistent_config import PersistentConfigError

    try:
        result = call()
    except PersistentConfigError as error:
        raise CliError(_route_error_code(error), str(error)) from error
    try:
        result["daemonReload"] = _daemon_request(
            "adapter.reload", {}, timeout=5.0, restore_wait=0.0
        )
    except (CliError, ipc_errors.TransientDaemonError):
        result["daemonReload"] = None
    return result


@route_app.command("list")
def adapter_route_list(
    name: str | None = typer.Argument(
        None, help="Configured adapter name; omit to list every adapter."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the routes bound on an adapter, read from local config."""

    def operation() -> JsonObject:
        from hyprial.adapter_registration import list_gateway_routes
        from hyprial.persistent_config import PersistentConfigError

        try:
            return list_gateway_routes(hyprial_home=_hyprial_home(), name=name)
        except PersistentConfigError as error:
            raise CliError(_route_error_code(error), str(error)) from error

    _execute(operation, json_output=json_output)


@route_app.command("add")
def adapter_route_add(
    name: str = typer.Argument(..., help="Configured adapter name."),
    route: str = typer.Argument(
        ..., help="Route as name=native_chat_id (the chat/user id to send to)."
    ),
    make_default: bool = typer.Option(
        False, "--default", help="Also make this the gateway's default route."
    ),
    force: bool = typer.Option(
        False, "--force", help="Rebind a route name that already exists."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Bind one outbound route on an existing adapter.

    Touches only ``channels.json`` -- never the App credential -- then asks a
    running daemon to reload. Use this instead of ``adapter add --force``,
    which replaces the whole gateway entry and rewrites its secret file.
    """

    def operation() -> JsonObject:
        from hyprial.adapter_registration import add_gateway_route

        parsed = _parse_one_route(route)
        return _route_operation(
            lambda: add_gateway_route(
                hyprial_home=_hyprial_home(),
                name=name,
                route=parsed,
                make_default=make_default,
                force=force,
            )
        )

    _execute(operation, json_output=json_output)


@route_app.command("remove")
def adapter_route_remove(
    name: str = typer.Argument(..., help="Configured adapter name."),
    route_name: str = typer.Argument(..., help="Route name to unbind."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Remove even when it is the default route (clears the default).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Unbind one outbound route from an adapter."""

    def operation() -> JsonObject:
        from hyprial.adapter_registration import remove_gateway_route

        return _route_operation(
            lambda: remove_gateway_route(
                hyprial_home=_hyprial_home(),
                name=name,
                route_name=route_name,
                force=force,
            )
        )

    _execute(operation, json_output=json_output)


@adapter_app.command("doctor")
def adapter_doctor(
    name: str = typer.Argument(..., help="Configured Lark adapter name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Inventory declared and tenant-authorized Lark App scopes."""

    def operation() -> JsonObject:
        from hyprial.adapters.lark.scopes import configured_scope_client, diagnose_scopes
        from hyprial.persistent_config import PersistentConfigError

        try:
            app_id, client = configured_scope_client(_hyprial_home(), _state_dir(), name)
            return diagnose_scopes(
                adapter=name, app_id=app_id, response=client.list_scopes()
            )
        except PersistentConfigError as error:
            raise CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error

    _execute(operation, json_output=json_output)


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
    sys.stderr.write(
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
    sys.stderr.flush()


def _authorize_interactively(name: str, capabilities: Sequence[str]) -> JsonObject:
    """Mint and present a one-click device-authorization link for an adapter.

    The adapter must already exist: its configured app_id is what makes this a
    re-authorization rather than an App creation. There is deliberately no
    ``ensure_lark_gateway_addable`` guard here -- that guard protects the
    *creation* path from stranding a new App behind a name clash, and applying
    it here would reject exactly the adapters this command is for.
    """

    import lark_oapi

    from hyprial.adapters.lark.onboarding import (
        LARK_ONBOARDING_REQUIRED_EVENTS,
        LarkOnboardingError,
        reauthorize_lark_app,
    )
    from hyprial.adapters.lark.scopes import (
        SENSITIVE_CAPABILITY_NOTES,
        capability_scopes,
        configured_app_id,
    )
    from hyprial.persistent_config import PersistentConfigError

    requested = list(dict.fromkeys(capabilities))
    try:
        scopes = capability_scopes(requested)
    except ValueError as error:
        raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    try:
        # Only the app_id is resolved: this flow authenticates the human, so no
        # app secret is fetched, sent, rotated, or re-persisted anywhere in it.
        app_id = configured_app_id(_hyprial_home(), _state_dir(), name)
    except PersistentConfigError as error:
        raise CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error

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
        raise CliError("LARK_AUTHORIZATION_FAILED", str(error)) from error
    except Exception as error:  # noqa: BLE001 - SDK raises its own error types
        raise CliError(
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

    def operation() -> JsonObject:
        from hyprial.adapters.lark.scopes import (
            configured_app_id,
            configured_scope_client,
            developer_console_permission_url,
            request_scope_authorization,
            scope_apply_url,
            valid_scope_name,
        )
        from hyprial.persistent_config import PersistentConfigError

        if scope and (interactive or capability):
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--scope cannot be combined with --interactive or --capability",
            )
        if capability and not interactive:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--capability requires --interactive: the scopes/apply endpoint "
                "cannot declare a scope the App does not already have",
            )
        if interactive:
            return _authorize_interactively(name, capability)
        if scope:
            invalid = [value for value in scope if not valid_scope_name(value)]
            if invalid:
                raise CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "invalid --scope value(s): "
                    + ", ".join(repr(value) for value in invalid)
                    + "; scope names must be non-empty and contain no whitespace "
                    "or commas",
                )
            selected = list(dict.fromkeys(scope))
            try:
                app_id = configured_app_id(_hyprial_home(), _state_dir(), name)
            except PersistentConfigError as error:
                raise CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error
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
            app_id, client = configured_scope_client(_hyprial_home(), _state_dir(), name)
            result = request_scope_authorization(client.apply_scopes())
        except PersistentConfigError as error:
            raise CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error
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

    _execute(operation, json_output=json_output)


@adapter_app.command("pin")
def adapter_pin(
    adapter: str = typer.Argument(..., help="Configured adapter name."),
    actor: str = typer.Argument(
        ..., help="Agent name (or canonical agent:<owner>:<machine>:<actor> URI)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Bind this adapter's inbound messages to one agent on this machine.

    Adapter and agent bind one-to-one: an adapter routes to exactly one agent,
    and an agent can be pinned by at most one adapter (pin the target
    elsewhere first requires 'hyprial adapter unpin' on its current adapter). The
    agent must already exist ('hyprial agent create' / 'hyprial start'); the stored
    value is always its canonical URI. The pin lives on the agent's record and
    disappears with 'hyprial agent destroy'. Inbound-only: replies travel with
    each message's own correlation, never by reverse lookup of this pin.
    """

    _execute(
        lambda: _daemon_request("adapter.pin", {"name": adapter, "actor": actor}),
        json_output=json_output,
    )


@adapter_app.command("unpin")
def adapter_unpin(
    adapter: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove an adapter's receiver pin from its agent's record."""

    _execute(
        lambda: _daemon_request("adapter.unpin", {"name": adapter}),
        json_output=json_output,
    )


@adapter_app.command("pins")
def adapter_pins(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List adapter receiver pins (adapter -> canonical agent URI)."""

    _execute(lambda: _daemon_request("adapter.pins", {}), json_output=json_output)


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

    def operation() -> JsonObject:
        from hyprial.daemon.identity import resolve_node_owner
        from hyprial.daemon.ownership import DaemonOwnershipBusy
        from hyprial.management import (
            EnsureSquireRegistryCommand,
            ManagementError,
            OfflineManagementLease,
            SquireRegistryResult,
        )
        from hyprial.squire import SquireSetup, derive_setup_identity

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
                try:
                    response = _daemon_request(
                        "management.squire.ensure", command.to_payload()
                    )
                except ipc_errors.DaemonUnavailableError:
                    if command.start:
                        raise
                    try:
                        with OfflineManagementLease(
                            _state_dir(),
                            owner=command.owner,
                            machine=command.machine,
                            hyprial_home=_hyprial_home(),
                        ) as management:
                            return management.ensure_squire(command)
                    except DaemonOwnershipBusy as busy:
                        raise CliError(busy.code, str(busy)) from busy
                    except ManagementError as managed_error:
                        raise CliError(
                            managed_error.code, str(managed_error)
                        ) from managed_error
                if not isinstance(response, dict):
                    raise CliError(
                        ipc_errors.INVALID_RESPONSE,
                        "management.squire.ensure result must be an object",
                    )
                return SquireRegistryResult.from_payload(response)

        setup = SquireSetup(
            hyprial_home=_hyprial_home(),
            state_dir=_state_dir(),
            management_port=CliSquireManagement(),
        )
        try:
            return setup.run(
                identity,
                channel=channel,
                owner_open_id=owner_open_id,
                binding_code=binding_code,
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
        except ManagementError as error:
            raise CliError(error.code, str(error)) from error
        except ValueError as error:
            raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error

    _execute(operation, json_output=json_output)


def _parse_combo(raw: str) -> tuple[str, str | None, str]:
    parts = raw.split(":")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"--combo {raw!r} must be harness:provider:model "
            "(provider may be empty, e.g. claude::sonnet)",
        )
    harness, provider, model = parts
    return harness, provider or None, model


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

    def operation() -> JsonObject:
        from hyprial.squire import (
            RuntimeProber,
            UserProfileError,
            UserProfileStore,
            probe_combinations,
        )

        store = UserProfileStore(_state_dir() / "users.json")
        key = owner_key
        if key is None:
            profiles = store.list()
            if not profiles:
                raise CliError(
                    "NOT_FOUND", "no user profile; run hyprial squire setup first"
                )
            if len(profiles) > 1:
                raise CliError(
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
            raise CliError(
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
            raise CliError("NOT_FOUND", str(error)) from error
        except ValueError as error:
            raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        return {
            "ok": True,
            "probe": "squire-runtime-capability",
            "schemaVersion": 1,
            "ownerKey": key,
            "dryRun": dry_run,
            "changed": list(changed),
            "results": [result.to_json() for result in results],
            "profile": profile.to_json(),
        }

    _execute(operation, json_output=json_output)


identities_app = typer.Typer(
    help=(
        "Record and query platform-identity mappings (who is who): "
        "platform id ↔ display name ↔ hyprial owner, with provenance and "
        "confidence. Identity lookups run on recorded data, never on "
        "live-chat deduction."
    )
)
adapter_app.add_typer(identities_app, name="identities")


def _identity_json(identity: Any) -> JsonObject:
    return {
        "kind": identity.kind,
        "platformId": identity.platform_id,
        "displayName": identity.display_name,
        "unionId": identity.union_id,
        "hyprialOwner": identity.hyprial_owner,
        "standing": identity.standing,
        "source": identity.source,
        "firstSeenMs": identity.first_seen_ms,
        "lastSeenMs": identity.last_seen_ms,
    }


def _identities_result(name: str, rows: Any) -> JsonObject:
    return {
        "adapter": name,
        "count": len(rows),
        "identities": [_identity_json(row) for row in rows],
    }


def _checked_identity_kind(kind: str | None) -> str | None:
    from hyprial.adapters.lark.state import IDENTITY_KINDS

    if kind is not None and kind not in IDENTITY_KINDS:
        raise CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"--kind must be one of {', '.join(IDENTITY_KINDS)}",
        )
    return kind


@identities_app.command("sync")
def adapter_identities_sync(
    name: str = typer.Argument(..., help="Configured Lark adapter name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Enumerate this adapter's identities from the platform into the store.

    Records the App itself, its bot open_id, and every member of every group
    the App belongs to — draining every pagination cursor (a truncated member
    view once produced a confidently wrong who-is-who). Uses the adapter's
    own App credential: open_ids are namespaced per App, so ids enumerated
    through any other App would never match this adapter's events. Read-only
    platform calls; nothing is sent to any chat.
    """

    def operation() -> JsonObject:
        from hyprial.adapters.lark.identities import (
            configured_identity_gateway,
            open_identity_store,
            sync_identities,
        )
        from hyprial.persistent_config import PersistentConfigError

        try:
            app_id, gateway = configured_identity_gateway(
                _hyprial_home(), _state_dir(), name
            )
        except PersistentConfigError as error:
            raise CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error
        store = open_identity_store(_state_dir(), name)
        try:
            report = sync_identities(state=store, gateway=gateway, app_id=app_id)
        finally:
            store.close()
        return {"adapter": name, **report}

    _execute(operation, json_output=json_output)


@identities_app.command("list")
def adapter_identities_list(
    name: str = typer.Argument(..., help="Lark adapter name."),
    kind: str | None = typer.Option(
        None, "--kind", help="Filter by identity kind: user, bot or app."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List recorded identities for one adapter's namespace."""

    def operation() -> JsonObject:
        from hyprial.adapters.lark.identities import open_identity_store

        _checked_identity_kind(kind)
        store = open_identity_store(_state_dir(), name)
        try:
            rows = store.identities(kind=kind)
        finally:
            store.close()
        return _identities_result(name, rows)

    _execute(operation, json_output=json_output)


@identities_app.command("find")
def adapter_identities_find(
    name: str = typer.Argument(..., help="Lark adapter name."),
    display_name: str | None = typer.Option(
        None, "--name", help="Display-name substring to search for."
    ),
    platform_id: str | None = typer.Option(
        None, "--id", help="Exact platform id (open_id or app_id)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Find recorded identities by display name and/or platform id."""

    def operation() -> JsonObject:
        from hyprial.adapters.lark.identities import open_identity_store

        if display_name is None and platform_id is None:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "identities find requires --name and/or --id",
            )
        store = open_identity_store(_state_dir(), name)
        try:
            rows = store.find_identities(name=display_name, platform_id=platform_id)
        finally:
            store.close()
        return _identities_result(name, rows)

    _execute(operation, json_output=json_output)


@identities_app.command("upsert")
def adapter_identities_upsert(
    name: str = typer.Argument(..., help="Lark adapter name."),
    kind: str = typer.Option(..., "--kind", help="Identity kind: user, bot or app."),
    platform_id: str = typer.Option(
        ..., "--id", help="Platform id: open_id (user/bot) or app_id (app)."
    ),
    source: str = typer.Option(
        ...,
        "--source",
        help="Provenance, e.g. 'manual:allen-confirmed 2026-08-14'.",
    ),
    display_name: str | None = typer.Option(None, "--name", help="Display name."),
    union_id: str | None = typer.Option(
        None,
        "--union-id",
        help="Feishu cross-App union_id, when known.",
    ),
    owner: str | None = typer.Option(
        None, "--owner", help="The hyprial owner/actor this identity maps to."
    ),
    standing: str = typer.Option(
        "observed",
        "--standing",
        help="observed (mechanical/presumed) or verified (human-confirmed).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Authoritatively record one identity mapping (operator surface).

    This is the only surface that may set or revoke 'verified' standing;
    'hyprial adapter identities sync' and passive event collection never
    overwrite a verified row.
    """

    def operation() -> JsonObject:
        from hyprial.adapters.lark.identities import open_identity_store
        from hyprial.adapters.lark.state import Identity

        store = open_identity_store(_state_dir(), name)
        try:
            stored = store.upsert_identity(
                Identity(
                    kind=kind,
                    platform_id=platform_id,
                    display_name=display_name,
                    union_id=union_id,
                    hyprial_owner=owner,
                    standing=standing,
                    source=source,
                )
            )
        except ValueError as error:
            raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        finally:
            store.close()
        return {"adapter": name, "identity": _identity_json(stored)}

    _execute(operation, json_output=json_output)


media_app = typer.Typer(
    help=(
        "Retrieve platform media referenced by inbound stand-ins. Image and "
        "file messages reach agents as labels carrying a "
        "ref:<message_id>/<key> handle; 'media get' turns that handle into "
        "a local file on demand (directed pull — nothing is auto-inlined)."
    )
)
adapter_app.add_typer(media_app, name="media")


@media_app.command("get")
def adapter_media_get(
    name: str = typer.Argument(..., help="Configured Lark adapter name."),
    ref: str = typer.Argument(
        ...,
        help=(
            "Media reference as printed in the stand-in label: "
            "<message_id>/<key>, optionally prefixed with 'ref:'."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Download one referenced image/file via the adapter's own credential.

    Works for refs harvested anywhere a stand-in appears — direct messages,
    rich-text posts, and merge-forward children at any nesting depth (the
    ref names its carrying message directly). The payload is stored under
    <hyprial-home>/media/<adapter>/ (0700) and the local path is printed.
    """

    def operation() -> Any:
        from hyprial.adapters.lark.identities import configured_identity_gateway
        from hyprial.adapters.lark.media import (
            MediaFetchError,
            fetch_media,
            parse_media_ref,
        )
        from hyprial.persistent_config import PersistentConfigError

        try:
            parsed = parse_media_ref(ref)
        except ValueError as error:
            raise CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        try:
            # The same credential-loading path as 'identities sync': the
            # adapter's own App secret, resolved locally, never printed.
            _app_id, gateway = configured_identity_gateway(
                _hyprial_home(), _state_dir(), name
            )
        except PersistentConfigError as error:
            raise CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error
        try:
            result = fetch_media(
                gateway,
                adapter=name,
                ref=parsed,
                media_root=_hyprial_home() / "media",
            )
        except MediaFetchError as error:
            # Already sanitized: summaries only, never SDK error text.
            raise CliError("MEDIA_FETCH_FAILED", str(error)) from error
        if not json_output:
            return str(result.path)
        return {
            "adapter": result.adapter,
            "messageId": result.message_id,
            "key": result.key,
            "resourceType": result.resource_type,
            "path": str(result.path),
            "sizeBytes": result.size_bytes,
            "contentType": result.content_type,
            "fileName": result.file_name,
        }

    _execute(operation, json_output=json_output)


# ── H3: mount app-declared commands (design-app-manifest-commands §3) ─────────
_MOUNTED_NAMES: set[str] = set()


# Runs after every built-in is registered so the collision check sees the real
# inventory.  Reads only receipts + manifests and cannot raise: each bad app is
# skipped with a visible warning.  ⛔ Nothing below this line may add a
# built-in -- it would be invisible to the collision check.
def _mount_app_commands() -> None:
    from typer.main import get_command

    from hyprial.installers import mount as _mount

    try:
        home = _hyprial_home()
    except Exception:  # noqa: BLE001 - no resolvable home ⇒ nothing installed
        return
    # Names this hook mounted earlier in the process are not built-ins; without
    # this, a second run (tests re-mount per HYPRIAL_HOME) would refuse its own
    # previous mount as a collision.
    builtins = get_command(app).commands.keys() - _MOUNTED_NAMES
    report = _mount.discover_mounts(home, builtin_names=builtins)
    _MOUNTED_NAMES.update(m.command.name for m in report.mounts)
    _mount.warn_skipped(report)

    def register(name: str, help_text: str, fn: Any) -> None:
        if name == "gui":
            def gui_command(
                app_or_action: str = typer.Argument("start", help="start (default), status, stop, or upgrade the DSH GUI."),
                check: bool = typer.Option(False, "--check", help="Upgrade: report only."),
                force: bool = typer.Option(False, "--force", help="Upgrade: replace dirty/diverged sources."),
                yes: bool = typer.Option(False, "--yes", help="Upgrade: skip the confirmation prompt."),
                json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
            ) -> None:
                def operation() -> Any:
                    from hyprial.gui_apps import resolve_invocation

                    verb, selected = resolve_invocation(app_or_action)
                    if verb != "upgrade" and (check or force or yes):
                        raise CliError(ipc_errors.INVALID_ARGUMENT, "--check, --force, and --yes are only valid with: hyprial gui upgrade")
                    options = dict(check=check, force=force, yes=yes, json_output=json_output)
                    if selected is not None:
                        options["gui_app"] = selected
                    return fn(verb, **options)

                _execute(operation, json_output=json_output)

            app.command(name, help=(
                "DSH GUI: hyprial gui [start|status|stop|upgrade]. "
                "Starts DSH by default; upgrade updates the GUI package."
            ))(gui_command)
            return
        # ``fn`` validates the action against the manifest; this wrapper only
        # adds the CLI surface.  ``--check/--force/--yes`` exist because the
        # old ``hyprial gui upgrade`` had them, and dropping flags during a
        # migration is the kind of omission that reads as completion.
        def command(
            action: str = typer.Argument("start", help="start, status, stop, or upgrade."),
            check: bool = typer.Option(False, "--check", help="Upgrade: report only."),
            force: bool = typer.Option(False, "--force", help="Upgrade: replace dirty/diverged sources."),
            yes: bool = typer.Option(False, "--yes", help="Upgrade: skip the confirmation prompt."),
            json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
        ) -> None:
            def operation() -> Any:
                # Inside _execute so it renders through the same envelope as
                # every other CLI error; raised outside, it escapes as a bare
                # exception with no output.
                if action != "upgrade" and (check or force or yes):
                    raise CliError(
                        ipc_errors.INVALID_ARGUMENT,
                        f"--check, --force, and --yes are only valid with: hyprial {name} upgrade",
                    )
                return fn(action, check=check, force=force, yes=yes, json_output=json_output)

            _execute(operation, json_output=json_output)

        command.__name__ = name
        command.__doc__ = help_text
        app.command(name)(command)

    _mount.register_mounts(report, register=register, run=_run_mounted_app_action)


def _run_mounted_app_action(
    name: str, action: str, *, check: bool = False, force: bool = False,
    yes: bool = False, json_output: bool = False, gui_app: str | None = None,
) -> Any:
    """The generic body behind every mounted ``hyprial <cmd> <action>``."""

    hyprial_home = _hyprial_home()
    if gui_app is not None and (name != "gui" or action == "upgrade"):
        raise CliError(
            ipc_errors.INVALID_ARGUMENT,
            "Application selection is only valid for GUI start/status/stop; "
            "upgrade updates the whole GUI package",
        )
    if name == "gui" and action != "upgrade":
        from hyprial.gui_apps import perform

        return perform(hyprial_home, action, gui_app)
    if action == "status":
        return gui_status(hyprial_home, name)
    if action == "stop":
        return stop_gui(hyprial_home, name)
    if action == "upgrade":
        if check and (force or yes):
            raise CliError(ipc_errors.INVALID_ARGUMENT, "--check cannot be combined with --force or --yes")
        return upgrade_mounted_app(
            name, hyprial_home, check_only=check, force=force, yes=yes, json_output=json_output,
        )
    return start_gui_background(hyprial_home, name)


def gui_status(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.gui_runtime import gui_status as operation

    return operation(hyprial_home, name)


def start_gui_background(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.gui_runtime import start_gui_background as operation

    return operation(hyprial_home, name)


def stop_gui(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.gui_runtime import stop_gui as operation

    return operation(hyprial_home, name)


def upgrade_mounted_app(
    name: str, hyprial_home: Path, *, check_only: bool, force: bool, yes: bool, json_output: bool,
) -> dict[str, Any]:
    """Upgrade a mounted app and preserve its prior running state.

    The old ``upgrade_gui`` with the app name threaded through; the
    confirm / stop / apply / restart shape is unchanged.
    """

    prior = gui_status(hyprial_home, name) if name != "gui" else {}
    was_running = prior.get("state") == "running"
    stopped = False

    def confirm(plan: JsonObject) -> bool:
        if json_output and not yes:
            raise CliError(
                "CONFIRMATION_REQUIRED",
                f"{name} upgrade requires confirmation; rerun with --yes",
                plan,
            )
        if not json_output:
            _emit(plan, json_output=False)
        return yes or typer.confirm(f"Upgrade {name} to this exact commit?")

    if name == "gui":
        from hyprial.gui_apps import upgrade

        return upgrade(
            hyprial_home,
            lambda before: upgrade_application(
                name,
                hyprial_home=hyprial_home,
                confirm=confirm,
                check_only=check_only,
                force=force,
                json_output=json_output,
                before_apply=before,
            ),
        )

    def before_apply() -> None:
        nonlocal stopped
        if was_running:
            stop_gui(hyprial_home, name)
            stopped = True

    try:
        result = upgrade_application(
            name, hyprial_home=hyprial_home, confirm=confirm, check_only=check_only,
            force=force, json_output=json_output, before_apply=before_apply,
        )
    except BaseException:
        if stopped:
            start_gui_background(hyprial_home, name)
        raise
    if stopped:
        restarted = start_gui_background(hyprial_home, name)
        result["restarted"] = restarted.get("state") == "running"
        result["url"] = restarted.get("url")
    else:
        result["restarted"] = False
    return result


_mount_app_commands()


def main() -> None:
    """Installed ``hyprial`` entry point."""

    json_output = "--json" in sys.argv[1:]
    try:
        exit_code = app(standalone_mode=False)
    except TyperClickException as error:
        if json_output:
            _emit(
                {
                    "ok": False,
                    "code": ipc_errors.INVALID_ARGUMENT,
                    "error": error.format_message(),
                },
                json_output=True,
            )
        else:
            from rich.console import Console

            Console(stderr=True).print(
                f"[bold red]hyprial:[/bold red] {error.format_message()}"
            )
        raise SystemExit(error.exit_code) from error
    except TyperAbort as error:
        if json_output:
            _emit(
                {"ok": False, "code": "ABORTED", "error": "operation aborted"},
                json_output=True,
            )
        else:
            from rich.console import Console

            Console(stderr=True).print("[bold red]hyprial:[/bold red] operation aborted")
        raise SystemExit(1) from error
    if isinstance(exit_code, int) and exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
