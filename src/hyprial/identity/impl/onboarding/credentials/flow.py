"""Identity permit validation and the one-way credential/owner commit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from hyprial.kernel import is_identity_id_segment

from ..home_binding import (
    DIFFERENT_ACCOUNT_REASON,
    HOME_ACCOUNT_MIGRATION_GUIDE,
    LEGACY_UNBOUND_REASON,
    home_account_conflict_message,
    normalize_issuer,
)
from ..identity_record import read_identity_snapshot, write_settings_identity
from ..ports import (
    AuthProfile,
    CommitPermit,
    HomeBinding,
    IdentityError,
    IdentitySnapshot,
)
from .credential_store import (
    CREDENTIAL_VERSION,
    IdentityCredential,
    credential_path,
    reserve_identity_generation,
    write_login_credential,
)
from .permit_ledger import consume_permit


def _snapshots_match(
    observed: IdentitySnapshot, expected: IdentitySnapshot
) -> bool:
    return (
        observed.owner == expected.owner
        and observed.mode == expected.mode
        and observed.issuer == expected.issuer
        and (expected.user_id is None or observed.user_id == expected.user_id)
    )


def validate_begin_permit(
    *,
    permit: CommitPermit | None,
    profile: AuthProfile,
    home: Path,
    lock_home: Path,
    now_epoch: int,
) -> IdentitySnapshot:
    """Refuse a missing, expired, mismatched, or stale commit permit."""

    if permit is None:
        raise IdentityError(
            "PERMIT_REQUIRED",
            "identity commit requires a narrow commit permit; nothing was written",
        )
    if not permit.operation_id or not permit.nonce:
        raise IdentityError(
            "PERMIT_INVALID",
            "commit permit needs a non-empty operation id and nonce",
        )
    if Path(home).resolve() != Path(permit.home).resolve():
        raise IdentityError(
            "PERMIT_HOME_MISMATCH",
            "commit permit belongs to another home; nothing was written",
        )
    if Path(lock_home).resolve() != Path(home).resolve():
        raise IdentityError(
            "PERMIT_HOME_MISMATCH",
            "identity lock is not the permit home lock; nothing was written",
        )
    if permit.issuer != profile.issuer or permit.client_id != profile.client_id:
        raise IdentityError(
            "PERMIT_PROFILE_MISMATCH",
            "commit permit does not match the trusted auth profile; nothing "
            "was written",
        )
    if now_epoch >= permit.expires_at:
        raise IdentityError(
            "PERMIT_EXPIRED",
            "commit permit expired before the identity stage started; nothing "
            "was written",
        )
    observed = read_identity_snapshot(home)
    if not _snapshots_match(observed, permit.expected_identity):
        raise IdentityError(
            "STALE_IDENTITY_SNAPSHOT",
            "settings identity changed after the commit permit was issued; "
            "nothing was written",
        )
    return observed


def _owner_gate(
    owner: str, observed: IdentitySnapshot, profile: AuthProfile
) -> None:
    if not owner or ":" in owner or is_identity_id_segment(owner.lower()):
        raise IdentityError(
            "OWNER_INVALID",
            "login identity must be non-empty, contain no ':', and not have an "
            f"exact identity id shape; got {owner!r}",
        )
    if observed.owner is None:
        return
    if observed.owner != owner:
        raise IdentityError(
            "OWNER_MISMATCH",
            f"settings.owner is {observed.owner!r} but authenticated identity "
            f"is {owner!r}; this worker performs first login or same-owner "
            "reauth only, and nothing was written",
            data={"currentOwner": observed.owner, "loginOwner": owner},
        )
    if observed.mode not in {None, "casdoor"}:
        raise IdentityError(
            "IDENTITY_MODE_CONFLICT",
            f"settings identity mode is {observed.mode!r}; this worker cannot "
            "replace it, and nothing was written",
        )
    if observed.issuer not in {None, profile.issuer}:
        raise IdentityError(
            "IDENTITY_ISSUER_CONFLICT",
            f"settings identity issuer is {observed.issuer!r} but the trusted "
            f"profile issuer is {profile.issuer!r}; nothing was written",
        )


def commit_identity(
    *,
    permit: CommitPermit,
    profile: AuthProfile,
    home: Path,
    owner: str,
    subject: str,
    refresh_token: str,
    environ: Mapping[str, str],
    now_epoch: int,
) -> dict[str, Any]:
    """Commit credential first and settings second, preserving partial state.

    The permit expiry is rechecked here because device authorization can
    outlive it after ``begin``.  A valid permit is consumed in the durable home
    ledger before the credential write, under the caller's identity
    transaction lock.  Consumption returns only after the ledger entry's
    directory is synced; if that durability confirmation fails, no credential
    is written and the landed ledger keeps the permit burned, so a retry needs
    a new permit.
    """

    observed = read_identity_snapshot(home)
    if not _snapshots_match(observed, permit.expected_identity):
        raise IdentityError(
            "STALE_IDENTITY_SNAPSHOT",
            "settings identity changed before the commit; nothing was written",
        )
    if now_epoch >= permit.expires_at:
        raise IdentityError(
            "PERMIT_EXPIRED",
            "commit permit expired before the identity commit; nothing was "
            "written",
        )
    normalized_issuer = normalize_issuer(profile.issuer)
    binding = permit.home_binding
    if binding is None:
        if permit.legacy_business_data:
            raise IdentityError(
                "HOME_ACCOUNT_CONFLICT",
                home_account_conflict_message(LEGACY_UNBOUND_REASON),
                data={
                    "reason": LEGACY_UNBOUND_REASON,
                    "guide": HOME_ACCOUNT_MIGRATION_GUIDE,
                    "loginUserId": subject,
                    "issuer": normalized_issuer,
                },
            )
        effective_owner = owner
    elif (
        normalize_issuer(binding.issuer) == normalized_issuer
        and binding.user_id == subject
    ):
        effective_owner = binding.owner
    elif permit.legacy_business_data:
        raise IdentityError(
            "HOME_ACCOUNT_CONFLICT",
            home_account_conflict_message(DIFFERENT_ACCOUNT_REASON),
            data={
                "reason": DIFFERENT_ACCOUNT_REASON,
                "guide": HOME_ACCOUNT_MIGRATION_GUIDE,
                "loginUserId": subject,
                "issuer": normalized_issuer,
            },
        )
    else:
        effective_owner = owner
    _owner_gate(effective_owner, observed, profile)
    override = (environ.get("HYPRIAL_OWNER") or "").strip()
    if override and override != effective_owner:
        raise IdentityError(
            "OWNER_OVERRIDE_CONFLICT",
            f"HYPRIAL_OWNER is {override!r} but login identity is {effective_owner!r}; "
            "writing settings would not change the daemon's identity",
            data={"override": override, "loginOwner": effective_owner},
        )
    target = credential_path(hyprial_home=home)
    consume_permit(permit, home=home, now_epoch=now_epoch)
    credential = IdentityCredential(
        version=CREDENTIAL_VERSION,
        issuer=profile.issuer,
        refresh_token=refresh_token,
        obtained_at=datetime.now(UTC).isoformat(),
        identity_generation=reserve_identity_generation(home),
    )
    write_login_credential(credential, target)
    try:
        write_settings_identity(
            effective_owner,
            mode="casdoor",
            issuer=profile.issuer,
            user_id=subject,
            username=owner,
            home_account=HomeBinding(
                issuer=normalized_issuer,
                user_id=subject,
                owner=effective_owner,
            ),
            environ=environ,
            hyprial_home=home,
        )
    except Exception as error:
        raise IdentityError(
            "OWNER_WRITE_FAILED",
            f"credential written but settings.owner could not be written: {error}; "
            "re-run the identity stage to complete",
            data={"credentialCommitted": True, "settingsCommitted": False},
        ) from error
    return {"credentialCommitted": True, "settingsCommitted": True}


__all__ = ["commit_identity", "validate_begin_permit"]
