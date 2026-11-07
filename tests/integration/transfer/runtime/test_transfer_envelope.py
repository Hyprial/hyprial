"""AT06: sealed credential envelope + credential strategy (red-first mutants)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.envelope import (
    ENVELOPE_ALGORITHM,
    ENVELOPE_SCHEMA_VERSION,
    PROTOTYPE_PROTOCOL,
    CredentialRecord,
    EnvelopeFormatError,
    EnvelopeOpenError,
    EnvelopeStrategyError,
    generate_keypair,
    load_envelope,
    open_envelope,
    resolve_policy,
    seal_envelope,
    write_envelope)

CREATED_AT = "2026-09-30T22:30:00+08:00"


def _seal(credentials=None, **overrides):
    private, public = generate_keypair()
    kwargs = dict(
        recipient_public_key=public,
        actor="codex-sw",
        owner="owner@example.com",
        created_at=CREATED_AT,
    )
    kwargs.update(overrides)
    envelope = seal_envelope(
        credentials or {"openai": "sk-synthetic"}, **kwargs
    )
    return private, public, envelope


def test_seal_then_open_roundtrip(tmp_path: Path) -> None:
    private, _, envelope = _seal()

    assert envelope["schema_version"] == ENVELOPE_SCHEMA_VERSION
    assert envelope["algorithm"] == ENVELOPE_ALGORITHM
    assert envelope["prototype"] is PROTOTYPE_PROTOCOL
    assert "sk-synthetic" not in json.dumps(envelope)

    opened = open_envelope(envelope, recipient_private_key=private)
    assert opened == {"openai": "sk-synthetic"}


def test_envelope_file_roundtrip(tmp_path: Path) -> None:
    private, _, envelope = _seal()
    path = write_envelope(tmp_path / "envelope.json", envelope)

    assert load_envelope(path)["algorithm"] == ENVELOPE_ALGORITHM
    assert open_envelope(
        load_envelope(path), recipient_private_key=private
    ) == {"openai": "sk-synthetic"}


def test_wrong_recipient_key_is_refused() -> None:
    _, _, envelope = _seal()
    other_private, _ = generate_keypair()

    with pytest.raises(EnvelopeOpenError):
        open_envelope(envelope, recipient_private_key=other_private)


def test_tampered_ciphertext_is_refused() -> None:
    private, _, envelope = _seal()
    raw = bytearray(base64.b64decode(envelope["ciphertext"]))
    raw[0] ^= 0x01
    envelope = dict(envelope, ciphertext=base64.b64encode(bytes(raw)).decode())

    with pytest.raises(EnvelopeOpenError):
        open_envelope(envelope, recipient_private_key=private)


def test_changed_header_metadata_is_refused() -> None:
    private, _, envelope = _seal()

    with pytest.raises(EnvelopeOpenError):
        open_envelope(
            dict(envelope, owner="someone-else@example.com"),
            recipient_private_key=private,
        )


def test_unknown_schema_or_algorithm_is_refused() -> None:
    private, _, envelope = _seal()

    with pytest.raises(EnvelopeFormatError):
        open_envelope(
            dict(envelope, schema_version=99), recipient_private_key=private
        )
    with pytest.raises(EnvelopeFormatError):
        open_envelope(
            dict(envelope, algorithm="rot13"), recipient_private_key=private
        )


def test_host_credentials_need_an_explicit_host_owner_grant() -> None:
    default = resolve_policy()
    assert default.strategy == "original-user"
    assert default.host_owner_grant is None

    with pytest.raises(EnvelopeStrategyError) as caught:
        resolve_policy(requested="host")
    assert caught.value.code == "envelope_strategy_refused"

    granted = resolve_policy(requested="host", host_owner_grant="grant-1234")
    assert granted.strategy == "host"
    assert granted.host_owner_grant == "grant-1234"

    with pytest.raises(EnvelopeStrategyError):
        resolve_policy(requested="someone-elses-key")


def test_credential_records_report_machine_bound_by_default() -> None:
    _, _, envelope = _seal(
        records=[
            CredentialRecord(name="openai", machine_bound=False, refreshable=True),
            {"name": "device-token"},
        ]
    )

    listed = {entry["name"]: entry for entry in envelope["credentials"]}
    assert listed["openai"]["machine_bound"] is False
    assert listed["openai"]["refreshable"] is True
    # An unknown credential is reported as machine-bound rather than assumed safe.
    assert listed["device-token"]["machine_bound"] is True

    with pytest.raises(EnvelopeFormatError):
        CredentialRecord.from_dict({})


def test_sealed_policy_is_recorded() -> None:
    _, _, envelope = _seal(policy=resolve_policy(requested="host", host_owner_grant="g-1"))
    assert envelope["policy"]["strategy"] == "host"
    assert envelope["policy"]["host_owner_grant"] == "g-1"


# -- production entry points -------------------------------------------------


def test_cli_seal_requires_the_prototype_acknowledgement(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    credentials = tmp_path / "credentials.json"
    credentials.write_text('{"openai": "sk-synthetic"}\n')
    _, public = generate_keypair()
    runner = CliRunner()

    without = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "credential-seal",
            "--from",
            str(credentials),
            "--to",
            str(tmp_path / "envelope.json"),
            "--recipient-public-key",
            base64.b64encode(public).decode(),
            "--actor",
            "codex-sw",
            "--owner",
            "owner@example.com",
        ],
    )
    assert without.exit_code != 0
    assert not (tmp_path / "envelope.json").exists()

    with_flag = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "credential-seal",
            "--from",
            str(credentials),
            "--to",
            str(tmp_path / "envelope.json"),
            "--recipient-public-key",
            base64.b64encode(public).decode(),
            "--actor",
            "codex-sw",
            "--owner",
            "owner@example.com",
            "--record",
            '{"name": "openai", "machine_bound": false, "refreshable": true}',
            "--prototype",
            "--json",
        ],
    )
    assert with_flag.exit_code == 0, with_flag.output
    assert json.loads(with_flag.stdout)["prototype"] is True


def test_cli_seal_then_open_roundtrip(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    credentials = tmp_path / "credentials.json"
    credentials.write_text('{"openai": "sk-synthetic"}\n')
    private, public = generate_keypair()
    key_file = tmp_path / "target.key"
    key_file.write_bytes(private)
    envelope_path = tmp_path / "envelope.json"
    runner = CliRunner()

    sealed = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "credential-seal",
            "--from",
            str(credentials),
            "--to",
            str(envelope_path),
            "--recipient-public-key",
            base64.b64encode(public).decode(),
            "--actor",
            "codex-sw",
            "--owner",
            "owner@example.com",
            "--prototype",
        ],
    )
    assert sealed.exit_code == 0, sealed.output

    opened = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "credential-open",
            str(envelope_path),
            "--key",
            str(key_file),
            "--prototype",
            "--json",
        ],
    )
    assert opened.exit_code == 0, opened.output
    assert json.loads(opened.stdout)["credentials"] == {"openai": "sk-synthetic"}


def test_cli_seal_refuses_host_credentials_without_a_grant(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    credentials = tmp_path / "credentials.json"
    credentials.write_text('{"openai": "sk-synthetic"}\n')
    _, public = generate_keypair()
    runner = CliRunner()

    refused = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "credential-seal",
            "--from",
            str(credentials),
            "--to",
            str(tmp_path / "envelope.json"),
            "--recipient-public-key",
            base64.b64encode(public).decode(),
            "--actor",
            "codex-sw",
            "--owner",
            "owner@example.com",
            "--use-host-credentials",
            "--prototype",
        ],
    )
    assert refused.exit_code != 0
    assert not (tmp_path / "envelope.json").exists()


def test_credential_metadata_and_policy_are_authenticated() -> None:
    private, _, envelope = _seal(
        records=[{"name": "openai", "requires_reauth": True}],
        policy=resolve_policy(),
    )

    # Flipping the recorded metadata must break the seal, not slip through.
    with pytest.raises(EnvelopeOpenError):
        open_envelope(
            dict(
                envelope,
                credentials=[{"name": "openai", "requires_reauth": False}],
            ),
            recipient_private_key=private,
        )
    with pytest.raises(EnvelopeOpenError):
        open_envelope(
            dict(envelope, policy={"strategy": "host", "host_owner_grant": "forged"}),
            recipient_private_key=private,
        )
    with pytest.raises(EnvelopeOpenError):
        open_envelope(
            dict(envelope, prototype=False), recipient_private_key=private
        )
