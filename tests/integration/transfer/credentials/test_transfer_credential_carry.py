"""AT06 carry, export side: seal the credentials instead of shipping them.

The export-side half of the credential journey.  ``plan_credential_seal``
finds exactly the files the home layout names credential-bearing; the sealed
envelope rides the payload as an ordinary registered entry while the plaintext
files are excluded.  The two assertions that matter here are "none of the
values appears anywhere in the bundle" and "the declared envelope still
validates like any other payload entry".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import (
    BundlePathError,
    export_bundle,
    validate_bundle,
)
from hyprial.daemon.impl.transfer.archive.envelope import (
    ENVELOPE_RELPATH,
    KNOWN_NATIVE_CREDENTIAL_PATHS,
    EnvCredential,
    envelope_json_bytes,
    env_credential_name,
    plan_credential_seal,
    seal_credential_envelope,
)
from hyprial.daemon.impl.transfer.archive.envelope import generate_keypair

OWNER = "owner@example.com"
ACTOR = "wk"
CREATED_AT = "2026-10-14T12:00:00+08:00"

CODEX_AUTH = '{"OPENAI_API_KEY": "sk-codex-marker-3f9d"}\n'
CLAUDE_CREDENTIALS = '{"token": "claude-marker-aa11"}\n'
PI_AUTH = "pi-marker-77c2\n"
PI_STORE = '{"entries": "pi-store-marker-51b8"}\n'


def _home(tmp_path: Path) -> Path:
    """A home carrying one credential per harness, plus plain siblings."""

    home = tmp_path / "home"
    for harness, files in {
        "claude": {".credentials.json": CLAUDE_CREDENTIALS},
        "codex": {"auth.json": CODEX_AUTH, "settings.json": '{"theme": "dark"}\n'},
        "pi": {"auth.json": PI_AUTH, "models.json": PI_STORE},
    }.items():
        root = home / "secrets" / "native" / harness
        root.mkdir(parents=True)
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
    (home / "config").mkdir()
    (home / "config" / "agent.json").write_text('{"model": "x"}\n', encoding="utf-8")
    return home


def test_the_plan_seals_the_named_credentials_and_excludes_their_plaintext(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    plan = plan_credential_seal(home)

    assert [record.name for record in plan.records] == [
        "native-claude-credentials.json",
        "native-codex-auth.json",
        "native-pi-auth.json",
        "native-pi-models.json",
    ]
    assert all(record.install is not None for record in plan.records)
    assert {
        (record.install.harness, record.install.path) for record in plan.records
    } == {
        ("claude", ".credentials.json"),
        ("codex", "auth.json"),
        ("pi", "auth.json"),
        ("pi", "models.json"),
    }
    assert all(record.machine_bound is False for record in plan.records)
    assert all(record.requires_reauth is False for record in plan.records)
    assert plan.secrets == {
        "native-claude-credentials.json": CLAUDE_CREDENTIALS,
        "native-codex-auth.json": CODEX_AUTH,
        "native-pi-auth.json": PI_AUTH,
        "native-pi-models.json": PI_STORE,
    }
    assert plan.excluded == (
        "secrets/native/claude/.credentials.json",
        "secrets/native/codex/auth.json",
        "secrets/native/pi/auth.json",
        "secrets/native/pi/models.json",
    )
    # The non-credential sibling is neither sealed nor excluded: it rides the
    # payload like every other file.
    assert "secrets/native/codex/settings.json" not in plan.excluded


def test_a_credential_that_is_not_text_is_excluded_and_marked_for_reauth(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    binary = home / "secrets" / "native" / "codex" / "auth.json"
    binary.write_bytes(b"\x00\x01not utf-8\xff")

    plan = plan_credential_seal(home)

    by_name = {record.name: record for record in plan.records}
    record = by_name["native-codex-auth.json"]
    assert record.requires_reauth is True
    assert record.install is None
    assert "re-authenticate" in record.notes
    assert "native-codex-auth.json" not in plan.secrets
    # The bytes still must not ride the bundle in the clear: excluded either
    # way, because an unsealed copy would defeat the whole envelope.
    assert "secrets/native/codex/auth.json" in plan.excluded


def _env_credential(**overrides) -> EnvCredential:
    body = {
        "harness": "claude",
        "entry_id": "anthropic-auth",
        "environment": ("ANTHROPIC_AUTH_TOKEN",),
        "value": "dummy-token-marker-42",
    }
    body.update(overrides)
    return EnvCredential(**body)


def test_an_env_credential_becomes_an_env_install_record(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    plan = plan_credential_seal(home, env_credentials=(_env_credential(),))

    assert [record.name for record in plan.records] == ["env-claude-anthropic-auth"]
    record = plan.records[0]
    assert record.install is not None
    assert record.install.kind == "env"
    assert record.install.environment == ("ANTHROPIC_AUTH_TOKEN",)
    assert record.machine_bound is False
    assert record.requires_reauth is False
    assert plan.secrets == {"env-claude-anthropic-auth": "dummy-token-marker-42"}
    # An env credential has no plaintext file in the home to exclude unless the
    # daemon named one; a user-provider entry lives outside the home entirely.
    assert plan.excluded == ()


def test_an_unresolved_env_credential_degrades_and_is_not_dropped(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    credential = _env_credential(
        value=None,
        requires_reauth=True,
        exclude="secrets/anthropic-auth",
        notes="not resolvable through this daemon's gate (missing)",
    )

    plan = plan_credential_seal(home, env_credentials=(credential,))

    record = plan.records[0]
    assert record.name == "env-claude-anthropic-auth"
    assert record.requires_reauth is True
    assert record.install is None
    assert "env-claude-anthropic-auth" not in plan.secrets
    # The plaintext (when the grant is agent-private) is still held back.
    assert plan.excluded == ("secrets/anthropic-auth",)
    assert plan.secrets == {}


def test_a_sealed_native_file_wins_over_an_env_credential(tmp_path: Path) -> None:
    home = _home(tmp_path)

    plan = plan_credential_seal(home, env_credentials=(_env_credential(),))

    # The claude native credential was sealed, so no env auth record is added:
    # Claude refuses a native credential and an explicit auth env together.
    assert all("env-claude" not in record.name for record in plan.records)
    assert plan.secrets.get("native-claude-credentials.json") == CLAUDE_CREDENTIALS


def test_the_env_credential_wire_form_round_trips_and_is_validated() -> None:
    credential = _env_credential(field_name="token", exclude="secrets/entry")
    assert EnvCredential.from_dict(credential.as_dict()) == credential
    # ``as_dict`` carries the value: it is the envelope's input, not a receipt.
    assert credential.as_dict()["value"] == "dummy-token-marker-42"
    assert "dummy-token-marker-42" not in repr(credential)
    assert env_credential_name("claude", "anthropic-auth") == (
        "env-claude-anthropic-auth"
    )

    for bad in (
        {"harness": "Claude", "entryId": "x", "environment": ["A"]},
        {"harness": "claude", "entryId": "../escape", "environment": ["A"]},
        {"harness": "claude", "entryId": "x", "environment": []},
        {"harness": "claude", "entryId": "x", "environment": ["lower"]},
        {"harness": "claude", "entryId": "x", "environment": ["A"], "value": ""},
    ):
        with pytest.raises(Exception):
            EnvCredential.from_dict(bad)


def test_the_seal_table_matches_the_home_projection_table() -> None:
    from hyprial.identity.impl.agents.home import config as home_config

    assert KNOWN_NATIVE_CREDENTIAL_PATHS == home_config._KNOWN_NATIVE_CREDENTIAL_PATHS


def test_an_export_carries_the_envelope_and_no_plaintext(tmp_path: Path) -> None:
    home = _home(tmp_path)
    plan = plan_credential_seal(home)
    _private, public = generate_keypair()
    envelope = seal_credential_envelope(
        plan,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
        recipient_public_key=public,
    )
    bundle_dir = tmp_path / "bundle"

    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
        credential_envelope=ENVELOPE_RELPATH,
        exclude_paths=plan.excluded,
        extra_files={ENVELOPE_RELPATH: (envelope_json_bytes(envelope), 0o600)},
    )

    manifest = validate_bundle(bundle_dir)
    assert manifest.credential_envelope == ENVELOPE_RELPATH
    payload = bundle_dir / "payload"
    carried = payload / ENVELOPE_RELPATH
    assert carried.read_bytes() == envelope_json_bytes(envelope)
    assert (carried.stat().st_mode & 0o777) == 0o600
    # The envelope is a first-class payload entry, not a sidecar.
    entry = {item.path: item for item in manifest.entries}[ENVELOPE_RELPATH]
    assert entry.sha256 == hashlib.sha256(envelope_json_bytes(envelope)).hexdigest()
    # The credential files are absent; their plain sibling stays.
    assert not (payload / "secrets" / "native" / "codex" / "auth.json").exists()
    assert (payload / "secrets" / "native" / "codex" / "settings.json").is_file()
    # And no file in the whole bundle carries any sealed value in the clear.
    markers = (b"sk-codex-marker-3f9d", b"claude-marker-aa11", b"pi-marker-77c2", b"pi-store-marker-51b8")
    for path in bundle_dir.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            for marker in markers:
                assert marker not in raw, f"{marker!r} leaked into {path}"
    assert json.loads(carried.read_text("utf-8"))["ciphertext"]


def test_an_extra_that_collides_with_an_exclusion_or_entry_is_refused(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    plan = plan_credential_seal(home)
    _private, public = generate_keypair()
    envelope = seal_credential_envelope(
        plan,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
        recipient_public_key=public,
    )
    _write = (envelope_json_bytes(envelope), 0o600)

    # An extra naming an excluded path would smuggle a second copy past the
    # exclusion; an extra colliding with a copied entry would fork the record
    # of a file.  Both are refused, and no half-written bundle survives.
    first = tmp_path / "bundle-excluded"
    with pytest.raises(BundlePathError):
        export_bundle(
            home,
            first,
            owner=OWNER,
            actor=ACTOR,
            source_machine="laptop",
            created_at=CREATED_AT,
            credential_envelope=ENVELOPE_RELPATH,
            exclude_paths=plan.excluded,
            extra_files={
                "secrets/native/codex/auth.json": _write,
                ENVELOPE_RELPATH: _write,
            },
        )
    assert not first.exists()

    second = tmp_path / "bundle-collision"
    with pytest.raises(BundlePathError):
        export_bundle(
            home,
            second,
            owner=OWNER,
            actor=ACTOR,
            source_machine="laptop",
            created_at=CREATED_AT,
            credential_envelope=ENVELOPE_RELPATH,
            exclude_paths=plan.excluded,
            extra_files={
                "config/agent.json": _write,
                ENVELOPE_RELPATH: _write,
            },
        )
    assert not second.exists()
