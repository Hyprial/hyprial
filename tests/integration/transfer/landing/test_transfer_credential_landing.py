"""AT07 item 2, first phase: credentials on landing never become a host-key fallback.

Decision (W1D-1, 2026-11-21): ``transfer/landing/owner.py`` (``land_agent_home``)
was retired as an unreferenced dead layer; these first-phase header and
no-recipient-key landing invariants now run directly through the live
``land_bundle_into_registry`` and ``plan_credential_landing`` entry points.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle, load_manifest
from hyprial.daemon.impl.transfer.archive.envelope import (
    CredentialPolicy,
    CredentialRecord,
    generate_keypair,
    seal_envelope,
)
from hyprial.daemon.impl.transfer.landing.credentials import (
    CredentialEnvelopeInvalid,
    CredentialEnvelopeMissing,
    CredentialHostKeyRefused,
    plan_credential_landing,
)
from hyprial.daemon.impl.transfer.landing.registry import (
    CUSTODY_RECEIPT,
    land_bundle_into_registry,
)
from hyprial.identity.impl.agents.registry._core import AgentRegistry

ACTOR = "codex-sw"
OWNER = "owner@example.com"
MACHINE = "target-node"
CREATED_AT = "2026-10-01T01:40:00+08:00"
ENVELOPE_PATH = "config/credential-envelope.json"
MARKER = "ghp_marker_must_not_leak"


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
    for dirname in ("state", "config", "secrets"):
        (home / dirname).mkdir(parents=True, exist_ok=True)
    (home / CUSTODY_RECEIPT).write_text(
        json.dumps({"actor": ACTOR, "path": "/src/agents/codex-sw", "resource": "r1"})
    )
    (home / "state" / "sessions.json").write_text('{"session": 1}\n')
    (home / "config" / "agent.json").write_text('{"model": "x"}\n')
    declared = ""
    if envelope is not None:
        target = home / ENVELOPE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
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


def _registry(tmp_path: Path) -> AgentRegistry:
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700, exist_ok=True)
    return AgentRegistry(
        tmp_path / "agents.sqlite3",
        owner=OWNER,
        machine=MACHINE,
        hyprial_home=target_home,
    )


def _land(tmp_path: Path, bundle_dir: Path, registry: AgentRegistry, *, dry_run: bool = False):
    return land_bundle_into_registry(
        bundle_dir,
        registry=registry,
        home_root=tmp_path / "target-home" / "agents",
        machine_home=None,
        staging=tmp_path / "staging" / "landing-1",
        granted_by=f"user:{OWNER}",
        dry_run=dry_run,
    )


def test_a_carried_credential_is_reported_as_reauth_not_restored(
    tmp_path: Path,
) -> None:
    """Sealed plaintext stays unopened without a recipient key and never leaks."""
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
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, bundle_dir, registry)

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
        assert MARKER not in json.dumps(payload)
    finally:
        registry.close()


def test_landing_never_opens_the_envelope(tmp_path: Path) -> None:
    envelope = _sealed([CredentialRecord(name="github")])
    envelope["ciphertext"] = base64.b64encode(b"not the real plaintext").decode()
    bundle_dir = _bundle(tmp_path, envelope=envelope)
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, bundle_dir, registry)

        assert result.needs_reauth == ("github",)
        assert result.credential_policy == "original-user"
    finally:
        registry.close()


def test_landing_refuses_a_host_key_policy_without_the_owner_grant(
    tmp_path: Path,
) -> None:
    """Refusal happens before anything is written or adopted."""
    envelope = _sealed(
        [CredentialRecord(name="github")],
        policy=CredentialPolicy(strategy="host"),
    )
    bundle_dir = _bundle(tmp_path, envelope=envelope)
    registry = _registry(tmp_path)
    try:
        with pytest.raises(CredentialHostKeyRefused) as caught:
            _land(tmp_path, bundle_dir, registry)

        assert caught.value.code == "credential_host_key_refused"
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "target-home" / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging").exists()
    finally:
        registry.close()


def test_landing_accepts_a_host_key_policy_the_host_owner_granted(
    tmp_path: Path,
) -> None:
    envelope = _sealed(
        [CredentialRecord(name="github")],
        policy=CredentialPolicy(strategy="host", host_owner_grant="grant-9"),
    )
    bundle_dir = _bundle(tmp_path, envelope=envelope)
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, bundle_dir, registry)

        assert result.credential_policy == "host"
        assert result.needs_reauth == ("github",)
    finally:
        registry.close()


def test_a_bundle_without_a_declared_envelope_reports_no_credentials(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, _bundle(tmp_path), registry)

        payload = result.as_dict()
        assert payload["credentialPolicy"] == "original-user"
        assert payload["credentials"] == []
        assert result.needs_reauth == ()
    finally:
        registry.close()


def test_a_dry_run_reports_the_credentials_without_touching_the_daemon(
    tmp_path: Path,
) -> None:
    """A dry run inspects headers without adopting, granting, or writing."""
    bundle_dir = _bundle(tmp_path, envelope=_sealed([CredentialRecord(name="github")]))
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, bundle_dir, registry, dry_run=True)

        assert result.dry_run is True
        assert result.needs_reauth == ("github",)
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "target-home" / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging").exists()
    finally:
        registry.close()


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
    envelope = _sealed([CredentialRecord(name="fake-provider")])
    envelope[field] = value
    bundle_dir = _bundle(tmp_path, envelope=envelope)
    registry = _registry(tmp_path)
    try:
        with pytest.raises(CredentialEnvelopeInvalid):
            _land(tmp_path, bundle_dir, registry)
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "target-home" / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging").exists()
    finally:
        registry.close()
