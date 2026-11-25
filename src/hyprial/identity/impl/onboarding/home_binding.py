# Decision record (W10c / L3, 2026-11-23):
# - Home account binding records {issuer, userId, owner} in settings.homeAccount.
# - legacy_business_data_present is strictly stat-only (lstat + os.scandir) and
#   fail-closed on OSError/PermissionError so upgrade/unreadable homes never open
#   business SQLite or JSON files before onboarding admission.
"""Home account binding reader and stat-only legacy business data detector."""

from __future__ import annotations

import os
from pathlib import Path

from hyprial.kernel import is_identity_id_segment

from .identity_record import read_strict_settings_record
from .ports import HomeBinding, IdentityRecordError

HOME_ACCOUNT_MIGRATION_GUIDE = "docs/guides/migration-account-binding.md"
DIFFERENT_ACCOUNT_REASON = "home-bound-to-another-account"
LEGACY_UNBOUND_REASON = "home-binding-missing"

LEGACY_BUSINESS_DATABASES = (
    "agents.sqlite3",
    "adapters.sqlite3",
    "inbox.sqlite3",
    "lifecycle-operations.sqlite3",
    "routines.sqlite3",
    "workflows.sqlite3",
    "pac-graph.sqlite3",
    "conversation-records.sqlite3",
    "cells.sqlite3",
)
SQLITE_AUXILIARY_SUFFIXES = ("", "-wal", "-shm")
LEGACY_BUSINESS_TEXT_FILES = ("desired-state.json.v1", "users.json")
LEGACY_DEVICE_STATE_FILES = (
    "state/tailcat/device.json",
    "state/tailcat/device-identity.json",
    "state/tailcat/machine-binding.json",
)
LEGACY_USER_KEYS_DIR = "state/identity/user-keys"

_LEGACY_UNBOUND_MESSAGE = (
    "此 HYPRIAL_HOME 留有数据但没有账号绑定记录，本次登录未写入任何内容。"
    "请换一个新的 HYPRIAL_HOME，或让 agent 按 docs/guides/migration-account-binding.md 处理后重试。"
)
_DIFFERENT_ACCOUNT_MESSAGE = (
    "此 HYPRIAL_HOME 已绑定另一个账号并留有其数据，本次登录未写入任何内容。"
    "请换一个新的 HYPRIAL_HOME，或让 agent 按 docs/guides/migration-account-binding.md 处理后重试。"
)


def normalize_issuer(issuer: str) -> str:
    """Return the canonical issuer string without a trailing slash."""

    stripped = issuer.strip()
    if not stripped:
        raise ValueError("issuer must be non-empty")
    normalized = stripped.rstrip("/")
    return normalized or stripped


def home_account_conflict_message(reason: str) -> str:
    """Return the fixed Chinese prompt for a ``HOME_ACCOUNT_CONFLICT`` reason."""

    if reason == LEGACY_UNBOUND_REASON:
        return _LEGACY_UNBOUND_MESSAGE
    return _DIFFERENT_ACCOUNT_MESSAGE


def _binding_field_error(home: Path, detail: str) -> IdentityRecordError:
    return IdentityRecordError(
        "IDENTITY_METADATA_INVALID",
        f"settings.homeAccount {detail}; refusing home account binding",
        {"path": str(Path(home) / "settings.json"), "field": "homeAccount"},
    )


def read_home_binding(home: Path) -> HomeBinding | None:
    """Return the immutable binding from ``homeAccount`` or active login fields."""

    resolved_home = Path(home)
    record = read_strict_settings_record(resolved_home)
    if record is None:
        return None
    if "homeAccount" in record:
        raw = record.get("homeAccount")
        if raw is not None:
            if not isinstance(raw, dict):
                raise _binding_field_error(resolved_home, "must be a JSON object")
            issuer = raw.get("issuer")
            user_id = raw.get("userId")
            owner = raw.get("owner")
            if not isinstance(issuer, str) or not issuer.strip():
                raise _binding_field_error(
                    resolved_home, "issuer must be a non-empty string"
                )
            if not isinstance(user_id, str) or not user_id.strip():
                raise _binding_field_error(
                    resolved_home, "userId must be a non-empty string"
                )
            if not isinstance(owner, str) or not owner.strip():
                raise _binding_field_error(
                    resolved_home, "owner must be a non-empty string"
                )
            clean_owner = owner.strip()
            if ":" in clean_owner or is_identity_id_segment(clean_owner.lower()):
                raise _binding_field_error(
                    resolved_home, "owner has an invalid local shape"
                )
            return HomeBinding(
                issuer=normalize_issuer(issuer),
                user_id=user_id.strip(),
                owner=clean_owner,
            )
    issuer = record.get("identityIssuer")
    user_id = record.get("userId")
    owner = record.get("owner")
    if (
        isinstance(issuer, str)
        and issuer.strip()
        and isinstance(user_id, str)
        and user_id.strip()
        and isinstance(owner, str)
        and owner.strip()
    ):
        clean_owner = owner.strip()
        if ":" not in clean_owner and not is_identity_id_segment(clean_owner.lower()):
            return HomeBinding(
                issuer=normalize_issuer(issuer),
                user_id=user_id.strip(),
                owner=clean_owner,
            )
    return None


def _stat_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


def _non_empty_dir_stat(path: Path) -> bool:
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


def legacy_business_data_present(home: Path) -> bool:
    """Stat-only fail-closed detector for pre-existing local business state."""

    root = Path(home)
    state_dir = root / "state"
    for database in LEGACY_BUSINESS_DATABASES:
        for suffix in SQLITE_AUXILIARY_SUFFIXES:
            if _stat_exists(state_dir / f"{database}{suffix}"):
                return True
    for filename in LEGACY_BUSINESS_TEXT_FILES:
        if _stat_exists(state_dir / filename):
            return True
    for relpath in LEGACY_DEVICE_STATE_FILES:
        if _stat_exists(root / relpath):
            return True
    return _non_empty_dir_stat(root / LEGACY_USER_KEYS_DIR)


__all__ = [
    "DIFFERENT_ACCOUNT_REASON",
    "HOME_ACCOUNT_MIGRATION_GUIDE",
    "LEGACY_BUSINESS_DATABASES",
    "LEGACY_BUSINESS_TEXT_FILES",
    "LEGACY_DEVICE_STATE_FILES",
    "LEGACY_UNBOUND_REASON",
    "LEGACY_USER_KEYS_DIR",
    "SQLITE_AUXILIARY_SUFFIXES",
    "home_account_conflict_message",
    "legacy_business_data_present",
    "normalize_issuer",
    "read_home_binding",
]
