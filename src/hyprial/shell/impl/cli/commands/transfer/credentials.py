"""Transfer credential seal/open over the bundle channel."""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import typer



def register_credential_commands(app, get_dependencies):

    @app.command("transfer-credential-seal")
    def transfer_credential_seal(
        credentials_file: Path = typer.Option(
            ...,
            "--from",
            help="JSON object mapping credential name to value.",
        ),
        envelope_file: Path = typer.Option(
            ...,
            "--to",
            help="Sealed envelope to write; readable only by the target's private key.",
        ),
        recipient_public_key: str = typer.Option(
            ...,
            "--recipient-public-key",
            help="Base64 raw X25519 public key of the target machine.",
        ),
        actor: str = typer.Option(..., "--actor", help="Agent actor the credentials belong to."),
        owner: str = typer.Option(..., "--owner", help="Owner the credentials belong to."),
        record: list[str] | None = typer.Option(
            None,
            "--record",
            help="Per-credential risk metadata as JSON; repeatable.",
        ),
        host_owner_grant: str | None = typer.Option(
            None,
            "--host-owner-grant",
            help="Only when the host owner explicitly let this landing use host credentials.",
        ),
        use_host_credentials: bool = typer.Option(
            False,
            "--use-host-credentials",
            help="Request host credentials; refused without an explicit host owner grant.",
        ),
        created_at: str | None = typer.Option(
            None,
            "--created-at",
            help="ISO timestamp to record (default: now, local offset).",
        ),
        prototype: bool = typer.Option(
            False,
            "--prototype",
            help="Acknowledge this envelope is a replaceable prototype, not a frozen protocol.",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Seal credentials for a transfer bundle (AT06 credential envelope, prototype)."""

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type

        from hyprial.daemon import (
            EnvelopeError,
            resolve_policy,
            seal_envelope,
            write_envelope,
        )

        def operation() -> dict[str, object]:
            if not prototype:
                raise CliError(
                    "envelope_prototype_required",
                    "the credential envelope is a replaceable prototype until the "
                    "manifest/credential ruling; pass --prototype to use it",
                )
            try:
                credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CliError("envelope_invalid", f"cannot read credentials JSON: {error}") from error
            if not isinstance(credentials, dict):
                raise CliError("envelope_invalid", "credentials file must hold a JSON object")
            records: list[object] = []
            for raw in record or ():
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as error:
                    raise CliError("envelope_invalid", f"--record is not JSON: {error}") from error
                if not isinstance(parsed, dict):
                    raise CliError("envelope_invalid", "--record must be a JSON object")
                records.append(parsed)
            try:
                key = base64.b64decode(recipient_public_key, validate=True)
            except (ValueError, TypeError) as error:
                raise CliError("envelope_invalid", "recipient public key is not base64") from error
            try:
                policy = resolve_policy(
                    requested="host" if use_host_credentials else "original-user",
                    host_owner_grant=host_owner_grant,
                )
                envelope = seal_envelope(
                    credentials,
                    recipient_public_key=key,
                    actor=actor,
                    owner=owner,
                    created_at=created_at or datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
                    records=records,
                    policy=policy,
                )
            except EnvelopeError as error:
                raise CliError(error.code, str(error)) from error
            write_envelope(envelope_file, envelope)
            return {
                "envelope": str(envelope_file),
                "actor": actor,
                "owner": owner,
                "algorithm": envelope["algorithm"],
                "prototype": envelope["prototype"],
                "policy": policy.as_dict(),
                "credentials": envelope["credentials"],
            }

        _execute(operation, json_output=json_output)


    @app.command("transfer-credential-open")
    def transfer_credential_open(
        envelope_file: Path = typer.Argument(..., help="Sealed envelope to open."),
        key_file: Path = typer.Option(
            ...,
            "--key",
            help="File holding this machine's raw 32-byte X25519 private key (or its base64).",
        ),
        prototype: bool = typer.Option(
            False,
            "--prototype",
            help="Acknowledge this envelope is a replaceable prototype, not a frozen protocol.",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Open a sealed credential envelope (AT06 credential envelope, prototype)."""

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type

        from hyprial.daemon import EnvelopeError, load_envelope, open_envelope

        def operation() -> dict[str, object]:
            if not prototype:
                raise CliError(
                    "envelope_prototype_required",
                    "the credential envelope is a replaceable prototype until the "
                    "manifest/credential ruling; pass --prototype to use it",
                )
            raw = key_file.read_bytes()
            private = raw
            if len(raw) != 32:
                try:
                    private = base64.b64decode(raw.strip(), validate=True)
                except (ValueError, TypeError) as error:
                    raise CliError("envelope_invalid", "--key is not a raw 32-byte key") from error
            if len(private) != 32:
                raise CliError("envelope_invalid", "--key file must hold 32 raw bytes or their base64")
            try:
                envelope = load_envelope(envelope_file)
                credentials = open_envelope(envelope, recipient_private_key=private)
            except EnvelopeError as error:
                raise CliError(error.code, str(error)) from error
            return {
                "envelope": str(envelope_file),
                "actor": envelope.get("actor"),
                "owner": envelope.get("owner"),
                "credentials": credentials,
            }

        _execute(operation, json_output=json_output)

    return {
        "transfer_credential_seal": transfer_credential_seal,
        "transfer_credential_open": transfer_credential_open,
    }
