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
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from hyprial.identity import HomePayloadFile, snapshot_home_payload
from hyprial.daemon.impl.transfer.archive.bundle import validate_bundle
from hyprial.daemon.impl.transfer.landing.credentials import plan_credential_landing
from hyprial.daemon.impl.transfer.landing.owner import (
    LandingActorMismatch,
    LandingError,
    LandingFailed,
    LandingNameTaken,
    LandingResult)
from hyprial.daemon.impl.transfer.landing.receive import receive_bundle

__all__ = [
    "CUSTODY_RECEIPT",
    "HOME_TOPOLOGY",
    "LandingGrantInvalid",
    "LandingOwnerMismatch",
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

    def create(self, actor: str) -> object: ...

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
    ) -> None:
        """Mint the row and move the staged payload into the minted home."""

        if owner != self.registry.owner:
            raise LandingOwnerMismatch(
                f"this target speaks for owner {self.registry.owner!r}; the "
                f"bundle carries {owner!r}, and a cross-owner landing is not "
                "this slice"
            )
        if self._expected_files is None:
            self._expected_files = snapshot_home_payload(Path(home))
        created = self.registry.create(actor)
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


def land_bundle_into_registry(
    bundle_dir: os.PathLike[str] | str,
    *,
    registry: RegistryPort,
    home_root: os.PathLike[str] | str,
    staging: os.PathLike[str] | str,
    granted_by: str,
    expected_actor: str | None = None,
    dry_run: bool = False,
) -> LandingResult:
    """Land a bundle on this target: fresh incarnation, payload, grants.

    ``staging`` must not exist; it is created by the receive and removed again
    whether the landing succeeds or is undone.  ``dry_run`` stops after the
    read-only checks and reports what a real landing would do.
    """

    manifest = validate_bundle(bundle_dir)
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
    # Credentials are decided in the same pre-write phase as the name check, so
    # a policy this target cannot honour refuses while the registry and the
    # disk are untouched -- and the receipt says so either way.
    credential_plan = plan_credential_landing(manifest, bundle_dir)
    dest = authority.home_root / manifest.actor
    if authority.agent_exists(manifest.actor):
        raise LandingNameTaken(
            f"{manifest.actor!r} already exists on this target; a transfer "
            "never overwrites an existing identity"
        )
    total_bytes = sum(entry.size for entry in manifest.entries)
    if dry_run:
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

    staged = receive_bundle(bundle_dir, staging, expect_actor=manifest.actor)
    grants: list[str] = []
    adopted = False
    try:
        authority.adopt_agent(
            manifest.actor,
            owner=manifest.owner,
            home=staged.destination,
            source_machine=manifest.source_machine,
        )
        adopted = True
        for grant in manifest.grants:
            grant_id = authority.materialise_grant(manifest.actor, grant)
            if grant_id is not None:
                grants.append(grant_id)
        _prune(staged.destination)
        authority.confirm_landing(manifest.actor)
    except BaseException as error:
        problems = _undo(authority, manifest.actor, staged.destination, grants)
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
        credentials=credential_plan.credentials,
    )


def _undo(
    authority: RegistryLandingAuthority,
    actor: str,
    staging: os.PathLike[str] | str,
    grants: Sequence[str],
) -> list[str]:
    """Take back, in reverse, whatever the attempt already did."""

    problems: list[str] = []
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
