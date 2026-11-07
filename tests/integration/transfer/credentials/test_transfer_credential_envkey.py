"""C3 env-key export side: the producer's degradation arms.

The positive full chain lives in ``p2/e2e/test_claude_envkey_chain.py``.  This
file pins the two ways the producer must *not* claim a restore: a reserved
environment name the target's registry cannot bind, and a value the daemon gate
could not resolve. Both must land as ``reauth-required`` with no plaintext anywhere.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import hyprial.identity as identity_api
from hyprial.identity.impl.agents.runtime.secrets import (
    SecretEnvironmentNameRejected,
    SecretGrant,
    SecretSource,
)

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle, load_manifest
from hyprial.daemon.impl.transfer.archive.env_credentials import (
    resolve_env_credentials,
)
from hyprial.daemon.impl.transfer.archive.envelope import (
    ENVELOPE_RELPATH,
    EnvCredential,
    envelope_json_bytes,
    generate_keypair,
    plan_credential_seal,
    seal_credential_envelope,
)
from hyprial.daemon.impl.transfer.landing.credentials import (
    CREDENTIAL_REAUTH_REQUIRED,
    CREDENTIAL_RESTORABLE,
    plan_credential_landing,
)

OWNER = "owner@example.com"
ACTOR = "claude-env"
CREATED_AT = "2026-10-16T00:00:00+08:00"
DUMMY = "sk-dummy-unapproved-marker-77"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "agents" / ACTOR
    (home / "config" / "native" / "claude").mkdir(parents=True)
    (home / "config" / "native" / "claude" / "settings.json").write_text("{}\n")
    return home


def _landing_plan(
    tmp_path: Path,
    credentials: tuple[EnvCredential, ...],
):
    home = _home(tmp_path)
    plan = plan_credential_seal(home, env_credentials=credentials)
    private, public = generate_keypair()
    envelope = seal_credential_envelope(
        plan,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
        recipient_public_key=public,
    )
    bundle = tmp_path / "bundle"
    export_bundle(
        home,
        bundle,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
        credential_envelope=ENVELOPE_RELPATH,
        exclude_paths=plan.excluded,
        extra_files={ENVELOPE_RELPATH: (envelope_json_bytes(envelope), 0o600)},
    )
    landed = plan_credential_landing(
        load_manifest(bundle), bundle, recipient_key=private
    )
    return plan, landed


@pytest.mark.parametrize(
    ("environment_name", "expected_allowed"),
    (("FORGEJO_ACCESS_TOKEN", True), ("PATH", False)),
)
def test_identity_and_transfer_share_one_environment_name_verdict(
    tmp_path: Path,
    environment_name: str,
    expected_allowed: bool,
) -> None:
    """A grant field gets the same allow/deny decision during landing."""

    try:
        SecretGrant(
            actor=ACTOR,
            entity_token="incarnation-token",
            grant_id="paired-verdict",
            source=SecretSource.AGENT_PRIVATE,
            entry_id="paired-verdict",
            field_name="token",
            environment_names=(environment_name,),
            revision=1,
        )
    except SecretEnvironmentNameRejected:
        identity_allowed = False
    else:
        identity_allowed = True

    credential = EnvCredential(
        harness="claude",
        entry_id="paired-verdict",
        environment=(environment_name,),
        value=DUMMY,
    )
    _plan, landed = _landing_plan(tmp_path, (credential,))
    transfer_allowed = (
        landed.credentials[0].disposition == CREDENTIAL_RESTORABLE
    )

    assert (
        identity_api.secret_environment_name_is_allowed(environment_name)
        is expected_allowed
    )
    assert identity_allowed is transfer_allowed is expected_allowed
    assert not hasattr(identity_api, "SECRET_ENVIRONMENT_NAMES")


def test_a_reserved_env_name_stays_reauth_required(tmp_path: Path) -> None:
    """The landing correctness policy cannot be widened by the producer."""

    credential = EnvCredential(
        harness="claude",
        entry_id="reserved",
        environment=("PATH",),
        value=DUMMY,
    )

    plan, landed = _landing_plan(tmp_path, (credential,))

    record = plan.records[0]
    assert record.install is not None and record.install.kind == "env"
    item = landed.credentials[0]
    assert item.disposition == CREDENTIAL_REAUTH_REQUIRED
    assert "not ones this target can bind" in item.notes
    assert landed.payload == {}
    # The value the producer held never reaches a receipt.
    assert DUMMY not in json.dumps([entry.as_dict() for entry in landed.credentials])


def test_a_value_the_gate_could_not_resolve_degrades_not_disappears(
    tmp_path: Path,
) -> None:
    """A grant that resolved no value is reported, never silently dropped."""

    credential = EnvCredential(
        harness="claude",
        entry_id="anthropic-auth",
        environment=("ANTHROPIC_AUTH_TOKEN",),
        requires_reauth=True,
        exclude="secrets/anthropic-auth",
        notes="not resolvable through this daemon's gate (missing)",
    )

    plan, landed = _landing_plan(tmp_path, (credential,))

    record = plan.records[0]
    assert record.name == "env-claude-anthropic-auth"
    assert record.requires_reauth is True
    assert record.install is None
    assert "secrets/anthropic-auth" in plan.excluded

    item = landed.credentials[0]
    assert item.disposition == CREDENTIAL_REAUTH_REQUIRED
    assert "not resolvable through this daemon's gate" in item.notes
    assert landed.payload == {}


def _fake_registry(grants, entity_token):
    """The minimal gate port: only what ``SecretResolver`` reads."""

    class _Agent:
        def __init__(self) -> None:
            self.entity_token = entity_token

    class _Registry:
        def secret_inventory(self, actor):
            return tuple(grants)

        def require(self, actor):
            return _Agent()

        def secret_grant(self, actor, grant_id):
            return next((g for g in grants if g.grant_id == grant_id), None)

        def home_receipt(self, actor):
            raise NotImplementedError

    return _Registry()


def test_resolve_env_credentials_degrades_a_missing_grant(tmp_path: Path) -> None:
    """The direct daemon-gate test for the degradation branch (S1 review obs. 3).

    The positive chain exercises the gate through quiesce; this pins the
    ``except SecretResolutionError`` path itself: one grant resolves from a
    user-provider entry, an equally authorized grant whose entry file is gone
    becomes a ``requires_reauth`` record with no value -- never dropped.
    """

    home = tmp_path / "hyprial"
    home.mkdir(mode=0o700)
    providers = home / "secrets" / "providers"
    providers.mkdir(parents=True)
    os.chmod(home / "secrets", 0o700)
    os.chmod(providers, 0o700)
    present = providers / "present.json"
    present.write_text(json.dumps({"token": DUMMY}))
    os.chmod(present, 0o600)

    token = "incarnation-token"
    grants = (
        SecretGrant(
            actor=ACTOR,
            entity_token=token,
            grant_id="g-present",
            source=SecretSource.USER_PROVIDER,
            entry_id="present",
            field_name="token",
            environment_names=("ANTHROPIC_AUTH_TOKEN",),
            revision=1,
        ),
        SecretGrant(
            actor=ACTOR,
            entity_token=token,
            grant_id="g-missing",
            source=SecretSource.USER_PROVIDER,
            entry_id="missing",
            field_name="token",
            environment_names=("ANTHROPIC_BASE_URL",),
            revision=1,
        ),
    )

    resolved = resolve_env_credentials(
        registry=_fake_registry(grants, token),
        hyprial_home=home,
        agent_name=ACTOR,
        harness="claude",
    )

    by_env = {item.environment[0]: item for item in resolved}
    good = by_env["ANTHROPIC_AUTH_TOKEN"]
    assert good.value == DUMMY and good.requires_reauth is False
    assert good.name == "env-claude-present"
    bad = by_env["ANTHROPIC_BASE_URL"]
    assert bad.requires_reauth is True and bad.value is None
    assert "missing" in bad.notes
    assert DUMMY not in repr(bad)


def test_a_resolvable_value_is_restorable_and_the_plaintext_stays_sealed(
    tmp_path: Path,
) -> None:
    """The control arm: an approved name with a value is restorable, sealed."""

    credential = EnvCredential(
        harness="claude",
        entry_id="anthropic-auth",
        environment=("ANTHROPIC_AUTH_TOKEN",),
        value=DUMMY,
    )

    plan, landed = _landing_plan(tmp_path, (credential,))

    assert plan.secrets == {"env-claude-anthropic-auth": DUMMY}
    assert landed.credentials[0].disposition == CREDENTIAL_RESTORABLE
    assert landed.payload == {"env-claude-anthropic-auth": DUMMY}
    for path in (tmp_path / "bundle").rglob("*"):
        if path.is_file():
            assert DUMMY.encode() not in path.read_bytes(), path
