"""AT07 second slice: land a received agent home on the target identity.

``receive_bundle`` puts the bytes down; landing is the identity half of the
same step.  The target must adopt the incoming actor under an incarnation of
its **own** (the source's entity token is authority state and never travels
with a bundle), materialise the private grants recorded in the manifest, and
leave nothing behind when any part of that fails.

Only the daemon can mint an incarnation or write a grant row, so those arrive
here as a small protocol (``LandingAuthority``).  The wiring passes the real
implementation; tests pass a fake.  Undo runs in the reverse order of the work:
grants revoked, agent row dropped, then the files this attempt created.

Refusals that happen before anything is written: the target already speaks for
this actor, the bundle carries a different actor than the caller expects, the
destination already holds something (``receive_bundle`` refuses to merge into
it), or the bundle itself does not validate.

Credentials are decided here but never decrypted: ``plan_credential_landing``
reads the sealed envelope's header, refuses a landing whose policy would fall
back to the host key without the host owner's grant, and reports every carried
credential as needing re-authentication (card 356 item 2, first phase).  The
daemon-side wiring -- the real authority over the agent registry, container
materialisation, and the two-machine run -- is the next slice; until it lands
this module has no production caller, and nothing here writes to a registry by
itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from hyprial.daemon.impl.transfer.archive.bundle import BundleManifest, validate_bundle
from hyprial.daemon.impl.transfer.landing.credentials import (
    CredentialLanding,
    plan_credential_landing)
from hyprial.daemon.impl.transfer.landing.receive import ReceiveResult, receive_bundle

__all__ = [
    "LandingActorMismatch",
    "LandingAuthority",
    "LandingError",
    "LandingFailed",
    "LandingNameTaken",
    "LandingResult",
    "land_agent_home",
]


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


@runtime_checkable
class LandingAuthority(Protocol):
    """The daemon-side operations a landing cannot do for itself."""

    def agent_exists(self, actor: str) -> bool:
        """Whether the target already has a row for ``actor``."""

    def adopt_agent(
        self,
        actor: str,
        *,
        owner: str,
        home: Path,
        source_machine: str,
    ) -> None:
        """Insert the agent row, minting a fresh incarnation for it."""

    def drop_agent(self, actor: str) -> None:
        """Remove the row ``adopt_agent`` inserted (undo)."""

    def materialise_grant(self, actor: str, grant: str) -> str | None:
        """Write one private grant; return an id to revoke, or ``None``."""

    def revoke_grants(self, actor: str, grant_ids: Sequence[str]) -> None:
        """Undo ``materialise_grant`` for the ids it returned."""


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
        """Credentials the target must re-authenticate before it can use them.

        Card 356 item 2's first phase does not decrypt the envelope on the
        target, so this is the honest answer for every credential carried.
        """

        return tuple(item.name for item in self.credentials)

    def as_dict(self) -> dict[str, object]:
        """A serialisable receipt.

        The entity token is deliberately absent: like ``transfer.plan``, this
        result says that an incarnation was minted, never what it is.
        """

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


def land_agent_home(
    bundle_dir: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    *,
    authority: LandingAuthority,
    expect_actor: str | None = None,
    dry_run: bool = False,
) -> LandingResult:
    """Adopt a validated bundle as this target's copy of the agent.

    Order matters: the name conflict is checked before anything is written, the
    home is materialised before the identity exists, and every later failure
    takes both back.  ``dry_run`` stops after the read-only checks and reports
    what a real landing would do without touching the registry or the disk.
    """

    manifest = validate_bundle(bundle_dir)
    if expect_actor is not None and manifest.actor != expect_actor:
        raise LandingActorMismatch(
            f"bundle carries actor {manifest.actor!r}, expected {expect_actor!r}"
        )
    # Credentials are decided before anything is written: a policy the target
    # cannot honour must refuse while the registry and the disk are untouched.
    credential_plan = plan_credential_landing(manifest, bundle_dir)
    dest = Path(destination)
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

    destination_existed = dest.exists()
    landed = receive_bundle(bundle_dir, dest, expect_actor=manifest.actor)

    grants: list[str] = []
    adopted = False
    try:
        authority.adopt_agent(
            manifest.actor,
            owner=manifest.owner,
            home=landed.destination,
            source_machine=manifest.source_machine,
        )
        adopted = True
        for grant in manifest.grants:
            grant_id = authority.materialise_grant(manifest.actor, grant)
            if grant_id is not None:
                grants.append(grant_id)
    except BaseException as error:
        problems = _undo(
            authority,
            manifest,
            landed,
            grants=grants,
            adopted=adopted,
            destination_existed=destination_existed,
        )
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        detail = f"; undo problems: {'; '.join(problems)}" if problems else ""
        raise LandingFailed(
            f"landing {manifest.actor!r} failed and was undone: {error}{detail}"
        ) from error

    return LandingResult(
        actor=manifest.actor,
        owner=manifest.owner,
        source_machine=manifest.source_machine,
        destination=landed.destination,
        files=landed.files,
        bytes=landed.bytes,
        grants=tuple(grants),
        identity_rebound=True,
        dry_run=False,
        credential_policy=credential_plan.policy.strategy,
        credentials=credential_plan.credentials,
    )


def _undo(
    authority: LandingAuthority,
    manifest: BundleManifest,
    landed: ReceiveResult,
    *,
    grants: Sequence[str],
    adopted: bool,
    destination_existed: bool,
) -> list[str]:
    """Take back, in reverse, whatever the attempt already did.

    Returns the steps that could *not* be undone.  Those travel back with the
    failure instead of being swallowed: a half-undone landing is exactly what
    an operator needs to see.
    """

    problems: list[str] = []
    if grants:
        try:
            authority.revoke_grants(manifest.actor, list(grants))
        except Exception as error:  # noqa: BLE001 - reported, never masked
            problems.append(f"revoke grants: {error}")
    if adopted:
        try:
            authority.drop_agent(manifest.actor)
        except Exception as error:  # noqa: BLE001 - reported, never masked
            problems.append(f"drop agent {manifest.actor!r}: {error}")
    try:
        _remove_home(manifest, landed.destination, destination_existed)
    except OSError as error:
        problems.append(f"remove {landed.destination}: {error}")
    return problems


def _remove_home(
    manifest: BundleManifest, destination: Path, destination_existed: bool
) -> None:
    """Delete exactly what this landing copied, and nothing else.

    A destination that was already there (empty, because receiving refuses a
    non-empty one) is left in place, still empty.
    """

    for entry in manifest.entries:
        try:
            (destination / entry.path).unlink()
        except FileNotFoundError:
            continue
    for current, _dirnames, _filenames in os.walk(destination, topdown=False):
        path = Path(current)
        if path == destination and destination_existed:
            continue
        try:
            path.rmdir()
        except OSError:
            continue
