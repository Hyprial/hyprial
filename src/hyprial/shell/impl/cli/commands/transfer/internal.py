"""Worker transfer session command registration."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer

from hyprial.kernel import ipc_errors


def register_hidden_session_commands(app, get_dependencies):

    @app.command("transfer-precheck", hidden=True)
    def transfer_precheck(
        harness: str = typer.Option(..., "--harness"),
        name: str = typer.Option(..., "--name"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Target-side admission check (driven by 'hyprial transfer' over SSH)."""

        dependencies = get_dependencies()
        _execute = dependencies.execute
        _daemon_request = dependencies.daemon_request

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

        dependencies = get_dependencies()
        _execute = dependencies.execute
        _daemon_request = dependencies.daemon_request
        CliError = dependencies.error_type

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

        dependencies = get_dependencies()
        _execute = dependencies.execute
        _state_dir = dependencies.state_dir
        CliError = dependencies.error_type

        def operation() -> Any:
            from hyprial.daemon import transfer_container as xfer_container

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

        dependencies = get_dependencies()
        _execute = dependencies.execute
        _state_dir = dependencies.state_dir

        def operation() -> Any:
            from hyprial.daemon import transfer_container as xfer_container

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

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type

        def operation() -> Any:
            from hyprial.daemon import (
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

    return {
        "transfer_precheck": transfer_precheck,
        "transfer_receive": transfer_receive,
        "transfer_cred_stage": transfer_cred_stage,
        "transfer_container_home": transfer_container_home,
        "transfer_session_path": transfer_session_path,
    }
