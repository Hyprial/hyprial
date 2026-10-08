"""Worker transfer session command registration."""

from __future__ import annotations

import os
import sys
from typing import Any

import typer



def register_session_commands(app, get_dependencies):

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
                "owner's credentials (design docs/design/design-transfer-container.md)."
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

        dependencies = get_dependencies()
        _execute = dependencies.execute
        _daemon_request = dependencies.daemon_request
        CliError = dependencies.error_type
        configured_hyprial_home = dependencies.configured_home
        default_hyprial_home = dependencies.default_home
        print = dependencies.print

        def operation() -> Any:
            from hyprial.daemon import TransferError, run_transfer
            from hyprial.daemon import SshRunner

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
                # Facts must be visible BEFORE execution, including --json, so
                # they go to stderr in both modes through the injected print
                # hook (tests observe their order against the transfer calls;
                # allowlisted in the output discipline gate).
                print(message, file=sys.stderr, flush=True)

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



    def _parse_depends(values: "list[str] | tuple[str, ...]") -> "list[dict[str, object]]":
        """Parse ``--depends`` into manifest dependency records.

        ``KIND=PATH`` declares a file the bundle carries (``present=True``);
        ``KIND=!note`` declares something the target must provide.
        """

        dependencies = get_dependencies()
        CliError = dependencies.error_type

        records: list[dict[str, object]] = []
        for raw in values:
            kind, sep, value = raw.partition("=")
            kind = kind.strip()
            value = value.strip()
            if not sep or not kind:
                raise CliError(
                    "depends_invalid",
                    f"--depends needs KIND=PATH or KIND=!note, got {raw!r}",
                )
            if value.startswith("!"):
                note = value[1:].strip()
                if not note:
                    raise CliError(
                        "depends_invalid",
                        f"--depends {raw!r} declares nothing the target must provide",
                    )
                records.append({"kind": kind, "present": False, "note": note})
            elif value:
                records.append({"kind": kind, "present": True, "path": value})
            else:
                raise CliError(
                    "depends_invalid", f"--depends {raw!r} names no path"
                )
        return records

    return {
        "transfer": transfer,
        "_parse_depends": _parse_depends,
    }
