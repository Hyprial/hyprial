"""Shared verified-identity and selected-home helpers for workflow CLI work."""

from __future__ import annotations

import os
from pathlib import Path
import stat

from hyprial.identity import PAC_OWNER_UNKNOWN, PAC_PRINCIPAL_UNVERIFIED, PacError
from hyprial.kernel import canonical_user_uri


def state_dir() -> Path:
    configured = os.environ.get("HARNESS_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    from hyprial.kernel import configured_hyprial_home

    home, _source = configured_hyprial_home()
    return home / "state"


def actor_owner() -> str:
    from hyprial.daemon import resolve_node_owner
    from hyprial.kernel import configured_hyprial_home

    home, _source = configured_hyprial_home()
    try:
        return canonical_user_uri(resolve_node_owner(hyprial_home=home))
    except ValueError as error:
        raise PacError(PAC_OWNER_UNKNOWN, str(error)) from error


def _file_session_ref(raw_path: str) -> str:
    """Read one carrier-owned session ref without following unsafe files."""

    path = Path(raw_path)
    try:
        if not path.is_absolute():
            raise ValueError("path must be absolute")
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow and path.is_symlink():
            raise ValueError("path must not be a symlink")
        descriptor = os.open(path, os.O_RDONLY | no_follow)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("path is not a regular file")
            effective_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
            if metadata.st_uid != effective_uid or metadata.st_mode & 0o077:
                raise ValueError("file must be owned by this user and mode 0600")
            if metadata.st_size > 4096:
                raise ValueError("file is too large")
            session_ref = stream.read(4097).strip()
        if not session_ref or any(character.isspace() for character in session_ref):
            raise ValueError("file must contain one non-empty session ref")
        return session_ref
    except (OSError, UnicodeError, ValueError) as error:
        raise PacError(
            PAC_PRINCIPAL_UNVERIFIED,
            f"interactive agent session binding is unreadable or unsafe: {error}",
        ) from error


def worker_binding() -> tuple[str, str] | None:
    actor = os.environ.get("HYPRIAL_WORKER_ACTOR")
    session_ref = os.environ.get("HYPRIAL_WORKER_SESSION_REF")
    session_ref_file = os.environ.get("HYPRIAL_WORKER_SESSION_REF_FILE")
    if session_ref and session_ref_file:
        raise PacError(
            PAC_PRINCIPAL_UNVERIFIED,
            "agent session has conflicting direct and file-backed bindings",
        )
    if session_ref_file:
        session_ref = _file_session_ref(session_ref_file)
    if actor and session_ref:
        return actor, session_ref
    marker = os.environ.get("HYPRIAL_MANAGED_WORKER") or os.environ.get(
        "HYPRIAL_AGENT_SESSION"
    )
    if actor or session_ref or session_ref_file or marker:
        missing = [
            name
            for name, value in (
                ("HYPRIAL_WORKER_ACTOR", actor),
                ("HYPRIAL_WORKER_SESSION_REF or HYPRIAL_WORKER_SESSION_REF_FILE", session_ref),
            )
            if not value
        ]
        raise PacError(
            PAC_PRINCIPAL_UNVERIFIED,
            "agent session context without its complete binding "
            f"(missing {', '.join(missing)}); refusing to write under the "
            "human identity -- the carrier must inject the full binding",
        )
    return None


def check_actor_claim(actor_claim: str | None, verified: str) -> None:
    if actor_claim is not None and actor_claim != verified:
        raise PacError(
            PAC_PRINCIPAL_UNVERIFIED,
            f"--actor {actor_claim!r} disagrees with the verified acting "
            f"principal {verified!r}; the workflow surface trusts the verified identity",
            {"claimed": actor_claim, "verified": verified},
        )
