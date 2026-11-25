"""Reconcile worker commits with public identity before same-process promotion.

These checks run inside the admission's continuous lock lifetime. A verified
result is local state, never a wire credential or a replacement for operation
ownership/receipt checks. No credential file or worker implementation is read.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.request_catalog.onboarding import (
    CallContext,
    FirstRunSnapshot,
    OperationError,
)
from hyprial.identity import (
    DIFFERENT_ACCOUNT_REASON,
    HOME_ACCOUNT_MIGRATION_GUIDE,
    HomeBinding,
    IdentityPublicState,
    IdentityRecordError,
    IdentitySnapshot,
    LEGACY_UNBOUND_REASON,
    home_account_conflict_message,
    legacy_business_data_present,
    read_home_binding,
    read_identity_snapshot,
    read_onboarding_identity,
    read_settings_username,
)
from hyprial.kernel import ipc_errors

from ..first_run import first_run_status
from ..locks import require_unoverridden_owner
from ..operations import OperationService
from .admission import BootstrapAdmission


def prepare_permit_binding(home: Path) -> tuple[HomeBinding | None, bool]:
    """Read home binding and stat-only legacy state before writing any auth receipt."""

    resolved_home = Path(home)
    return read_home_binding(resolved_home), legacy_business_data_present(resolved_home)


def classify_auth_failure(state: IdentityPublicState) -> tuple[str, OperationError | None]:
    """Map an identity worker terminal non-commit state into journal outcome + error."""

    if state.state == "failed" and state.error_code == "HOME_ACCOUNT_CONFLICT":
        return "failed", project_home_account_conflict(state)
    if state.state == "failed":
        outcome = {
            "DEVICE_CODE_EXPIRED": "expired",
            "ACCESS_DENIED": "denied",
        }.get(state.error_code, "unknown")
        return outcome, None
    return "unknown", None


def build_bootstrap_status(
    *,
    home: Path,
    operations: OperationService,
    context: CallContext,
    verified: "VerifiedIdentity | None",
) -> dict[str, Any]:
    """Project bootstrap onboarding status with verified firstRun and username."""

    try:
        trusted = read_onboarding_identity(home)
    except (IdentityRecordError, OSError, ValueError):
        trusted = None
    if (
        verified is None
        or trusted is None
        or trusted[0] != verified.snapshot
    ):
        first_run = FirstRunSnapshot(
            state="unavailable" if context.owner is not None else "none"
        )
    else:
        first_run = first_run_status(
            home,
            operations.root,
            authenticated=context.owner is not None,
            subject=trusted[2],
        )
    username = read_settings_username(home) if context.owner is not None else None
    result = operations.status(context, first_run=first_run, username=username)
    try:
        current = read_onboarding_identity(home)
    except (IdentityRecordError, OSError, ValueError):
        current = None
    if current != trusted:
        result["firstRun"] = FirstRunSnapshot(state="unavailable").as_status()
    return result


def project_home_account_conflict(state: IdentityPublicState) -> OperationError:
    """Project an internal ``HOME_ACCOUNT_CONFLICT`` failure into ``IDENTITY_CONFLICT``."""

    receipt = state.receipt
    reason_raw = receipt.get("reason")
    reason = (
        reason_raw
        if isinstance(reason_raw, str)
        and reason_raw in {DIFFERENT_ACCOUNT_REASON, LEGACY_UNBOUND_REASON}
        else DIFFERENT_ACCOUNT_REASON
    )
    guide_raw = receipt.get("guide")
    guide = (
        guide_raw
        if isinstance(guide_raw, str) and guide_raw.strip()
        else HOME_ACCOUNT_MIGRATION_GUIDE
    )
    login_user_id = receipt.get("loginUserId")
    issuer = receipt.get("issuer")
    data: dict[str, str] | None = None
    if (
        isinstance(login_user_id, str)
        and login_user_id.strip()
        and isinstance(issuer, str)
        and issuer.strip()
    ):
        data = {
            "reason": reason,
            "guide": guide,
            "loginUserId": login_user_id.strip(),
            "issuer": issuer.strip(),
        }
    return OperationError(
        code=ipc_errors.IDENTITY_CONFLICT,
        message=home_account_conflict_message(reason),
        data=data,
    )


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    operation_id: str
    snapshot: IdentitySnapshot
    admission: BootstrapAdmission = field(repr=False, compare=False)


def verify_worker_commit(
    admission: BootstrapAdmission,
    result: IdentityPublicState,
    *,
    operation_id: str,
    issuer: str,
) -> VerifiedIdentity:
    admission.locks.require_active()
    require_unoverridden_owner()
    if any(not isinstance(v, str) or not v.strip() for v in (operation_id, issuer)):
        raise ValueError("expected operation and issuer must be explicit")
    if (not isinstance(result, IdentityPublicState)
            or result.operation_id != operation_id or result.state != "committed"
            or not isinstance(result.owner, str) or not result.owner.strip()
            or result.issuer != issuer
            or result.receipt.get("credentialCommitted") is not True
            or result.receipt.get("settingsCommitted") is not True):
        raise RuntimeError("identity worker has no matching complete commit receipt")
    observed = read_identity_snapshot(admission.home)
    expected_uid = result.user_id if result.user_id is not None else observed.user_id
    if observed != IdentitySnapshot(result.owner, "casdoor", issuer, expected_uid):
        raise RuntimeError("public identity does not match the verified worker commit")
    return VerifiedIdentity(operation_id, observed, admission)


def verify_promotion_identity(
    admission: BootstrapAdmission, verified: VerifiedIdentity,
) -> IdentitySnapshot:
    admission.locks.require_active()
    require_unoverridden_owner()
    if not isinstance(verified, VerifiedIdentity) or verified.admission is not admission:
        raise RuntimeError("identity verification belongs to another bootstrap admission")
    observed = read_identity_snapshot(admission.home)
    if observed != verified.snapshot or observed.owner is None:
        raise RuntimeError("public identity changed before daemon construction")
    return observed
