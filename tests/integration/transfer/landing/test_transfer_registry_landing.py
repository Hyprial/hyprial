"""AT07 wiring: land a bundle through the target's own agent registry.

The unit half drives ``land_bundle_into_registry`` against a real
``AgentRegistry`` (the seam that owns incarnation rotation and the home
custody receipt).  The daemon half drives the same path through
``DaemonApplication.handle("transfer.land", ...)`` -- the production entry
point card 356 asks to run.
"""

from __future__ import annotations

from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import threading
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.daemon.impl.transfer.archive.envelope import (
    CredentialPolicy,
    CredentialRecord,
    generate_keypair,
    seal_envelope)
from hyprial.daemon.impl.transfer.landing.owner import LandingFailed, LandingNameTaken
from hyprial.daemon.impl.transfer.landing.registry import (
    CUSTODY_RECEIPT,
    LandingOwnerMismatch,
    land_bundle_into_registry)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL
from tests.support.transfer.signed import sign_bundle_for_target

OWNER = "alice"
MACHINE = "server"
ACTOR = "wk"
CREATED_AT = "2026-09-30T23:30:00+08:00"


def _source_home(tmp_path: Path) -> Path:
    """A source home shaped like a registry home (card 355's carry list)."""

    home = tmp_path / "source"
    for name in ("state", "config", "secrets"):
        (home / name).mkdir(parents=True)
    (home / CUSTODY_RECEIPT).write_text(
        json.dumps({"actor": ACTOR, "path": "/laptop/agents/wk", "resource": "src"})
    )
    (home / "state" / "sessions.json").write_text('{"session": 1}\n')
    (home / "config" / "agent.json").write_text('{"model": "x"}\n')
    (home / "secrets" / "token.txt").write_text("s3cret\n")
    return home


def _bundle(
    tmp_path: Path,
    home: Path,
    *,
    actor: str = ACTOR,
    owner: str = OWNER,
    grants=(),
    credential_envelope: str = "",
) -> Path:
    bundle_dir = tmp_path / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=owner,
        actor=actor,
        source_machine="laptop",
        created_at=CREATED_AT,
        grants=grants,
        credential_envelope=credential_envelope,
    )
    # Slice 2b: sign with the source's key and enroll it in the home ``_daemon``
    # builds (``state_dir=tmp_path``); the direct-call suites keep the rehearsal.
    sign_bundle_for_target(
        bundle_dir, actor=actor, source_machine="laptop", target_home=tmp_path,
        credential_envelope=credential_envelope or None,
    )
    return bundle_dir


def _sealed(
    records, *, policy: CredentialPolicy | None = None
) -> dict[str, object]:
    """A sealed envelope as ``transfer-credential-seal`` would write it."""

    _sender_private, recipient_public = generate_keypair()
    return seal_envelope(
        {"github": "secret-that-must-stay-sealed"},
        recipient_public_key=recipient_public,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
        records=records,
        policy=policy,
    )


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        tmp_path / "agents.sqlite3", owner=OWNER, machine=MACHINE, hyprial_home=tmp_path
    )


def _land(
    tmp_path: Path,
    bundle_dir: Path,
    registry: AgentRegistry,
    *,
    staging: Path | None = None,
    dry_run: bool = False,
    expected_actor: str | None = None,
):
    return land_bundle_into_registry(
        bundle_dir,
        registry=registry,
        home_root=tmp_path / "agents",
        staging=staging or (tmp_path / "staging" / "landing-1"),
        granted_by=f"user:{OWNER}",
        expected_actor=expected_actor,
        dry_run=dry_run,
        signature=UNSIGNED_REHEARSAL,
    )


def test_landing_moves_the_payload_under_a_fresh_incarnation(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, bundle_dir, registry)

        assert registry.exists(ACTOR)
        landed = tmp_path / "agents" / ACTOR
        assert result.destination == landed
        assert (landed / "state" / "sessions.json").read_text() == '{"session": 1}\n'
        assert (landed / "config" / "agent.json").read_text() == '{"model": "x"}\n'
        assert (landed / "secrets" / "token.txt").read_text() == "s3cret\n"
        assert result.files == 4
        assert result.identity_rebound is True
        # The staging tree is empty and pruned: nothing of the attempt is
        # left behind (the parent is the daemon's own state directory).
        assert not (tmp_path / "staging" / "landing-1").exists()
    finally:
        registry.close()


def test_landing_keeps_the_targets_own_custody_receipt(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        _land(tmp_path, bundle_dir, registry)

        mirrored = json.loads((tmp_path / "agents" / ACTOR / CUSTODY_RECEIPT).read_text())
        assert mirrored["actor"] == ACTOR
        # The source's receipt said /laptop/agents/wk; the target minted its own.
        assert mirrored["path"] != "/laptop/agents/wk"
        assert str(tmp_path / "agents" / ACTOR) == mirrored["path"]
    finally:
        registry.close()


def test_landing_materialises_the_manifest_grants(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    grant = json.dumps({"capability": "see-actors", "scope": json.dumps(["user:alice"])})
    bundle_dir = _bundle(tmp_path, home, grants=[grant])
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, bundle_dir, registry)

        recorded = registry.capability_grants(ACTOR)
        assert [item.capability for item in recorded] == ["see-actors"]
        assert recorded[0].granted_by == f"user:{OWNER}"
        assert result.grants == (recorded[0].grant_id,)
    finally:
        registry.close()


def test_the_landed_home_survives_the_registrys_own_fences(tmp_path: Path) -> None:
    """The payload must fit: destroy() walks the same topology it validates."""

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        _land(tmp_path, bundle_dir, registry)

        assert registry.destroy(ACTOR) is True
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
    finally:
        registry.close()


def test_a_taken_name_is_refused_before_anything_moves(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        registry.create(ACTOR)

        with pytest.raises(LandingNameTaken):
            _land(tmp_path, bundle_dir, registry)

        # The existing agent's home is untouched and the staging never appeared.
        assert (tmp_path / "agents" / ACTOR).is_dir()
        assert not (tmp_path / "staging").exists()
        assert bundle_dir.is_dir()
    finally:
        registry.close()


def test_a_failed_landing_leaves_no_row_no_home_and_no_staging(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bad = json.dumps({"capability": "wash-the-dishes", "scope": "self"})
    bundle_dir = _bundle(tmp_path, home, grants=[bad])
    registry = _registry(tmp_path)
    try:
        with pytest.raises(LandingFailed):
            _land(tmp_path, bundle_dir, registry)

        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging" / "landing-1").exists()
    finally:
        registry.close()


class _CreateLosesTheRace:
    """A registry whose ``create`` loses to a concurrent ``agent create``.

    ``exists`` reports the name free, so the preflight passes, and then
    ``create`` raises -- exactly the window between the name check and the
    insert.  Whatever the other process made must survive our undo.
    """

    def __init__(self, inner: AgentRegistry) -> None:
        self._inner = inner
        self.destroyed: list[str] = []

    @property
    def owner(self) -> str:
        return self._inner.owner

    def exists(self, actor: str) -> bool:
        return False

    def create(self, actor: str, *, cwd: str | None = None) -> object:
        raise RuntimeError(f"agent {actor!r} already exists")

    def destroy(self, actor: str) -> bool:
        self.destroyed.append(actor)
        return True

    def grant_capability(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("no grant may be recorded when create failed")

    def revoke_capability(self, *args: object, **kwargs: object) -> bool:
        raise AssertionError("nothing was granted, so nothing may be revoked")


def test_a_lost_create_race_does_not_destroy_the_other_agents_row(
    tmp_path: Path,
) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    racer = _CreateLosesTheRace(registry)
    try:
        # Somebody else's agent: what a concurrent `agent create` leaves behind
        # between our name check and our insert.
        registry.create(ACTOR)
        other_home = tmp_path / "agents" / ACTOR
        (other_home / "theirs.txt").write_text("not ours\n")

        with pytest.raises(LandingFailed):
            _land(tmp_path, bundle_dir, racer)

        # Our undo must not have touched their row or their files.
        assert racer.destroyed == []
        assert registry.exists(ACTOR) is True
        assert (other_home / "theirs.txt").read_text() == "not ours\n"
    finally:
        registry.close()


def test_a_payload_outside_a_registry_home_is_refused(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    (home / "notes.md").write_text("a top-level file the registry cannot fence\n")
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        with pytest.raises(LandingFailed) as caught:
            _land(tmp_path, bundle_dir, registry)

        assert "notes.md" in str(caught.value)
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
    finally:
        registry.close()


def test_a_foreign_owner_is_refused(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home, owner="bob")
    registry = _registry(tmp_path)
    try:
        with pytest.raises(LandingOwnerMismatch):
            _land(tmp_path, bundle_dir, registry)

        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
    finally:
        registry.close()


def test_dry_run_touches_neither_registry_nor_disk(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, bundle_dir, registry, dry_run=True)

        assert result.dry_run is True
        assert result.identity_rebound is False
        assert result.files == 4
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging" / "landing-1").exists()
    finally:
        registry.close()


def test_expected_actor_mismatch_is_a_landing_actor_error(tmp_path: Path) -> None:
    from hyprial.daemon.impl.transfer.landing.owner import LandingActorMismatch

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        with pytest.raises(LandingActorMismatch):
            _land(tmp_path, bundle_dir, registry, expected_actor="someone-else")
    finally:
        registry.close()


# -- the production entry point ---------------------------------------------


class _StubInbox:
    def unread_count(self) -> int:
        return 0


def _daemon(tmp_path: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=MACHINE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    app._inbox = _StubInbox()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    return app


def test_daemon_transfer_land_round_trip(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    app = _daemon(tmp_path)
    try:
        result = app.handle(
            "transfer.land", {"bundleDir": str(bundle_dir), "actor": ACTOR}
        )

        assert result["ok"] is True
        assert result["actor"] == ACTOR
        assert result["destination"] == str(tmp_path / "agents" / ACTOR)
        assert app.agents.exists(ACTOR) is True
        assert (tmp_path / "agents" / ACTOR / "state" / "sessions.json").is_file()
    finally:
        app.agents.close()


def test_daemon_transfer_land_reports_a_name_conflict(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    app = _daemon(tmp_path)
    try:
        app.agents.create(ACTOR)

        with pytest.raises(DaemonRequestError) as caught:
            app.handle("transfer.land", {"bundleDir": str(bundle_dir)})

        assert caught.value.code == "TRANSFER_CONFLICT"
    finally:
        app.agents.close()


def test_daemon_transfer_land_refuses_a_host_key_policy_without_a_grant(
    tmp_path: Path,
) -> None:
    """Card 356 item 2 on the production entry point, not just the unit one."""

    home = _source_home(tmp_path)
    envelope = _sealed(
        [CredentialRecord(name="github")],
        policy=CredentialPolicy(strategy="host"),
    )
    (home / "config" / "credential-envelope.json").write_text(
        json.dumps(envelope) + "\n"
    )
    bundle_dir = _bundle(
        tmp_path,
        home,
        credential_envelope="config/credential-envelope.json",
    )
    app = _daemon(tmp_path)
    staging_root = app.state_dir / "transfer-landing"
    try:
        with pytest.raises(DaemonRequestError) as caught:
            app.handle("transfer.land", {"bundleDir": str(bundle_dir)})

        assert caught.value.code == "CREDENTIAL_HOST_KEY_REFUSED"
        # Refused in the pre-write phase: no row, no home, no staging tree.
        assert app.agents.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
        assert not staging_root.exists()
    finally:
        app.agents.close()


def test_daemon_transfer_land_reports_credentials_as_reauth_required(
    tmp_path: Path,
) -> None:
    """The receipt, not the log, is what an operator reads after a landing."""

    home = _source_home(tmp_path)
    envelope = _sealed(
        [
            CredentialRecord(name="github", machine_bound=True),
            CredentialRecord(name="lark", requires_reauth=True, refreshable=True),
        ]
    )
    (home / "config" / "credential-envelope.json").write_text(
        json.dumps(envelope) + "\n"
    )
    bundle_dir = _bundle(
        tmp_path,
        home,
        credential_envelope="config/credential-envelope.json",
    )
    app = _daemon(tmp_path)
    try:
        result = app.handle("transfer.land", {"bundleDir": str(bundle_dir)})

        assert result["credentialPolicy"] == "original-user"
        by_name = {item["name"]: item for item in result["credentials"]}
        assert by_name["github"]["disposition"] == "reauth-required"
        assert by_name["lark"]["requiresReauth"] is True
        # The sealed plaintext never travels into a receipt.
        assert "secret-that-must-stay-sealed" not in json.dumps(result)
    finally:
        app.agents.close()


def test_daemon_transfer_land_refuses_a_bad_bundle(tmp_path: Path) -> None:
    app = _daemon(tmp_path)
    try:
        with pytest.raises(DaemonRequestError) as caught:
            app.handle("transfer.land", {"bundleDir": str(tmp_path / "nope")})

        assert caught.value.code == "TRANSFER_LANDING_FAILED"
        assert app.agents.exists(ACTOR) is False
    finally:
        app.agents.close()


@pytest.mark.parametrize("production", [False, True])
def test_landing_create_race_preserves_the_other_creator(tmp_path, monkeypatch, production):
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    app = _daemon(tmp_path) if production else None
    registry = app.agents if app is not None else _registry(tmp_path)
    original = registry.create
    winners = []

    def competing_create(
        actor, *, cwd=None, provider=None, model=None, preferred_harness=None
    ):
        winner = original(
            actor,
            cwd=cwd,
            provider=provider,
            model=model,
            preferred_harness=preferred_harness,
        )
        winners.append(winner)
        return original(actor, cwd=cwd)  # This landing loses the admission race.

    monkeypatch.setattr(registry, "create", competing_create)
    try:
        with pytest.raises((LandingFailed, DaemonRequestError)):
            if app is not None:
                app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
            else:
                _land(tmp_path, bundle_dir, registry)
        assert len(winners) == 1
        remaining = registry.get(ACTOR)
        assert remaining is not None, "rollback destroyed another creator's identity"
        assert remaining.entity_token == winners[0].entity_token
        assert (tmp_path / "agents" / ACTOR / CUSTODY_RECEIPT).is_file()
    finally:
        registry.close()


@pytest.mark.parametrize("mode", ["replacement", "restore-owned", "restore-replacement"])
def test_landing_rollback_fences_incarnation_after_replacement_or_restore(tmp_path, monkeypatch, mode):
    from hyprial.daemon.impl.transfer.landing.registry import RegistryLandingAuthority

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    active = [_registry(tmp_path)]
    replacement = []
    original = RegistryLandingAuthority._materialise

    def fail_after_interleaving(authority, staged, destination):
        original(authority, staged, destination)
        if mode != "restore-owned":
            assert active[0].destroy(ACTOR)
            replacement.append(active[0].create(ACTOR))
            (destination / "config" / "winner.txt").write_text("must survive")
        if mode.startswith("restore-"):
            active[0].close()
            active[0] = _registry(tmp_path)
            authority.registry = active[0]
        raise OSError("controlled failure after incarnation interleaving")

    monkeypatch.setattr(RegistryLandingAuthority, "_materialise", fail_after_interleaving)
    try:
        with pytest.raises(LandingFailed):
            _land(tmp_path, bundle_dir, active[0])
        current = active[0].get(ACTOR)
        if replacement:
            assert current is not None, "rollback destroyed a replacement incarnation"
            assert current.entity_token == replacement[0].entity_token
            assert (tmp_path / "agents" / ACTOR / "config" / "winner.txt").read_text() == "must survive"
        else:
            assert current is None  # The exact owned incarnation still rolls back.
            assert not (tmp_path / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging" / "landing-1").exists()
    finally:
        active[0].close()


def test_daemon_transfer_land_materialises_private_grants_via_agent_owner(tmp_path):
    home = _source_home(tmp_path)
    grant = json.dumps({"capability": "see-actors", "scope": json.dumps(["user:alice"])})
    bundle_dir = _bundle(tmp_path, home, grants=[grant])
    app = _daemon(tmp_path)
    result = app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
    assert result["ok"] is True
    grants = app._agent_session_domains.agent.read_capability_grants(ACTOR)
    assert len(grants) == 1
    assert grants[0].granted_by == f"user:{OWNER}"
    assert grants[0].entity_token == app.agents.require(ACTOR).entity_token


@pytest.mark.parametrize("production", [False, True])
def test_landing_grant_undo_cannot_touch_replacement_incarnation(tmp_path, production):
    from hyprial.identity.impl.agents.registry._base import AgentEntityConflict
    from hyprial.daemon.impl.composition import DomainCommandError
    from hyprial.daemon.impl.transfer.landing.registry import RegistryLandingAuthority

    app = _daemon(tmp_path) if production else None
    registry = app.agents if app is not None else _registry(tmp_path)
    authority = RegistryLandingAuthority(
        registry, home_root=tmp_path / "agents", granted_by=f"user:{OWNER}",
    )
    grant = json.dumps({"capability": "see-actors", "scope": json.dumps(["user:alice"])})
    try:
        authority.adopt_agent(ACTOR, owner=OWNER, home=_source_home(tmp_path), source_machine="source")
        grant_id = authority.materialise_grant(ACTOR, grant)
        assert registry.destroy(ACTOR)
        replacement = registry.create(ACTOR)
        registry.grant_capability(
            ACTOR, grant_id=grant_id, capability="see-actors", scope=json.dumps(["user:alice"]),
            granted_by=f"user:{OWNER}", revision=1,
        )
        for operation in (
            lambda: authority.revoke_grants(ACTOR, [grant_id]),
            lambda: authority.materialise_grant(ACTOR, grant),
            lambda: authority.drop_agent(ACTOR),
        ):
            with pytest.raises((AgentEntityConflict, DomainCommandError)) as caught:
                operation()
            assert caught.value.code == "AGENT_VERSION_CONFLICT"
        assert registry.require(ACTOR).entity_token == replacement.entity_token
        grants = (app._agent_session_domains.agent.read_capability_grants(ACTOR)
                  if app is not None else registry.capability_grants(ACTOR))
        assert len(grants) == 1 and grants[0].grant_id == grant_id
        assert grants[0].entity_token == replacement.entity_token
    finally:
        registry.close()


@pytest.mark.parametrize("production", [False, True])
def test_landing_payload_never_enters_a_replacement_home(tmp_path, monkeypatch, production):
    from hyprial.daemon.impl.transfer.landing.registry import RegistryLandingAuthority

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    app = _daemon(tmp_path) if production else None
    registry = app.agents if app is not None else _registry(tmp_path)
    original = RegistryLandingAuthority._materialise
    winners = []

    def replace_before_materialisation(authority, staged, destination):
        assert registry.destroy(ACTOR)
        winners.append(registry.create(ACTOR))
        (destination / "config" / "agent.json").write_text("replacement-owned bytes")
        original(authority, staged, destination)

    monkeypatch.setattr(RegistryLandingAuthority, "_materialise", replace_before_materialisation)
    try:
        with pytest.raises((LandingFailed, DaemonRequestError)):
            if app is not None:
                app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
            else:
                _land(tmp_path, bundle_dir, registry)
        assert registry.require(ACTOR).entity_token == winners[0].entity_token
        assert (tmp_path / "agents" / ACTOR / "config" / "agent.json").read_text() == "replacement-owned bytes"
    finally:
        registry.close()



def test_home_owner_serializes_payload_against_destroy_and_recreate(tmp_path, monkeypatch):
    from hyprial.identity.impl.agents.home.provisioner import AgentHomeProvisioner

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    app = _daemon(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    destroy_committed = threading.Event()
    results, errors, winners, replacement_errors, receipts = [], [], [], [], []
    materialise = AgentHomeProvisioner.materialise_payload
    destroy_record = app._agent_registry.prepare_destroy_record

    def gated_materialise(provisioner, receipt, staged, expected_files):
        assert threading.current_thread().name == "agent-home-effect"
        receipts.append(receipt)
        entered.set()
        assert release.wait(5.0)
        return materialise(provisioner, receipt, staged, expected_files)

    def observed_destroy(*args, **kwargs):
        result = destroy_record(*args, **kwargs)
        destroy_committed.set()
        return result

    def land():
        try:
            results.append(app.handle("transfer.land", {"bundleDir": str(bundle_dir)}))
        except BaseException as error:
            errors.append(error)

    def replace():
        try:
            assert app.agents.destroy(ACTOR, expected_entity_token=receipts[0].entity_token)
            winners.append(app.agents.create(ACTOR))
            (tmp_path / "agents" / ACTOR / "config" / "agent.json").write_text("winner")
        except BaseException as error:
            replacement_errors.append(error)

    monkeypatch.setattr(AgentHomeProvisioner, "materialise_payload", gated_materialise)
    monkeypatch.setattr(app._agent_registry, "prepare_destroy_record", observed_destroy)
    landing = threading.Thread(target=land)
    replacing = threading.Thread(target=replace)
    landing.start()
    try:
        assert entered.wait(5.0)
        replacing.start()
        assert destroy_committed.wait(5.0)
        assert winners == []  # Cleanup/create is still behind the admitted FS effect.
        release.set()
        landing.join(5.0)
        replacing.join(5.0)
        assert not landing.is_alive() and not replacing.is_alive()
        assert results == []
        assert len(errors) == 1 and isinstance(errors[0], DaemonRequestError)
        assert errors[0].code == "TRANSFER_LANDING_FAILED"
        assert replacement_errors == []
        assert winners[0].entity_token != receipts[0].entity_token
        assert app.agents.require(ACTOR).entity_token == winners[0].entity_token
        assert (tmp_path / "agents" / ACTOR / "config" / "agent.json").read_text() == "winner"
    finally:
        release.set()
        landing.join(5.0)
        if replacing.ident is not None:
            replacing.join(5.0)


def test_final_landing_confirmation_refuses_a_replacement(tmp_path, monkeypatch):
    from hyprial.daemon.impl.transfer.landing.registry import RegistryLandingAuthority

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    app = _daemon(tmp_path)
    original = RegistryLandingAuthority.confirm_landing
    winners = []

    def replace_before_confirmation(authority, actor):
        assert app.agents.destroy(actor)
        winners.append(app.agents.create(actor))
        (tmp_path / "agents" / actor / "config" / "agent.json").write_text("final winner")
        original(authority, actor)

    monkeypatch.setattr(RegistryLandingAuthority, "confirm_landing", replace_before_confirmation)
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
    assert caught.value.code == "TRANSFER_LANDING_FAILED"
    assert app.agents.require(ACTOR).entity_token == winners[0].entity_token
    assert (tmp_path / "agents" / ACTOR / "config" / "agent.json").read_text() == "final winner"


def test_landing_checks_incarnation_after_staging_cleanup(tmp_path, monkeypatch):
    from hyprial.daemon.impl.transfer.landing import registry as registry_landing

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    app = _daemon(tmp_path)
    original = registry_landing._prune
    winners = []

    def cleanup_then_replace(staged):
        original(staged)
        assert app.agents.destroy(ACTOR)
        winners.append(app.agents.create(ACTOR))
        (tmp_path / "agents" / ACTOR / "config" / "agent.json").write_text("cleanup winner")

    monkeypatch.setattr(registry_landing, "_prune", cleanup_then_replace)
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
    assert caught.value.code == "TRANSFER_LANDING_FAILED"
    assert app.agents.require(ACTOR).entity_token == winners[0].entity_token
    assert (tmp_path / "agents" / ACTOR / "config" / "agent.json").read_text() == "cleanup winner"


@pytest.mark.parametrize("change", ["content", "extra", "missing", "symlink", "mode"])
def test_admitted_landing_rechecks_frozen_manifest_payload(tmp_path, monkeypatch, change):
    from hyprial.identity.impl.agents.home.provisioner import AgentHomeProvisioner

    bundle_dir = _bundle(tmp_path, _source_home(tmp_path))
    app = _daemon(tmp_path)
    original = AgentHomeProvisioner.materialise_payload
    stages = []

    def mutate_after_admission(provisioner, receipt, staged, expected_files):
        stages.append(staged)
        target = staged / "config" / "agent.json"
        if change == "content":
            target.write_text("changed after manifest validation")
        elif change == "extra":
            (staged / "config" / "extra").write_text("not in manifest")
        elif change == "missing":
            target.unlink()
        elif change == "symlink":
            target.unlink()
            target.symlink_to(tmp_path / "source" / "config" / "agent.json")
        else:
            target.chmod(target.stat().st_mode ^ 0o100)
        return original(provisioner, receipt, staged, expected_files)

    monkeypatch.setattr(AgentHomeProvisioner, "materialise_payload", mutate_after_admission)
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
    assert caught.value.code == "TRANSFER_LANDING_FAILED"
    assert not app.agents.exists(ACTOR)
    assert not (tmp_path / "agents" / ACTOR).exists()
    assert len(stages) == 1 and not stages[0].exists()


def test_timed_out_landing_retains_stage_and_owner_until_io_settles(tmp_path, monkeypatch):
    import inspect
    from hyprial.identity.impl.agents.actor._base import _MaterialiseLandingHomeCommand
    from hyprial.identity.impl.agents.home.provisioner import AgentHomeProvisioner

    bundle_dir = _bundle(tmp_path, _source_home(tmp_path))
    app = _daemon(tmp_path)
    actor = app.agents._domains.agent
    lane = actor._home_effects
    entered, release = threading.Event(), threading.Event()
    stages, results, errors = [], [], []
    original_materialise = AgentHomeProvisioner.materialise_payload
    original_call = actor._call_home_access
    assert inspect.signature(original_call).parameters["timeout"].default == 70.0

    def gate(provisioner, receipt, staged, expected_files):
        stages.append(staged)
        entered.set()
        assert release.wait(5.0)
        return original_materialise(provisioner, receipt, staged, expected_files)

    def shortened_call(command, timeout=70.0):
        return original_call(command, timeout=0.5 if isinstance(command, _MaterialiseLandingHomeCommand) else timeout)

    def land():
        try:
            results.append(app.handle("transfer.land", {"bundleDir": str(bundle_dir)}))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(AgentHomeProvisioner, "materialise_payload", gate)
    monkeypatch.setattr(actor, "_call_home_access", shortened_call)
    landing = threading.Thread(target=land)
    landing.start()
    try:
        assert entered.wait(5.0)
        assert actor.drain(0.5).complete is False
        assert lane._closed is True
        landing.join(2.0)
        assert not landing.is_alive()
        assert not results and len(errors) == 1
        assert isinstance(errors[0], DaemonRequestError)
        assert errors[0].code == "TRANSFER_LANDING_FAILED"
        assert stages[0].exists()  # Caller rollback must not retire in-use input.
        assert len(lane._pending) == 1
        assert actor._home_calls.pending_count == 1  # Accepted I/O retains custody.
        release.set()
        assert actor.drain(2.0).complete is True
        assert not stages[0].exists()
        assert not lane._pending and actor._home_calls.pending_count == 0
    finally:
        release.set()
        landing.join(5.0)


def test_landing_rejects_completion_from_retired_generation(tmp_path, monkeypatch):
    import time
    from hyprial.identity.impl.agents.actor._core import _AgentGeneration
    from hyprial.identity.impl.agents.home.provisioner import AgentHomeProvisioner
    from hyprial.identity.impl.agents.actor.ports import ClearRestoreDispositionCommand
    from hyprial.kernel.impl.contracts.ports import PortAdmission

    bundle_dir = _bundle(tmp_path, _source_home(tmp_path))
    app = _daemon(tmp_path)
    actor = app.agents._domains.agent
    entered, release, restarted = threading.Event(), threading.Event(), threading.Event()
    results, errors, stages = [], [], []
    materialise = AgentHomeProvisioner.materialise_payload
    dispatch = _AgentGeneration._dispatch

    def gate(provisioner, receipt, staged, expected_files):
        stages.append(staged)
        entered.set()
        assert release.wait(5.0)
        return materialise(provisioner, receipt, staged, expected_files)

    def crash_once(generation, command):
        if getattr(command, "correlation_id", "") == "landing-controlled-restart":
            raise RuntimeError("controlled actor restart with landing I/O held")
        return dispatch(generation, command)

    def land():
        try:
            results.append(app.handle("transfer.land", {"bundleDir": str(bundle_dir)}))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(AgentHomeProvisioner, "materialise_payload", gate)
    monkeypatch.setattr(_AgentGeneration, "_dispatch", crash_once)
    landing = threading.Thread(target=land)
    landing.start()
    try:
        assert entered.wait(5.0)
        before = actor.generation
        assert actor.submit(ClearRestoreDispositionCommand("landing-controlled-restart", ACTOR)) is PortAdmission.ACCEPTED
        deadline = time.monotonic() + 2.0
        while actor.generation <= before and time.monotonic() < deadline:
            restarted.wait(0.005)
        assert actor.generation > before
        release.set()
        landing.join(5.0)
        assert not landing.is_alive()
        assert results == [] and len(errors) == 1
        assert isinstance(errors[0], DaemonRequestError)
        assert errors[0].code == "TRANSFER_LANDING_FAILED"
        assert not app.agents.exists(ACTOR)
        assert not stages[0].exists()
        assert actor._home_calls.pending_count == 0
    finally:
        release.set()
        landing.join(5.0)


def test_landing_rechecks_bytes_after_payload_copy(tmp_path, monkeypatch):
    from hyprial.identity.impl.agents.home import provisioner as home_module

    bundle_dir = _bundle(tmp_path, _source_home(tmp_path))
    app = _daemon(tmp_path)
    original = home_module.shutil.copyfileobj

    def copy_then_mutate(source, target):
        original(source, target)
        target.write(b"changed during I/O")

    monkeypatch.setattr(home_module.shutil, "copyfileobj", copy_then_mutate)
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
    assert caught.value.code == "TRANSFER_LANDING_FAILED"
    assert not app.agents.exists(ACTOR)
    assert not (tmp_path / "agents" / ACTOR).exists()


@pytest.mark.parametrize("production", [False, True])
@pytest.mark.parametrize("phase", ["before_open", "after_open"])
def test_landing_cannot_move_files_through_a_replaced_stage_parent(tmp_path, monkeypatch, production, phase):
    import shutil
    from hyprial.identity.impl.agents.home import provisioner as home_module

    bundle_dir = _bundle(tmp_path, _source_home(tmp_path))
    app = _daemon(tmp_path) if production else None
    registry = app.agents if app else _registry(tmp_path)
    outside = tmp_path / "unowned-source"
    outside.mkdir()
    fingerprint = home_module.snapshot_home_payload
    swapped, stages = [], []
    open_payload = home_module._open_payload_file

    def swap(staged):
        source = staged / "config"
        shutil.copy2(source / "agent.json", outside / "agent.json")
        source.rename(staged / "original-config")
        source.symlink_to(outside, target_is_directory=True)
        swapped.append(staged)

    def fingerprint_then_swap(staged, **kwargs):
        rows = fingerprint(staged, **kwargs)
        stages.append(staged)
        if not swapped and phase == "before_open":
            swap(staged)
        return rows

    def open_then_swap(root_fd, relative):
        fd = open_payload(root_fd, relative)
        if not swapped and phase == "after_open" and relative.name == "agent.json":
            swap(stages[0])
        return fd

    monkeypatch.setattr(home_module, "snapshot_home_payload", fingerprint_then_swap)
    monkeypatch.setattr(home_module, "_open_payload_file", open_then_swap)
    try:
        with pytest.raises(LandingFailed):
            _land(tmp_path, bundle_dir, registry)
        assert len(swapped) == 1
        assert (outside / "agent.json").read_text() == '{"model": "x"}\n'
        assert not registry.exists(ACTOR)
        assert not (tmp_path / "agents" / ACTOR).exists()
    finally:
        if app is None:
            registry.close()


def test_landing_refuses_missing_safe_source_descriptor_capability(tmp_path, monkeypatch):
    import os

    bundle_dir = _bundle(tmp_path, _source_home(tmp_path))
    app = _daemon(tmp_path)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {os.open})
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("transfer.land", {"bundleDir": str(bundle_dir)})
    assert caught.value.code == "TRANSFER_LANDING_FAILED"
    assert not app.agents.exists(ACTOR)
    assert not (tmp_path / "agents" / ACTOR).exists()
