"""AT07 item 2, second phase: a key this machine holds installs what it opens."""

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle, load_manifest
from hyprial.daemon.impl.transfer.archive.envelope import (
    CredentialInstall,
    CredentialPolicy,
    CredentialRecord,
    EnvelopeFormatError,
    RecipientKeyError,
    ensure_recipient_key,
    generate_keypair,
    read_recipient_key,
    recipient_key_path,
    recipient_public_key_b64,
    seal_envelope,
    write_recipient_key,
)
from hyprial.daemon.impl.transfer.landing.credentials import (
    CREDENTIAL_REAUTH_REQUIRED,
    CREDENTIAL_RESTORABLE,
    CREDENTIAL_RESTORED,
    CredentialBinding,
    CredentialEnvelopeInvalid,
    apply_credential_landing,
    plan_credential_landing,
)

ACTOR = "codex-sw"
OWNER = "owner@example.com"
CREATED_AT = "2026-10-14T10:00:00+08:00"
ENVELOPE_PATH = "credentials/envelope.json"
CODEX_AUTH = '{"OPENAI_API_KEY": "sk-marker-value"}'
DEEPSEEK = "sk-deepseek-marker"
MARKER = "sk-marker-value"


def _sealed(
    records: Sequence[CredentialRecord],
    *,
    recipient_public_key: bytes,
    secrets: Mapping[str, object],
    policy: CredentialPolicy | None = None,
) -> dict[str, object]:
    return seal_envelope(
        dict(secrets),
        recipient_public_key=recipient_public_key,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
        records=records,
        policy=policy,
    )


def _bundle(root: Path, envelope: Mapping[str, object]) -> Path:
    home = root / "src" / "agents" / ACTOR
    (home / "sessions").mkdir(parents=True)
    (home / "notes.md").write_text("payload\n")
    target = home / ENVELOPE_PATH
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(envelope, sort_keys=True) + "\n")
    bundle_dir = root / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="devusr-dev",
        created_at=CREATED_AT,
        identity={"entityToken": "source-token"},
        credential_envelope=ENVELOPE_PATH,
    )
    return bundle_dir


def test_the_recipient_key_is_minted_once_at_0600(tmp_path: Path) -> None:
    home = tmp_path / "machine"
    path = recipient_key_path(home)

    private, public = ensure_recipient_key(path)

    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert ensure_recipient_key(path) == (private, public)
    assert read_recipient_key(path) == (private, public)
    assert recipient_public_key_b64(public)


def test_a_recipient_key_with_mismatched_halves_is_refused(tmp_path: Path) -> None:
    private, _public = generate_keypair()
    _other_private, other_public = generate_keypair()
    path = recipient_key_path(tmp_path / "machine")
    write_recipient_key(path, private, other_public)

    with pytest.raises(RecipientKeyError):
        read_recipient_key(path)


def _installable_bundle(
    root: Path, *, policy: CredentialPolicy | None = None
) -> tuple[Path, bytes]:
    private, public = generate_keypair()
    envelope = _sealed(
        [
            CredentialRecord(
                name="codex-auth",
                machine_bound=False,
                install=CredentialInstall(
                    kind="native-file", harness="codex", path="auth.json"
                ),
            ),
            CredentialRecord(
                name="deepseek",
                machine_bound=False,
                install=CredentialInstall(
                    kind="env",
                    environment=("DEEPSEEK_API_KEY",),
                    field="api_key",
                ),
            ),
            CredentialRecord(name="github"),
        ],
        recipient_public_key=public,
        secrets={"codex-auth": CODEX_AUTH, "deepseek": DEEPSEEK, "github": "ghp-x"},
        policy=policy,
    )
    return _bundle(root, envelope), private


def test_a_sealed_credential_with_an_install_target_becomes_restorable(
    tmp_path: Path,
) -> None:
    bundle_dir, private = _installable_bundle(tmp_path)

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )

    by_name = {item.name: item for item in plan.credentials}
    assert by_name["codex-auth"].disposition == CREDENTIAL_RESTORABLE
    assert by_name["deepseek"].disposition == CREDENTIAL_RESTORABLE
    assert by_name["github"].disposition == CREDENTIAL_REAUTH_REQUIRED
    assert "machine-bound" in by_name["github"].notes
    assert plan.needs_reauth == ("github",)
    assert plan.payload == {"codex-auth": CODEX_AUTH, "deepseek": DEEPSEEK}
    # The receipt never carries a value, only dispositions and install targets.
    receipt = json.dumps([item.as_dict() for item in plan.credentials])
    assert MARKER not in receipt
    assert DEEPSEEK not in receipt


def test_apply_writes_the_native_file_and_the_env_entry(tmp_path: Path) -> None:
    bundle_dir, private = _installable_bundle(tmp_path)
    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )
    home = tmp_path / "landed"

    result = apply_credential_landing(home, plan)

    auth = home / "secrets" / "native" / "codex" / "auth.json"
    assert auth.read_text("utf-8") == CODEX_AUTH
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    entry = home / "secrets" / "deepseek"
    assert json.loads(entry.read_text("utf-8")) == {"api_key": DEEPSEEK}
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "secrets").stat().st_mode) == 0o700

    by_name = {item.name: item for item in result.plan.credentials}
    assert by_name["codex-auth"].disposition == CREDENTIAL_RESTORED
    assert by_name["deepseek"].disposition == CREDENTIAL_RESTORED
    assert by_name["github"].disposition == CREDENTIAL_REAUTH_REQUIRED
    assert result.bindings == (
        CredentialBinding(
            entry_id="deepseek",
            environment=("DEEPSEEK_API_KEY",),
            field="api_key",
        ),
    )
    assert set(result.written) == {auth, entry}


def test_a_first_phase_plan_installs_nothing(tmp_path: Path) -> None:
    bundle_dir, _private = _installable_bundle(tmp_path)
    plan = plan_credential_landing(load_manifest(bundle_dir), bundle_dir)
    home = tmp_path / "landed"

    result = apply_credential_landing(home, plan)

    assert result.written == ()
    assert result.bindings == ()
    assert result.plan.credentials == plan.credentials
    assert not home.exists()


def test_a_wrong_recipient_key_degrades_to_reauth_required(tmp_path: Path) -> None:
    bundle_dir, _private = _installable_bundle(tmp_path)
    other_private, _other_public = generate_keypair()

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=other_private
    )

    assert plan.needs_reauth == ("codex-auth", "deepseek", "github")
    assert plan.payload == {}
    assert all("could not be opened" in item.notes for item in plan.credentials)


def test_tampered_ciphertext_degrades_instead_of_crashing(tmp_path: Path) -> None:
    bundle_dir, private = _installable_bundle(tmp_path)
    path = bundle_dir / "payload" / ENVELOPE_PATH
    envelope = json.loads(path.read_text("utf-8"))
    envelope["ciphertext"] = base64.b64encode(b"not the real plaintext").decode()
    path.write_text(json.dumps(envelope, sort_keys=True) + "\n")

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )

    assert plan.needs_reauth == ("codex-auth", "deepseek", "github")
    assert plan.payload == {}


def test_a_granted_host_policy_installs_nothing_even_with_the_key(
    tmp_path: Path,
) -> None:
    bundle_dir, private = _installable_bundle(
        tmp_path,
        policy=CredentialPolicy(strategy="host", host_owner_grant="grant-9"),
    )

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )

    assert plan.policy.strategy == "host"
    assert plan.payload == {}
    assert plan.needs_reauth == ("codex-auth", "deepseek", "github")


def test_duplicate_credential_names_are_refused(tmp_path: Path) -> None:
    _private, public = generate_keypair()
    envelope = _sealed(
        [CredentialRecord(name="github"), CredentialRecord(name="github")],
        recipient_public_key=public,
        secrets={"github": "ghp-x"},
    )
    bundle_dir = _bundle(tmp_path, envelope)

    with pytest.raises(CredentialEnvelopeInvalid):
        plan_credential_landing(load_manifest(bundle_dir), bundle_dir)


def test_install_metadata_round_trips_and_is_validated() -> None:
    native = CredentialInstall(kind="native-file", harness="codex", path="auth.json")
    assert CredentialInstall.from_dict(native.as_dict()) == native
    env = CredentialInstall(
        kind="env", environment=("DEEPSEEK_API_KEY",), field="api_key"
    )
    assert CredentialInstall.from_dict(env.as_dict()) == env

    with pytest.raises(EnvelopeFormatError):
        CredentialInstall.from_dict(
            {"kind": "native-file", "harness": "codex", "path": "../escape"}
        )
    with pytest.raises(EnvelopeFormatError):
        CredentialInstall.from_dict({"kind": "env", "environment": ["lowercase"]})
    with pytest.raises(EnvelopeFormatError):
        CredentialInstall.from_dict({"kind": "unknown"})
    with pytest.raises(EnvelopeFormatError):
        CredentialRecord.from_dict(
            {
                "name": "bad/name",
                "install": {"kind": "env", "environment": ["DEEPSEEK_API_KEY"]},
            }
        )
