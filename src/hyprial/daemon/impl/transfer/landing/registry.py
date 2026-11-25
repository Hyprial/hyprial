"""AT07 wiring: land a received bundle through this target's agent registry.

``land_agent_home`` (``hyprial.transfer.landing``) is the authority-agnostic
half of a landing: materialise the home, then ask an authority to adopt it.  A
daemon cannot run it in that order, and the reason is concrete rather than
stylistic:

* The target's custody receipt lives **inside** its own home at
  ``state/home-receipt.json``, and the registry refuses to provision a home
  whose path already exists without a matching receipt (``unowned-residue``).
* An AT06 bundle always carries the *source's* receipt, because that file is
  part of the source home.  Copying it down would overwrite the target's, with
  the source's path and resource token -- so the receipt must be re-minted
  here and the source's copy must not travel.

So the registry-backed landing runs in this order, and every step is undone in
reverse on any failure:

1. the bundle is received into a private staging directory under the daemon's
   state dir -- nothing belonging to the target is touched yet;
2. the target mints the agent row and its home (fresh incarnation, fresh
   receipt).  A name that already exists is refused before a byte moves;
3. the staged payload is moved into that home, skipping the source's custody
   receipt, and only into the directories a registry home may hold;
4. the agent-private grants recorded in the manifest are materialised.

The two-machine run, container materialisation and strict resume named by card
356 are not part of this slice.

UA1b (2026-11-18): a bundle whose actor is the user agent's reserved name is
refused before the row is minted.  Every machine ensures its own user agent,
so one never travels, and a landed row would hold the name without the
``template: user`` fact.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence, runtime_checkable

from hyprial.identity import HomePayloadFile, SecretSource, snapshot_home_payload
from hyprial.kernel import harden_private_path, path_is_reparse
from hyprial.daemon.impl.transfer.archive.bundle import validate_bundle
from hyprial.daemon.impl.transfer.archive.envelope import (
    RecipientKeyError,
    read_recipient_key,
    recipient_key_path,
)
from hyprial.daemon.impl.transfer.landing.credentials import (
    CredentialApplyResult,
    CredentialBinding,
    CredentialLanding,
    apply_credential_landing,
    plan_credential_landing,
)
from hyprial.daemon.impl.transfer.landing.receive import receive_bundle
from hyprial.daemon.impl.transfer.manifest_reading import load_sealed_manifest

__all__ = [
    "CUSTODY_RECEIPT",
    "HOME_TOPOLOGY",
    "LandingActorMismatch",
    "LandingError",
    "LandingFailed",
    "LandingGrantInvalid",
    "LandingNameTaken",
    "LandingOwnerMismatch",
    "LandingResult",
    "RegistryLandingAuthority",
    "land_bundle_into_registry",
]

#: The target's own custody receipt.  The source's copy never travels.
CUSTODY_RECEIPT = "state/home-receipt.json"

#: Directories a registry-managed agent home may hold.  Anything else in the
#: bundle is refused loudly: the registry's own cleanup fences a home with
#: extra top-level entries, so landing one would create an agent that cannot
#: be destroyed without operator surgery.
HOME_TOPOLOGY = ("state", "config", "secrets", "workspace")


class LandingError(Exception):
    """Base class for the identity half of a receive."""

    code = "landing_error"


class LandingNameTaken(LandingError):
    """The target already speaks for this actor; a transfer never overwrites."""

    code = "landing_name_taken"


class LandingActorMismatch(LandingError):
    """The bundle carries a different actor than the caller expects."""

    code = "landing_actor_mismatch"


class LandingFailed(LandingError):
    """The landing did not complete; every trace of the attempt was undone."""

    code = "landing_failed"


@dataclass(frozen=True)
class LandingResult:
    """What a successful landing did (or would do, with ``dry_run``)."""

    actor: str
    owner: str
    source_machine: str
    destination: Path
    files: int
    bytes: int
    grants: tuple[str, ...]
    identity_rebound: bool
    dry_run: bool
    credential_policy: str = ""
    credentials: tuple[CredentialLanding, ...] = ()

    @property
    def needs_reauth(self) -> tuple[str, ...]:
        """Credentials the target must re-authenticate before it can use them."""

        return tuple(item.name for item in self.credentials)

    def as_dict(self) -> dict[str, object]:
        """A serialisable receipt without incarnation authority tokens."""

        return {
            "actor": self.actor,
            "owner": self.owner,
            "source_machine": self.source_machine,
            "destination": str(self.destination),
            "files": self.files,
            "bytes": self.bytes,
            "grants": list(self.grants),
            "identityRebound": self.identity_rebound,
            "dryRun": self.dry_run,
            "credentialPolicy": self.credential_policy,
            "credentials": [item.as_dict() for item in self.credentials],
        }


class LandingOwnerMismatch(LandingError):
    """The bundle belongs to a different owner than this target speaks for."""

    code = "landing_owner_mismatch"


class LandingGrantInvalid(LandingError):
    """A grant string in the manifest is not a capability/scope pair."""

    code = "landing_grant_invalid"


@runtime_checkable
class RegistryPort(Protocol):
    """The slice of ``AgentRegistry`` a landing needs (duck-typed on purpose)."""

    owner: str

    def exists(self, actor: str) -> bool: ...

    def create(
        self,
        actor: str,
        *,
        cwd: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        preferred_harness: str | None = None,
    ) -> object: ...

    def destroy_landing_home(self, actor: str, *, expected_entity_token: str) -> bool: ...

    def materialise_landing_home(
        self, actor: str, staged_root: str, *, expected_entity_token: str,
        expected_files: tuple[HomePayloadFile, ...],
    ) -> str: ...

    def confirm_landing_home(self, actor: str, *, expected_entity_token: str) -> str: ...

    def grant_capability(
        self,
        actor: str,
        *,
        grant_id: str,
        capability: str,
        scope: str,
        granted_by: str,
        revision: int,
        expected_entity_token: str,
    ) -> object: ...

    def revoke_capability(
        self, actor: str, grant_id: str, *, revoked_by: str, expected_entity_token: str,
    ) -> bool: ...

    def grant_secret(
        self,
        actor: str,
        *,
        grant_id: str,
        source: SecretSource,
        entry_id: str,
        field_name: str | None,
        environment_names: Iterable[str],
        revision: int,
        prevalidated_home_token: str | None = None,
    ) -> object: ...

    def revoke_secret_grant(self, actor: str, grant_id: str) -> bool: ...


class RegistryLandingAuthority:
    """Materialise a landing against this target's agent registry.

    ``home_root`` is the directory the registry provisions homes under
    (``<hyprial home>/agents``); the authority derives the destination from the
    actor rather than trusting a path from the bundle.
    """

    def __init__(
        self, registry: RegistryPort, *, home_root: os.PathLike[str] | str, granted_by: str,
        expected_files: tuple[HomePayloadFile, ...] | None = None,
    ) -> None:
        if not isinstance(granted_by, str) or not granted_by.strip():
            raise ValueError("granted_by must name the principal recording grants")
        self.registry = registry
        self.home_root = Path(home_root)
        self.granted_by = granted_by
        # A failed create acquires no custody; a successful one records the
        # exact incarnation that all later grant and rollback writes must match.
        self._adopted: tuple[str, str] | None = None
        self._expected_files = expected_files
        self.stage_io_pending = False

    # -- LandingAuthority -------------------------------------------------

    @property
    def adopted(self) -> bool:
        """Whether this attempt holds an exact incarnation claim for undo."""

        return self._adopted is not None

    def agent_exists(self, actor: str) -> bool:
        return bool(self.registry.exists(actor))

    def adopt_agent(
        self,
        actor: str,
        *,
        owner: str,
        home: os.PathLike[str] | str,
        source_machine: str,
        cwd: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        preferred_harness: str | None = None,
    ) -> None:
        """Mint the row and move the staged payload into the minted home.

        ``cwd`` is the operator's resolved working directory on *this* target;
        the row records it so the landed agent's own record agrees with the
        runtime context the activation binds, instead of leaving the record
        with a cwd of ``None`` (P0 records it through ``create_transfer_hosted``).
        ``provider``/``model``/``preferred_harness`` are the source registry
        row's launch wiring, read from the sealed bundle identity bag; without
        them a dispatched message cannot resolve a launch.
        """

        if owner != self.registry.owner:
            raise LandingOwnerMismatch(
                f"this target speaks for owner {self.registry.owner!r}; the "
                f"bundle carries {owner!r}, and a cross-owner landing is not "
                "this slice"
            )
        from hyprial.daemon.impl.user_agent.naming import USER_AGENT_NAME

        if actor == USER_AGENT_NAME:
            raise LandingError(
                f"{actor!r} is reserved for the owner's user agent, which every "
                "machine creates for itself; it is never landed"
            )
        if self._expected_files is None:
            self._expected_files = snapshot_home_payload(Path(home))
        created = self.registry.create(
            actor,
            cwd=cwd,
            provider=provider,
            model=model,
            preferred_harness=preferred_harness,
        )
        token = getattr(created, "entity_token", None)
        if not isinstance(token, str) or not token:
            raise LandingError("adoption returned no incarnation custody token")
        self._adopted = (actor, token)
        self._materialise(Path(home), self.home_root / actor)

    def drop_agent(self, actor: str) -> None:
        """Undo ``adopt_agent``: destroy the row (and with it the home)."""

        if self._adopted is None:
            return  # Failed create never acquired custody of an identity.
        token = self._owned_token(actor)
        self.registry.destroy_landing_home(actor, expected_entity_token=token)
        self._adopted = None

    def _owned_token(self, actor: str) -> str:
        if self._adopted is None or self._adopted[0] != actor:
            raise LandingError("landing has no incarnation custody for this actor")
        return self._adopted[1]

    def materialise_grant(self, actor: str, grant: str) -> str | None:
        """Record one agent-private grant; return its id so undo can drop it."""

        capability, scope = _parse_grant(grant)
        grant_id = uuid.uuid4().hex
        self.registry.grant_capability(
            actor,
            grant_id=grant_id,
            capability=capability,
            scope=scope,
            granted_by=self.granted_by,
            revision=1,
            expected_entity_token=self._owned_token(actor),
        )
        return grant_id

    def revoke_grants(self, actor: str, grant_ids: Sequence[str]) -> None:
        for grant_id in grant_ids:
            self.registry.revoke_capability(
                actor, grant_id, revoked_by=self.granted_by,
                expected_entity_token=self._owned_token(actor),
            )

    def bind_secret(self, actor: str, binding: CredentialBinding) -> str:
        """Record one restored env credential on the incarnation this landing minted."""

        grant_id = uuid.uuid4().hex
        self.registry.grant_secret(
            actor,
            grant_id=grant_id,
            source=SecretSource.AGENT_PRIVATE,
            entry_id=binding.entry_id,
            field_name=binding.field,
            environment_names=binding.environment,
            revision=1,
            # The token undo, grants and the home receipt all speak: the one
            # this attempt minted, never a caller-supplied token.
            prevalidated_home_token=self._owned_token(actor),
        )
        return grant_id

    def revoke_secret_grants(self, actor: str, grant_ids: Sequence[str]) -> None:
        for grant_id in grant_ids:
            self.registry.revoke_secret_grant(actor, grant_id)

    # -- filesystem -------------------------------------------------------

    def _materialise(self, staged_root: Path, home: Path) -> None:
        """Give the frozen payload to the existing home FS owner."""
        if self._adopted is None:
            raise LandingError("payload import has no incarnation custody")
        actor, token = self._adopted
        assert self._expected_files is not None
        try:
            materialised = self.registry.materialise_landing_home(
                actor, str(staged_root), expected_entity_token=token,
                expected_files=self._expected_files,
            )
        except BaseException as error:
            self.stage_io_pending = bool(getattr(error, "home_io_pending", False))
            raise
        if Path(materialised) != home:
            raise LandingError("home authority returned a different destination")

    def confirm_landing(self, actor: str) -> None:
        home = self.registry.confirm_landing_home(
            actor, expected_entity_token=self._owned_token(actor),
        )
        if Path(home) != self.home_root / actor:
            raise LandingError("home authority confirmed a different destination")


def _launch_wiring(identity: object) -> "_LaunchWiring":
    """The launch wiring a bundle carries, or an all-``None`` wiring.

    Written by the source as ``identity.launch`` and sealed with the rest of
    the manifest.  A bundle exported before that field existed reads as an
    all-``None`` wiring: the landed row keeps the pre-fix ``provider``/``model``
    of ``None`` and nothing is invented for it.
    """

    if not isinstance(identity, dict):
        return _LaunchWiring(None, None, None)
    raw = identity.get("launch")
    if not isinstance(raw, dict):
        return _LaunchWiring(None, None, None)
    provider = raw.get("provider")
    model = raw.get("model")
    harness = raw.get("harness")
    return _LaunchWiring(
        provider if isinstance(provider, str) and provider else None,
        model if isinstance(model, str) and model else None,
        harness if isinstance(harness, str) and harness else None,
    )


@dataclass(frozen=True, slots=True)
class _LaunchWiring:
    """The source registry row's launch wiring as it rides the bundle."""

    provider: str | None
    model: str | None
    harness: str | None


def land_bundle_into_registry(
    bundle_dir: os.PathLike[str] | str,
    *,
    registry: RegistryPort,
    home_root: os.PathLike[str] | str,
    machine_home: os.PathLike[str] | str | None = None,
    staging: os.PathLike[str] | str,
    granted_by: str,
    expected_actor: str | None = None,
    cwd: str | None = None,
    harness: str | None = None,
    dry_run: bool = False,
) -> LandingResult:
    """Land a bundle on this target: fresh incarnation, payload, grants.

    ``staging`` must not exist; it is created by the receive and removed again
    whether the landing succeeds or is undone.  ``dry_run`` stops after the
    read-only checks and reports what a real landing would do.

    ``harness`` is the harness this landing is for, when the caller knows it
    (the P2 chain does).  It only gates the bundle's carried launch wiring: a
    bundle stamped for another harness is refused before anything is written,
    so one harness's provider/model can never be bound to another's row.

    ``machine_home`` is this daemon's own home: its X25519 recipient key (when
    one exists) is what opens the bundle's sealed credential envelope, and the
    restored credentials are installed and bound only after the home is
    confirmed ready.  Without it the envelope's public header is still read
    and policed, and every credential stops at ``reauth-required``.
    """

    reading = load_sealed_manifest(bundle_dir)
    manifest = validate_bundle(bundle_dir, manifest=reading.manifest)
    wiring = _launch_wiring(manifest.identity)
    if harness is not None and wiring.harness is not None and wiring.harness != harness:
        raise LandingError(
            f"the bundle carries launch wiring for harness {wiring.harness!r}, "
            f"but this landing is for {harness!r}; refusing to bind one "
            "harness's provider/model to another harness"
        )
    if expected_actor is not None and manifest.actor != expected_actor:
        raise LandingActorMismatch(
            f"bundle carries actor {manifest.actor!r}, expected {expected_actor!r}"
        )
    authority = RegistryLandingAuthority(
        registry, home_root=home_root, granted_by=granted_by,
        expected_files=tuple(HomePayloadFile(item.path, item.size, item.sha256, item.mode)
                             for item in manifest.entries),
    )
    if manifest.owner != registry.owner:
        raise LandingOwnerMismatch(
            f"this target speaks for owner {registry.owner!r}; the bundle "
            f"carries {manifest.owner!r}"
        )
    credential_plan = plan_credential_landing(
        manifest, bundle_dir, recipient_key=_machine_recipient_key(machine_home)
    )
    dest = authority.home_root / manifest.actor
    if authority.agent_exists(manifest.actor):
        raise LandingNameTaken(
            f"{manifest.actor!r} already exists on this target; a transfer "
            "never overwrites an existing identity"
        )
    total_bytes = sum(entry.size for entry in manifest.entries)
    if dry_run:
        for entry in manifest.entries:
            _check_topology(Path(entry.path))
        if _manifest_carries_exclusions(manifest):
            _validate_carried_org_exclusions(
                Path(bundle_dir) / "payload",
                require_private_dirs=False,
                require_present=True,
            )
        return LandingResult(
            actor=manifest.actor,
            owner=manifest.owner,
            source_machine=manifest.source_machine,
            destination=dest,
            files=len(manifest.entries),
            bytes=total_bytes,
            grants=(),
            identity_rebound=False,
            dry_run=True,
            credential_policy=credential_plan.policy.strategy,
            credentials=credential_plan.credentials,
        )

    if _manifest_carries_exclusions(manifest):
        try:
            _validate_carried_org_exclusions(
                Path(bundle_dir) / "payload",
                require_private_dirs=False,
                require_present=True,
            )
        except LandingError as error:
            raise LandingFailed(
                f"landing {manifest.actor!r} failed; rollback attempted: {error}"
            ) from error

    staging_existed = Path(staging).exists()
    staged = receive_bundle(
        bundle_dir, staging, expect_actor=manifest.actor, sealed=reading,
    )
    grants: list[str] = []
    secret_grants: list[str] = []
    adopted_exclusions: list[_ExclusionUndoHandle] = []
    applied: CredentialApplyResult | None = None
    adopted = False
    try:
        if _manifest_carries_exclusions(manifest):
            _harden_staging_exclusions_dirs(
                staged.destination, harden_root=not staging_existed
            )
            _validate_carried_org_exclusions(
                staged.destination, require_present=True
            )
        authority.adopt_agent(
            manifest.actor,
            owner=manifest.owner,
            home=staged.destination,
            source_machine=manifest.source_machine,
            cwd=cwd,
            provider=wiring.provider,
            model=wiring.model,
            preferred_harness=wiring.harness,
        )
        adopted = True
        for grant in manifest.grants:
            grant_id = authority.materialise_grant(manifest.actor, grant)
            if grant_id is not None:
                grants.append(grant_id)
        _prune(staged.destination)
        authority.confirm_landing(manifest.actor)
        applied = apply_credential_landing(dest, credential_plan)
        for binding in applied.bindings:
            secret_grants.append(authority.bind_secret(manifest.actor, binding))
        _adopt_carried_org_exclusions(
            dest=dest,
            machine_home=machine_home,
            registry=registry,
            actor=manifest.actor,
            adopted_exclusions=adopted_exclusions,
        )
    except BaseException as error:
        problems = _undo(
            authority,
            manifest.actor,
            staged.destination,
            grants,
            secret_grants,
            adopted_exclusions,
        )
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        detail = f"; undo problems: {'; '.join(problems)}" if problems else ""
        raise LandingFailed(
            f"landing {manifest.actor!r} failed; rollback attempted: {error}{detail}"
        ) from error

    return LandingResult(
        actor=manifest.actor,
        owner=manifest.owner,
        source_machine=manifest.source_machine,
        destination=dest,
        files=staged.files,
        bytes=staged.bytes,
        grants=tuple(grants),
        identity_rebound=adopted,
        dry_run=False,
        credential_policy=credential_plan.policy.strategy,
        credentials=(
            applied.plan.credentials
            if applied is not None
            else credential_plan.credentials
        ),
    )


class _ExclusionUndoHandle(Protocol):
    agent_id: str

    def undo(self) -> bool: ...


def _undo(
    authority: RegistryLandingAuthority,
    actor: str,
    staging: os.PathLike[str] | str,
    grants: Sequence[str],
    secret_grants: Sequence[str] = (),
    adopted_exclusions: Sequence[_ExclusionUndoHandle] = (),
) -> list[str]:
    """Take back, in reverse, whatever the attempt already did."""

    problems: list[str] = []
    for handle in reversed(adopted_exclusions):
        try:
            handle.undo()
        except Exception as error:  # noqa: BLE001 - reported, never masked
            problems.append(
                f"rollback org exclusions for {handle.agent_id!r}: {error}"
            )
    if secret_grants:
        try:
            authority.revoke_secret_grants(actor, list(secret_grants))
        except Exception as error:  # noqa: BLE001 - reported, never masked
            problems.append(f"revoke secret grants: {error}")
    if grants:
        try:
            authority.revoke_grants(actor, list(grants))
        except Exception as error:  # noqa: BLE001 - reported, never masked
            problems.append(f"revoke grants: {error}")
    if authority.adopted:
        try:
            # The writer still verifies the stored incarnation before removal.
            authority.drop_agent(actor)
        except Exception as error:  # noqa: BLE001 - reported, never masked
            problems.append(f"drop agent {actor!r}: {error}")
    # Otherwise there is nothing of ours to take back.  Reaching here without
    # ``adopted`` means the name was taken (a preflight refusal) or a
    # concurrent ``agent create`` won the race inside ``adopt_agent``; in both
    # cases the row and home belong to somebody else.
    if authority.stage_io_pending:
        return problems  # Accepted FS work retains its input until completion.
    try:
        shutil.rmtree(staging, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError as error:
        problems.append(f"remove staging {staging}: {error}")
    return problems


def _prune(staged_root: os.PathLike[str] | str) -> None:
    """Drop the staging tree once the home owner has finished reading it."""

    try:
        shutil.rmtree(staged_root)
    except FileNotFoundError:
        pass


def _manifest_carries_exclusions(manifest: object) -> bool:
    from hyprial.daemon.impl.org.network.member_agents.exclusions import (
        TRANSFER_EXCLUSIONS_RELPATH,
    )

    entries = getattr(manifest, "entries", ())
    return any(
        getattr(entry, "path", None) == TRANSFER_EXCLUSIONS_RELPATH
        for entry in entries
    )


def _harden_staging_exclusions_dirs(root: Path, *, harden_root: bool) -> None:
    targets = (root, root / "state") if harden_root else (root / "state",)
    for candidate in targets:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISLNK(info.st_mode)
            or path_is_reparse(info)
            or not stat.S_ISDIR(info.st_mode)
        ):
            return
        harden_private_path(candidate, mode=0o700)


def _validate_carried_org_exclusions(
    root: Path,
    *,
    require_private_dirs: bool = True,
    require_present: bool = False,
) -> tuple[str, ...] | None:
    from hyprial.daemon.impl.org.network.member_agents.exclusions import (
        OrgAgentExclusionStorageError,
        read_carried_exclusion_spaces,
    )

    try:
        spaces = read_carried_exclusion_spaces(
            root, require_private_dirs=require_private_dirs
        )
    except (OSError, ValueError, OrgAgentExclusionStorageError) as error:
        raise LandingError(
            f"invalid carried org agent exclusions: {error}"
        ) from error
    if require_present and spaces is None:
        raise LandingError(
            "invalid carried org agent exclusions: missing carried exclusions file"
        )
    return spaces


def _adopt_carried_org_exclusions(
    *,
    dest: Path,
    machine_home: os.PathLike[str] | str | None,
    registry: RegistryPort,
    actor: str,
    adopted_exclusions: list[_ExclusionUndoHandle] | None = None,
) -> None:
    from hyprial.daemon.impl.org.network.member_agents.exclusions import (
        TRANSFER_EXCLUSIONS_RELPATH,
        OrgAgentExclusionAdoption,
        OrgAgentExclusionStorageError,
        OrgAgentExclusionStore,
        read_carried_exclusion_spaces,
    )

    spaces = read_carried_exclusion_spaces(dest)
    if spaces is None or machine_home is None or not spaces:
        return
    carried = dest / TRANSFER_EXCLUSIONS_RELPATH
    landed = registry.require(actor)
    store = OrgAgentExclusionStore(Path(machine_home))
    recorded = False

    def _record(adoption: OrgAgentExclusionAdoption) -> None:
        nonlocal recorded
        if not recorded and adoption.changed and adopted_exclusions is not None:
            adopted_exclusions.append(adoption)
            recorded = True

    try:
        adoption = store.adopt_for_agent(
            landed.agent_id,
            spaces,
            on_committed=_record,
        )
    except OrgAgentExclusionStorageError as error:
        committed = getattr(error, "committed_adoption", None)
        if committed is not None:
            _record(committed)
        raise
    if isinstance(adoption, OrgAgentExclusionAdoption):
        _record(adoption)
    carried.unlink()


def _check_topology(relative: Path) -> None:
    if not relative.parts or relative.parts[0] not in HOME_TOPOLOGY:
        raise LandingError(
            f"the bundle holds {relative.as_posix()!r}, which is outside the "
            f"directories a registry home may hold {list(HOME_TOPOLOGY)}; "
            "refusing to create a home the registry cannot clean up"
        )


def _parse_grant(grant: str) -> tuple[str, str]:
    try:
        value = json.loads(grant)
    except (TypeError, json.JSONDecodeError) as error:
        raise LandingGrantInvalid(
            f"grant {grant!r} is not a JSON object"
        ) from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("capability"), str)
        or not isinstance(value.get("scope"), str)
    ):
        raise LandingGrantInvalid(
            f"grant {grant!r} must be an object with string 'capability' and 'scope'"
        )
    return str(value["capability"]), str(value["scope"])


def _machine_recipient_key(
    machine_home: os.PathLike[str] | str | None,
) -> bytes | None:
    """The private half this machine holds, or ``None`` when it holds none.

    Landing never mints: the key is minted by ``transfer-recipient-key`` when
    a source asks for the public half before sealing.  A machine with no key,
    or a key that cannot be read, lands with every sealed credential reported
    ``reauth-required`` -- an unreadable envelope must not fail the landing.
    """

    if machine_home is None:
        return None
    path = recipient_key_path(machine_home)
    if not path.exists():
        return None
    try:
        private, _public = read_recipient_key(path)
    except RecipientKeyError:
        return None
    return private
