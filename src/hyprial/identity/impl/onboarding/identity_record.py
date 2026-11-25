"""The one settings-identity record authority for identity and daemon.

Decision: these pure record functions moved here from the daemon configuration
package.  The daemon keeps thin delegates so its public surface and on-disk
format are unchanged, while the credential worker and offline identity readers
use the same validation and partial-commit semantics.  Strict admission reads
distinguish a missing record from an unreadable, corrupt, or residual identity
record and never coerce or clean one; the legacy lenient CLI reader remains for
compatibility.  This module never reads an OIDC credential or touches the
network.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hyprial.kernel import atomic_json_write
from hyprial.kernel import configured_hyprial_home
from hyprial.kernel import is_identity_id_segment
from hyprial.kernel import is_user_id_segment

from .ports import (
    HomeBinding,
    IdentityAdmission,
    IdentityRecordError,
    IdentitySnapshot,
    OnboardingSubject,
)

IDENTITY_RESIDUE_FIELDS = (
    "identityIssuer",
    "identityMode",
    "userId",
    "username",
)


def _settings_path(env: Mapping[str, str], hyprial_home: Path | None) -> Path:
    if hyprial_home is not None:
        return Path(hyprial_home) / "settings.json"
    home = configured_hyprial_home(env)[0]
    return home / "settings.json"


def settings_path(
    environ: Mapping[str, str], hyprial_home: Path | None
) -> Path:
    """The settings file this resolution reads, from the same env mapping."""

    return _settings_path(environ, hyprial_home)


def _settings_owner(path: Path) -> str | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    owner = record.get("owner")
    if owner is None:
        return None
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError(f"{path} owner must be a non-empty string; got {owner!r}")
    return owner.strip()


def node_owner_or_none(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> str | None:
    """Optional node identity with the same policy as ``resolve_node_owner``."""

    env = os.environ if environ is None else environ
    owner = env.get("HYPRIAL_OWNER", "").strip()
    if owner and is_identity_id_segment(owner.lower()):
        raise ValueError("HYPRIAL_OWNER must not have an exact identity id shape")
    if not owner:
        owner = _settings_owner(_settings_path(env, hyprial_home))
    if owner is not None and ":" in owner:
        raise ValueError(f"owner must not contain ':'; got {owner!r}")
    return owner


def _settings_user_id(path: Path) -> str | None:
    record = _strict_record(path)
    if record is None:
        return None
    value = record.get("userId")
    if value is None:
        return None
    if not isinstance(value, str):
        raise IdentityRecordError(
            "IDENTITY_METADATA_INVALID",
            "settings userId is not a string",
            {"path": str(path), "field": "userId"},
        )
    return value.strip() or None


def resolve_node_user_id(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> str | None:
    """The Casdoor ``sub`` the cell authority ``user:u.<sub>`` is built from.

    Source order mirrors ``resolve_node_owner``: ``HYPRIAL_USER_ID`` >
    ``settings.json`` ``userId`` > None.  When the ``HYPRIAL_OWNER`` override
    is in effect without ``HYPRIAL_USER_ID``, the answer is None: the settings
    file may belong to another account than the overriding owner.  A value
    whose ``u.<value>`` is not an exact user-id segment raises
    ``IdentityRecordError`` instead of falling back to anything.
    """

    env = os.environ if environ is None else environ
    value = env.get("HYPRIAL_USER_ID", "").strip() or None
    source = "HYPRIAL_USER_ID"
    if value is None:
        if env.get("HYPRIAL_OWNER", "").strip():
            return None
        value = _settings_user_id(_settings_path(env, hyprial_home))
        source = "userId"
    if value is None:
        return None
    if not is_user_id_segment("u." + value):
        raise IdentityRecordError(
            "IDENTITY_METADATA_INVALID",
            f"{source} is not a user id: u.<value> must be an exact user-id segment",
            {"field": source},
        )
    return value


def resolve_node_owner(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> str:
    """The **user identity** this daemon mints into agent/adapter URIs.

    Source order: ``HYPRIAL_OWNER`` > ``settings.json`` ``owner`` > loud
    failure.  ``settings.json`` is the one landing spot.  The manual
    pre-login owner flags were removed with U5 (D-U2-5); the ways in are
    exactly two: ``hyprial login`` (the Hyprial service, login U2) and the
    ``HYPRIAL_OWNER`` override below.

    ⭐ D7, pinned by U5: ``HYPRIAL_OWNER`` is an **override for tests and
    managed deployments only**.  It outranks settings for the daemon, so a
    ``hyprial login`` whose identity differs from a non-empty override errors
    **before writing anything** — writing settings would not change the
    daemon's real identity.

    ⚠️ Both surfaces below must keep naming **real** commands.
    ``test_identity_guidance.py`` asserts every command named **in this
    docstring and in the failure message below** exists -- it reads both
    surfaces, so adding a command here puts it under the check too.  See
    that test before rewording.

    ⛔ There is deliberately **no local-login fallback**.  This function used
    to end in ``getpass.getuser()``, which returns the *host* login and so
    made the owner segment a property of the machine rather than of the
    person — the exact defect this signature exists to remove.  The owner
    segment is a user identity: **one value across all of a user's machines**,
    with ``<machine>`` doing the per-node work.

    ⚠️ Consequence, and it is load-bearing: because owner is now identical
    across a user's machines, ``<owner>`` alone no longer distinguishes
    nodes.  Anything asking "is this actor mine?" must compare ``<machine>``
    as well — see ``_resolve_agent_alias``, which this change would otherwise
    have made ambiguous for every actor name the peer also uses.
    """

    env = os.environ if environ is None else environ
    owner = node_owner_or_none(env, hyprial_home=hyprial_home)
    if owner is None:
        path = _settings_path(env, hyprial_home)
        raise ValueError(
            "user identity is not set: HYPRIAL_OWNER is unset or empty, and "
            f"{path} has no 'owner'. Run: hyprial login "
            "(or set HYPRIAL_OWNER)"
        )
    return owner


def write_settings_owner(
    owner: str,
    *,
    user_id: str | None = None,
    username: str | None = None,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> Path:
    """Persist ``owner`` into ``settings.json`` and return the file written."""

    value = owner.strip()
    if not value:
        raise ValueError("owner must not be empty")
    if ":" in value:
        raise ValueError(f"owner must not contain ':'; got {owner!r}")
    if is_identity_id_segment(value.lower()):
        raise ValueError("owner must not have an exact identity id shape")
    env = os.environ if environ is None else environ
    path = _settings_path(env, hyprial_home)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    if (
        record.get("owner") == value
        and (user_id is None or record.get("userId") == user_id)
        and (username is None or record.get("username") == username)
    ):
        return path
    record["owner"] = value
    if user_id is not None:
        record["userId"] = user_id
    if username is not None:
        record["username"] = username
    atomic_json_write(path, record)
    return path


def _symlink_error(path: Path) -> IdentityRecordError:
    return IdentityRecordError(
        "IDENTITY_SETTINGS_SYMLINK",
        f"settings record is a symlink; refusing identity admission: {path}",
        {"path": str(path)},
    )


def _unreadable_error(
    path: Path, error: BaseException | None = None
) -> IdentityRecordError:
    detail = f" ({type(error).__name__})" if error is not None else ""
    return IdentityRecordError(
        "IDENTITY_SETTINGS_UNREADABLE",
        f"settings record is unreadable; refusing identity admission: {path}{detail}",
        {"path": str(path)},
    )


def _settings_text(path: Path) -> str | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _unreadable_error(path, error) from error
    if stat.S_ISLNK(before.st_mode):
        raise _symlink_error(path)
    if not stat.S_ISREG(before.st_mode):
        raise _unreadable_error(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno is not None and error.errno == getattr(errno, "ELOOP", -1):
            raise _symlink_error(path) from error
        raise _unreadable_error(path, error) from error
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise _unreadable_error(path)
            return stream.read()
    except UnicodeError as error:
        raise IdentityRecordError(
            "IDENTITY_SETTINGS_CORRUPT",
            f"settings record is not UTF-8 JSON; refusing identity admission: {path}",
            {"path": str(path)},
        ) from error
    except IdentityRecordError:
        raise
    except OSError as error:
        raise _unreadable_error(path, error) from error


def _strict_record(path: Path) -> dict[str, object] | None:
    text = _settings_text(path)
    if text is None:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError as error:
        raise IdentityRecordError(
            "IDENTITY_SETTINGS_CORRUPT",
            f"settings record is not valid JSON; refusing identity admission: {path}",
            {"path": str(path)},
        ) from error
    if not isinstance(record, dict):
        raise IdentityRecordError(
            "IDENTITY_SETTINGS_NOT_OBJECT",
            f"settings record top level is not an object; refusing admission: {path}",
            {"path": str(path)},
        )
    return record


def _strict_snapshot(record: Mapping[str, object], path: Path) -> IdentitySnapshot:
    residue = tuple(
        name for name in IDENTITY_RESIDUE_FIELDS if name in record
    )
    if "owner" not in record:
        if residue:
            raise IdentityRecordError(
                "IDENTITY_RESIDUE_PRESENT",
                "settings record has identity residue without an owner; "
                "refusing identity admission",
                {"path": str(path), "residueFields": list(residue)},
            )
        return IdentitySnapshot()
    owner = record.get("owner")
    data: dict[str, object] = {"path": str(path)}
    if residue:
        data["residueFields"] = list(residue)
    if not isinstance(owner, str) or not owner.strip():
        raise IdentityRecordError(
            "IDENTITY_OWNER_INVALID",
            "settings owner is not a non-empty string; refusing identity admission",
            data,
        )
    value = owner.strip()
    if ":" in value or is_identity_id_segment(value.lower()):
        raise IdentityRecordError(
            "IDENTITY_OWNER_INVALID",
            "settings owner is not a usable user identity; refusing admission",
            data,
        )
    mode = record.get("identityMode")
    if mode is not None and not isinstance(mode, str):
        raise IdentityRecordError(
            "IDENTITY_METADATA_INVALID",
            "settings identity mode is not a string; refusing identity admission",
            {"path": str(path), "field": "identityMode"},
        )
    issuer = record.get("identityIssuer")
    if issuer is not None and not isinstance(issuer, str):
        raise IdentityRecordError(
            "IDENTITY_METADATA_INVALID",
            "settings identity issuer is not a string; refusing identity admission",
            {"path": str(path), "field": "identityIssuer"},
        )
    user_id = record.get("userId")
    if user_id is not None and not isinstance(user_id, str):
        raise IdentityRecordError(
            "IDENTITY_METADATA_INVALID",
            "settings userId is not a string; refusing identity admission",
            {"path": str(path), "field": "userId"},
        )
    return IdentitySnapshot(owner=value, mode=mode, issuer=issuer, user_id=user_id)


def read_identity_admission(home: Path) -> IdentityAdmission:
    """Strictly classify one explicit home for bootstrap admission.

    A missing settings file returns ``present=False`` and an empty snapshot.
    An existing record without an owner and without known identity residue is
    present but ownerless.  Unreadable, corrupt, non-object, invalid-owner,
    invalid-metadata, symlinked, and residual records raise
    ``IdentityRecordError`` without writing or cleaning anything.  This reader
    is secret-free: it never opens ``secrets/login.json``.
    """

    path = Path(home) / "settings.json"
    record = _strict_record(path)
    if record is None:
        return IdentityAdmission(present=False, snapshot=IdentitySnapshot())
    return IdentityAdmission(
        present=True, snapshot=_strict_snapshot(record, path)
    )


def read_identity_snapshot(home: Path) -> IdentitySnapshot:
    """Return the strict owner/mode/issuer snapshot for one explicit home."""

    return read_identity_admission(home).snapshot


def read_onboarding_identity(
    home: Path,
) -> tuple[IdentitySnapshot, Any, OnboardingSubject | None]:
    """Return one consistent trusted identity view for onboarding.

    The settings record, device record and narrow receipt partition are read
    once so a caller cannot combine an account from one generation with a
    receipt from another.  The device owner must match the settings owner before
    a partition key exists; a mismatch fails closed rather than selecting a
    shared or owner-derived partition.  The result is never serialized to the
    GUI.
    """

    path = Path(home) / "settings.json"
    record = _strict_record(path)
    if record is None:
        return IdentitySnapshot(), None, None
    snapshot = _strict_snapshot(record, path)
    from hyprial.identity.impl.device.store import read_device_record

    device = read_device_record(Path(home))
    return snapshot, device, _onboarding_subject(record, snapshot, device, path)


def _onboarding_subject(
    record: Mapping[str, object],
    snapshot: IdentitySnapshot,
    device: Any,
    path: Path,
) -> OnboardingSubject | None:
    if snapshot.owner is None or snapshot.issuer is None:
        return None
    user_id = record.get("userId")
    if user_id is None:
        return None
    if not isinstance(user_id, str) or not user_id.strip():
        raise IdentityRecordError(
            "IDENTITY_METADATA_INVALID",
            "settings userId is not a non-empty string; refusing progress partition",
            {"path": str(path), "field": "userId"},
        )
    if device is None or device.device_uid is None:
        return None
    if getattr(device, "owner", None) != snapshot.owner:
        return None
    return OnboardingSubject(
        issuer=snapshot.issuer,
        user_id=user_id.strip(),
        device_uid=device.device_uid,
    )


def read_onboarding_subject(home: Path) -> OnboardingSubject | None:
    """Return the narrow server-side partition key, or ``None`` if unstable.

    Only the onboarding service may use this reader to select one local receipt
    partition.  The result is never serialized to the GUI.  A missing settings,
    issuer, user id or device uid means there is no stable subject: callers must
    not fall back to owner, username, session or a shared empty partition.
    Corrupt settings or corrupt device records fail loudly and stay untouched;
    a device owner that does not match the settings owner yields no subject.
    """

    return read_onboarding_identity(home)[2]


def read_settings_identity(
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> tuple[str, str | None, str | None] | None:
    """Return the persisted owner/mode/issuer tuple exactly as committed."""

    env = os.environ if environ is None else environ
    path = _settings_path(env, hyprial_home)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    owner = record.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return None
    mode = record.get("identityMode")
    issuer = record.get("identityIssuer")
    return (
        owner.strip(),
        mode if isinstance(mode, str) else None,
        issuer if isinstance(issuer, str) else None,
    )


def read_strict_settings_record(home: Path) -> dict[str, object] | None:
    """Strictly read settings.json for ``home`` without coercing errors."""

    return _strict_record(_settings_path({}, Path(home)))


def read_settings_username(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> str | None:
    """Return the persisted display username, never reading ``homeAccount``."""

    env = os.environ if environ is None else environ
    target_home = home if hyprial_home is None else hyprial_home
    path = _settings_path(env, target_home)
    try:
        record = _strict_record(path)
    except IdentityRecordError:
        return None
    if record is None:
        return None
    username = record.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip()
    return None


def read_settings_identity_metadata(
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> tuple[str | None, str | None]:
    """Return the configured mode/issuer without inventing legacy metadata."""

    identity = read_settings_identity(environ=environ, hyprial_home=hyprial_home)
    return (identity[1], identity[2]) if identity is not None else (None, None)


def write_settings_identity(
    owner: str,
    *,
    mode: str,
    issuer: str | None,
    user_id: str | None = None,
    username: str | None = None,
    home_account: HomeBinding | None = None,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> Path:
    """Atomically commit owner/mode/issuer as one settings record."""

    value = owner.strip()
    if not value or ":" in value or is_identity_id_segment(value.lower()):
        raise ValueError(
            "owner must be non-empty, contain no ':', and not have an exact "
            f"identity id shape; got {owner!r}"
        )
    if mode not in {"casdoor", "local-usage"}:
        raise ValueError(f"unsupported identity mode {mode!r}")
    if mode == "casdoor" and (not isinstance(issuer, str) or not issuer.strip()):
        raise ValueError("casdoor identity requires a non-empty issuer")
    if mode == "local-usage" and issuer not in {None, ""}:
        raise ValueError(f"{mode} identity must not retain an issuer")
    env = os.environ if environ is None else environ
    path = _settings_path(env, hyprial_home)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    desired_issuer = issuer.strip() if isinstance(issuer, str) else None
    desired_home_account = (
        home_account.as_dict() if home_account is not None else None
    )
    if (
        record.get("owner") == value
        and record.get("identityMode") == mode
        and record.get("identityIssuer") == desired_issuer
        and (user_id is None or record.get("userId") == user_id)
        and (username is None or record.get("username") == username)
        and (
            desired_home_account is None
            or record.get("homeAccount") == desired_home_account
        )
    ):
        return path
    record["owner"] = value
    record["identityMode"] = mode
    record["identityIssuer"] = desired_issuer
    if user_id is not None:
        record["userId"] = user_id
    if username is not None:
        record["username"] = username
    if desired_home_account is not None:
        record["homeAccount"] = desired_home_account
    atomic_json_write(path, record)
    return path


__all__ = [
    "IDENTITY_RESIDUE_FIELDS",
    "IdentityAdmission",
    "IdentityRecordError",
    "node_owner_or_none",
    "read_identity_admission",
    "read_identity_snapshot",
    "read_onboarding_subject",
    "read_settings_identity",
    "read_settings_identity_metadata",
    "read_settings_username",
    "read_strict_settings_record",
    "resolve_node_owner",
    "settings_path",
    "write_settings_identity",
    "write_settings_owner",
]
