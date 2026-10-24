"""``hyprial transfer-trust-source-*``: the target's offline enrollment surface.

A landing refuses any bundle whose ``signature.json`` names a key this target
has not enrolled.  That anchor is a local file --
``<home>/secrets/transfer/trusted-source-keys.json``, 0700/0600 -- and this
slice fetches no keys online, so enrolling a source machine is an operator's
explicit, auditable act.  These three commands are the whole surface for it:
enroll one key, list what is enrolled, retire one.  None of them needs the
daemon: they read and write the target's own home.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import typer


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_public_key(value: str) -> bytes:
    """Read a 32-byte Ed25519 public key from a record file or base64 text.

    The operator copies the *public* half, so ``--public-key`` accepts a path
    to the source machine's key record (``secrets/transfer/signing-key.json``),
    a file holding nothing but the base64 key, or the base64 text itself.
    Raises ``ValueError``; the command maps that onto the daemon's own
    ``bundle_signature_malformed`` code so the refusal names the same class
    as every other malformed-key refusal.
    """

    text = value.strip()
    candidate = Path(value).expanduser()
    if candidate.is_file():
        text = candidate.read_text("utf-8").strip()
    if text.startswith("{"):
        try:
            record = json.loads(text)
        except ValueError as error:
            raise ValueError("the key record is not valid JSON") from error
        if not isinstance(record, dict):
            raise ValueError("the key record must be an object")
        stored = record.get("publicKey")
        if not isinstance(stored, str):
            raise ValueError('the key record has no "publicKey" field')
        text = stored.strip()
    try:
        raw = base64.b64decode(text, validate=True)
    except (TypeError, ValueError) as error:
        raise ValueError("the public key is not valid base64") from error
    if len(raw) != 32:
        raise ValueError("an ed25519 public key is 32 bytes")
    return raw


def register_trust_commands(app, get_dependencies):
    @app.command("trust-source-add")
    def transfer_trust_source_add(
        actor: str = typer.Option(
            ...,
            "--actor",
            help=(
                "Actor this key is enrolled for; a bundle signed by it lands "
                "only when its sealed manifest claims the same actor."
            ),
        ),
        source_machine: str = typer.Option(
            ...,
            "--source-machine",
            help=(
                "Machine id the source records as its own (its HYPRIAL_NODE_ID or "
                "host name).  A re-registration of the same key must agree on it."
            ),
        ),
        public_key: str = typer.Option(
            ...,
            "--public-key",
            help=(
                "The source machine's Ed25519 public key: a path to its key record "
                "(secrets/transfer/signing-key.json), a file holding just the base64 "
                "key, or the base64 text itself."
            ),
        ),
        key_id: str | None = typer.Option(
            None,
            "--key-id",
            help="Assert the exact key id; refuse when it is not the key you passed.",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Enroll one source machine's signing key (offline and idempotent).

        Enrolling the same key again for the same actor and machine is a no-op;
        the same key id claimed for different material or identity is refused.
        The pair is a binding, not a label: a landing verifies the signature
        under this key *and* refuses a bundle whose sealed manifest claims a
        different actor or source machine than the ones enrolled here.
        """

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type

        from hyprial.daemon import (
            SignatureError,
            SignatureFormatError,
            TrustStoreError,
            add_trusted_source_key,
            public_key_id,
            trust_store_path,
        )

        def operation() -> dict[str, object]:
            home = Path(dependencies.configured_home()[0])
            path = trust_store_path(home)
            try:
                raw = _read_public_key(public_key)
                derived = public_key_id(raw)
                if key_id is not None and key_id != derived:
                    raise ValueError(
                        f"the key you passed is {derived}, not the asserted {key_id}"
                    )
                store = add_trusted_source_key(
                    path,
                    actor=actor,
                    source_machine=source_machine,
                    public_key=raw,
                    added_at=_now(),
                )
            except ValueError as error:
                raise CliError(SignatureFormatError.code, str(error)) from error
            except SignatureError as error:
                raise CliError(error.code, str(error)) from error
            except TrustStoreError as error:
                raise CliError(error.code, str(error)) from error
            key = next(item for item in store.keys if item.key_id == derived)
            return {
                "home": str(home),
                "trustStore": str(path),
                "keyId": key.key_id,
                "actor": key.actor,
                "sourceMachine": key.source_machine,
                "addedAt": key.added_at,
                "keys": len(store.keys),
            }

        _execute(operation, json_output=json_output)

    @app.command("trust-source-list")
    def transfer_trust_source_list(
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """List the source keys this target will accept.

        A missing store is an empty store, never a wildcard: a target that has
        enrolled nobody refuses every signature rather than trusting anybody.
        """

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type

        from hyprial.daemon import (
            TrustStoreError,
            load_trust_store,
            trust_store_path,
        )

        def operation() -> dict[str, object]:
            home = Path(dependencies.configured_home()[0])
            path = trust_store_path(home)
            try:
                store = load_trust_store(path)
            except TrustStoreError as error:
                raise CliError(error.code, str(error)) from error
            return {
                "home": str(home),
                "trustStore": str(path),
                "schemaVersion": store.schema_version,
                "retirementPolicy": store.retirement_policy,
                "keys": [key.as_dict() for key in store.keys],
            }

        _execute(operation, json_output=json_output)

    @app.command("trust-source-retire")
    def transfer_trust_source_retire(
        key_id: str = typer.Option(
            ...,
            "--key-id",
            help="Key id to retire (it is recorded, never deleted: audits need the line).",
        ),
        retired_at: str | None = typer.Option(
            None,
            "--retired-at",
            help="ISO-8601 instant the key retires (default: now, UTC).",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Mark one enrolled key as retired.

        Retirement itself is a record, not a delete.  Whether a bundle signed by
        a retired key still lands is the store's ``retirementPolicy`` (warn by
        default, refuse on demand); that switch lives in the store file and is
        deliberately not a CLI backdoor.
        """

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type

        from hyprial.daemon import (
            TrustStoreError,
            retire_trusted_source_key,
            trust_store_path,
        )

        def operation() -> dict[str, object]:
            home = Path(dependencies.configured_home()[0])
            path = trust_store_path(home)
            try:
                store = retire_trusted_source_key(
                    path, key_id, retired_at=retired_at or _now()
                )
            except TrustStoreError as error:
                raise CliError(error.code, str(error)) from error
            key = next(item for item in store.keys if item.key_id == key_id)
            return {
                "home": str(home),
                "trustStore": str(path),
                "keyId": key.key_id,
                "actor": key.actor,
                "sourceMachine": key.source_machine,
                "retiredAt": key.retired_at,
                "keys": len(store.keys),
            }

        _execute(operation, json_output=json_output)

    return {
        "transfer_trust_source_add": transfer_trust_source_add,
        "transfer_trust_source_list": transfer_trust_source_list,
        "transfer_trust_source_retire": transfer_trust_source_retire,
    }
