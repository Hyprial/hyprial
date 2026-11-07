"""T2 daemon-level: sealed credentials land on the machine that holds the key.

Split out of ``landing/test_transfer_registry_landing.py`` so that file and its
module stay inside the structural limits; the shared fixtures it borrows
(``_source_home``/``_daemon``/``_bundle``/``_sealed``) stay there as the
registry-landing base both files use.
"""
from __future__ import annotations

from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import Sequence

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.identity import SecretSource
from hyprial.daemon.impl.transfer.archive.bundle import export_bundle, load_manifest
from hyprial.daemon.impl.transfer.archive.envelope import (
    CredentialInstall,
    CredentialRecord,
    ENVELOPE_RELPATH,
    ensure_recipient_key,
    envelope_json_bytes,
    generate_keypair,
    plan_credential_seal,
    recipient_key_path,
    seal_credential_envelope,
    seal_envelope,
)
from hyprial.daemon.impl.transfer.landing.credentials import plan_credential_landing
from tests.integration.transfer.landing.test_transfer_registry_landing import (
    ACTOR,
    CREATED_AT,
    OWNER,
    _bundle,
    _daemon,
    _sealed,
    _source_home,
)
from tests.support.transfer.signed import sign_bundle_for_target


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 999),
        ("algorithm", "unsupported-header-format"),
        ("owner", "another-owner"),
        ("actor", "another-agent"),
    ],
)
def test_daemon_refuses_invalid_credential_header_before_any_landing_write(tmp_path, field, value):
    home = _source_home(tmp_path)
    envelope = _sealed([CredentialRecord(name="fake-provider")])
    envelope[field] = value
    (home / "config" / "credential-envelope.json").write_text(json.dumps(envelope))
    bundle_dir = _bundle(tmp_path, home, credential_envelope="config/credential-envelope.json")
    app = _daemon(tmp_path)
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
    assert caught.value.code == "CREDENTIAL_ENVELOPE_INVALID"
    assert not app.agents.exists(ACTOR)
    assert not (tmp_path / "agents" / ACTOR).exists()
    assert not (app.state_dir / "transfer-landing").exists()


# -- T2: sealed credential carry lands on the machine key ------------------

CODEX_AUTH_TEXT = '{"OPENAI_API_KEY": "sk-codex-marker-1f2e"}\n'
ENV_SECRET_TEXT = "sk-deepseek-marker-9a7b"


def _source_credential(home: Path, text: str) -> Path:
    """The one native credential file the carry tests seal and restore."""

    path = home / "secrets" / "native" / "codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sealed_bundle(
    tmp_path: Path,
    home: Path,
    *,
    envelope,
    exclude_paths: Sequence[str] = (),
) -> Path:
    """Export in the production seal shape and sign for the daemon home."""

    bundle_dir = tmp_path / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
        credential_envelope=ENVELOPE_RELPATH,
        exclude_paths=exclude_paths,
        extra_files={ENVELOPE_RELPATH: (envelope_json_bytes(envelope), 0o600)},
    )
    sign_bundle_for_target(
        bundle_dir,
        actor=ACTOR,
        source_machine="laptop",
        target_home=tmp_path,
        credential_envelope=ENVELOPE_RELPATH,
    )
    return bundle_dir


def test_daemon_transfer_land_restores_sealed_credentials_with_the_machine_key(
    tmp_path: Path,
) -> None:
    """T2 e2e: the export seals, this machine opens, installs and binds."""

    home = _source_home(tmp_path)
    auth = _source_credential(home, CODEX_AUTH_TEXT)
    app = _daemon(tmp_path)
    try:
        _private, public = ensure_recipient_key(recipient_key_path(app.hyprial_home))
        base = plan_credential_seal(home)
        plan = replace(
            base,
            records=base.records
            + (
                CredentialRecord(
                    name="deepseek",
                    machine_bound=False,
                    install=CredentialInstall(
                        kind="env",
                        environment=("DEEPSEEK_API_KEY",),
                        field="api_key",
                    ),
                ),
            ),
            secrets={**base.secrets, "deepseek": ENV_SECRET_TEXT},
        )
        envelope = seal_credential_envelope(
            plan,
            actor=ACTOR,
            owner=OWNER,
            created_at=CREATED_AT,
            recipient_public_key=public,
        )
        bundle_dir = _sealed_bundle(
            tmp_path, home, envelope=envelope, exclude_paths=plan.excluded
        )

        result = app.handle("transfer.land", {"bundleDir": str(bundle_dir)})

        assert result["ok"] is True
        assert result["credentialPolicy"] == "original-user"
        by_name = {item["name"]: item for item in result["credentials"]}
        assert by_name["native-codex-auth.json"]["disposition"] == "restored"
        assert by_name["deepseek"]["disposition"] == "restored"

        landed = tmp_path / "agents" / ACTOR
        restored = landed / "secrets" / "native" / "codex" / "auth.json"
        assert restored.read_text("utf-8") == CODEX_AUTH_TEXT
        assert restored.read_text("utf-8") == auth.read_text("utf-8")
        assert stat.S_IMODE(restored.stat().st_mode) == 0o600
        entry = landed / "secrets" / "deepseek"
        assert json.loads(entry.read_text("utf-8")) == {"api_key": ENV_SECRET_TEXT}
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600

        grants = app._agent_session_domains.agent.read_secret_inventory(ACTOR)
        assert len(grants) == 1
        assert grants[0].source is SecretSource.AGENT_PRIVATE
        assert grants[0].entry_id == "deepseek"
        assert grants[0].field_name == "api_key"
        assert grants[0].environment_names == ("DEEPSEEK_API_KEY",)
        assert grants[0].entity_token == app.agents.require(ACTOR).entity_token

        # Neither value ever reached a receipt, and no plaintext rode the
        # transport: the exclusion is what makes "restored" mean "sealed".
        receipt = json.dumps(result)
        assert "sk-codex-marker-1f2e" not in receipt
        assert ENV_SECRET_TEXT not in receipt
        for path in bundle_dir.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                assert b"sk-codex-marker-1f2e" not in raw
                assert ENV_SECRET_TEXT.encode() not in raw
    finally:
        app.agents.close()


def test_daemon_transfer_land_with_another_machines_key_restores_nothing(
    tmp_path: Path,
) -> None:
    home = _source_home(tmp_path)
    _source_credential(home, CODEX_AUTH_TEXT)
    app = _daemon(tmp_path)
    try:
        _sender_private, other_public = generate_keypair()
        plan = plan_credential_seal(home)
        envelope = seal_credential_envelope(
            plan,
            actor=ACTOR,
            owner=OWNER,
            created_at=CREATED_AT,
            recipient_public_key=other_public,
        )
        # A real key on this machine -- just not the one the envelope named.
        ensure_recipient_key(recipient_key_path(app.hyprial_home))
        bundle_dir = _sealed_bundle(
            tmp_path, home, envelope=envelope, exclude_paths=plan.excluded
        )

        result = app.handle("transfer.land", {"bundleDir": str(bundle_dir)})

        assert result["ok"] is True
        by_name = {item["name"]: item for item in result["credentials"]}
        assert by_name["native-codex-auth.json"]["disposition"] == "reauth-required"
        landed = tmp_path / "agents" / ACTOR
        assert landed.is_dir()
        assert not (landed / "secrets" / "native" / "codex" / "auth.json").exists()
        assert app._agent_session_domains.agent.read_secret_inventory(ACTOR) == ()
        assert "sk-codex-marker-1f2e" not in json.dumps(result)
        # The only copy of the value was in the sealed envelope; the degraded
        # landing never resurrects a plaintext one.
        for path in bundle_dir.rglob("*"):
            if path.is_file():
                assert b"sk-codex-marker-1f2e" not in path.read_bytes()
    finally:
        app.agents.close()


def test_a_reserved_env_name_stays_reauth_required(
    tmp_path: Path,
) -> None:
    """A reserved binding the target must reject is not restorable."""

    home = _source_home(tmp_path)
    _sender_private, public = generate_keypair()
    envelope = seal_envelope(
        {"weird": "value-marker"},
        recipient_public_key=public,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
        records=[
            CredentialRecord(
                name="weird",
                install=CredentialInstall(
                    kind="env",
                    environment=("PATH",),
                    field="api_key",
                ),
            )
        ],
    )
    bundle_dir = _sealed_bundle(tmp_path, home, envelope=envelope)

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=_sender_private
    )

    item = plan.credentials[0]
    assert item.disposition == "reauth-required"
    assert "not ones this target can bind" in item.notes
    assert plan.payload == {}
