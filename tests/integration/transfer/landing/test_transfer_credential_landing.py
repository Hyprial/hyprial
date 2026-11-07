"""AT07 item 2, first phase: credentials on landing never become a host-key fallback."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle, load_manifest
from hyprial.daemon.impl.transfer.landing.credentials import (
    CredentialEnvelopeMissing,
    CredentialHostKeyRefused,
    plan_credential_landing)
from hyprial.daemon.impl.transfer.archive.envelope import (
    CredentialPolicy,
    CredentialRecord,
    generate_keypair,
    seal_envelope)
from hyprial.daemon.impl.transfer.landing.owner import land_agent_home
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL

ACTOR = "codex-sw"
OWNER = "owner@example.com"
CREATED_AT = "2026-10-01T01:40:00+08:00"
ENVELOPE_PATH = "credentials/envelope.json"
MARKER = "ghp_marker_must_not_leak"


class FakeAuthority:
    """Stands in for the daemon registry, recording every call it receives."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rows: dict[str, dict[str, object]] = {}

    def agent_exists(self, actor: str) -> bool:
        self.calls.append(f"exists:{actor}")
        return actor in self.rows

    def adopt_agent(
        self, actor: str, *, owner: str, home: Path, source_machine: str
    ) -> None:
        self.calls.append(f"adopt:{actor}")
        self.rows[actor] = {"owner": owner, "home": str(home)}

    def drop_agent(self, actor: str) -> None:
        self.calls.append(f"drop:{actor}")
        self.rows.pop(actor, None)

    def materialise_grant(self, actor: str, grant: str) -> str | None:
        self.calls.append(f"grant:{grant}")
        return f"grant-id:{grant}"

    def revoke_grants(self, actor: str, grant_ids: Sequence[str]) -> None:
        self.calls.append("revoke:" + ",".join(grant_ids))


def _sealed(
    records: Sequence[CredentialRecord],
    *,
    secrets: Mapping[str, object] | None = None,
    policy: CredentialPolicy | None = None,
) -> dict[str, object]:
    _sender_private, recipient_public = generate_keypair()
    return seal_envelope(
        dict(secrets or {}),
        recipient_public_key=recipient_public,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
        records=records,
        policy=policy,
    )


def _bundle(
    root: Path, *, envelope: Mapping[str, object] | None = None, name: str = "bundle"
) -> Path:
    home = root / "src" / "agents" / ACTOR
    (home / "sessions").mkdir(parents=True)
    (home / "notes.md").write_text("payload\n")
    declared = ""
    if envelope is not None:
        target = home / ENVELOPE_PATH
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(envelope, sort_keys=True) + "\n")
        declared = ENVELOPE_PATH
    bundle_dir = root / name
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="devusr-dev",
        created_at=CREATED_AT,
        identity={"entityToken": "source-token"},
        credential_envelope=declared,
    )
    return bundle_dir


def test_a_carried_credential_is_reported_as_reauth_not_restored(
    tmp_path: Path,
) -> None:
    envelope = _sealed(
        [
            CredentialRecord(name="github", machine_bound=True),
            CredentialRecord(
                name="lark",
                machine_bound=False,
                requires_reauth=True,
                refreshable=True,
            ),
        ],
        secrets={"github": MARKER},
    )
    bundle_dir = _bundle(tmp_path, envelope=envelope)

    result = land_agent_home(
        bundle_dir,
        tmp_path / "dest",
        authority=FakeAuthority(),
        signature=UNSIGNED_REHEARSAL,
    )

    payload = result.as_dict()
    assert payload["credentialPolicy"] == "original-user"
    by_name = {item["name"]: item for item in payload["credentials"]}
    assert by_name["github"] == {
        "name": "github",
        "disposition": "reauth-required",
        "machineBound": True,
        "requiresReauth": False,
        "refreshable": False,
        "notes": "",
    }
    assert by_name["lark"]["requiresReauth"] is True
    assert by_name["lark"]["refreshable"] is True
    assert result.needs_reauth == ("github", "lark")
    # The sealed plaintext is never opened, so its marker cannot reach the receipt.
    assert MARKER not in json.dumps(payload)


def test_landing_never_opens_the_envelope(tmp_path: Path) -> None:
    envelope = _sealed([CredentialRecord(name="github")])
    envelope["ciphertext"] = base64.b64encode(b"not the real plaintext").decode()

    bundle_dir = _bundle(tmp_path, envelope=envelope)

    result = land_agent_home(
        bundle_dir,
        tmp_path / "dest",
        authority=FakeAuthority(),
        signature=UNSIGNED_REHEARSAL,
    )

    assert result.needs_reauth == ("github",)
    assert result.credential_policy == "original-user"


def test_landing_refuses_a_host_key_policy_without_the_owner_grant(
    tmp_path: Path,
) -> None:
    envelope = _sealed(
        [CredentialRecord(name="github")],
        policy=CredentialPolicy(strategy="host"),
    )
    bundle_dir = _bundle(tmp_path, envelope=envelope)
    authority = FakeAuthority()
    destination = tmp_path / "dest"

    with pytest.raises(CredentialHostKeyRefused) as caught:
        land_agent_home(
            bundle_dir,
            destination,
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    assert caught.value.code == "credential_host_key_refused"
    # Refusal happens before anything is written or adopted.
    assert authority.calls == []
    assert not destination.exists()


def test_landing_accepts_a_host_key_policy_the_host_owner_granted(
    tmp_path: Path,
) -> None:
    envelope = _sealed(
        [CredentialRecord(name="github")],
        policy=CredentialPolicy(strategy="host", host_owner_grant="grant-9"),
    )
    bundle_dir = _bundle(tmp_path, envelope=envelope)

    result = land_agent_home(
        bundle_dir,
        tmp_path / "dest",
        authority=FakeAuthority(),
        signature=UNSIGNED_REHEARSAL,
    )

    assert result.credential_policy == "host"
    assert result.needs_reauth == ("github",)


def test_a_bundle_without_a_declared_envelope_reports_no_credentials(
    tmp_path: Path,
) -> None:
    result = land_agent_home(
        _bundle(tmp_path),
        tmp_path / "dest",
        authority=FakeAuthority(),
        signature=UNSIGNED_REHEARSAL,
    )

    payload = result.as_dict()
    assert payload["credentialPolicy"] == "original-user"
    assert payload["credentials"] == []
    assert result.needs_reauth == ()


def test_a_dry_run_reports_the_credentials_without_touching_the_daemon(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path, envelope=_sealed([CredentialRecord(name="github")]))
    authority = FakeAuthority()

    result = land_agent_home(
        bundle_dir,
        tmp_path / "dest",
        authority=authority,
        dry_run=True,
        signature=UNSIGNED_REHEARSAL,
    )

    assert result.dry_run is True
    assert result.needs_reauth == ("github",)
    # A dry run may look, but it never adopts, grants or writes.
    assert authority.calls == ["exists:codex-sw"]
    assert authority.rows == {}
    assert not (tmp_path / "dest").exists()


def test_a_declared_envelope_the_bundle_lacks_is_refused(tmp_path: Path) -> None:
    manifest = replace(
        load_manifest(_bundle(tmp_path)), credential_envelope=ENVELOPE_PATH
    )

    with pytest.raises(CredentialEnvelopeMissing) as caught:
        plan_credential_landing(manifest, tmp_path / "bundle")

    assert caught.value.code == "credential_envelope_missing"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 999),
        ("algorithm", "unsupported-header-format"),
        ("owner", "another-owner"),
        ("actor", "another-agent"),
    ],
)
def test_invalid_or_inconsistent_header_refuses_before_landing(tmp_path, field, value):
    from hyprial.daemon.impl.transfer.landing.credentials import CredentialEnvelopeInvalid

    envelope = _sealed([CredentialRecord(name="fake-provider")])
    envelope[field] = value
    bundle_dir = _bundle(tmp_path, envelope=envelope)
    authority = FakeAuthority()
    with pytest.raises(CredentialEnvelopeInvalid):
        land_agent_home(
            bundle_dir,
            tmp_path / "dest",
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )
    assert authority.calls == []
    assert not (tmp_path / "dest").exists()
