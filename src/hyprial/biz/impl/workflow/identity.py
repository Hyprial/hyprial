"""Shared verified-identity and selected-home helpers for workflow CLI work."""

from __future__ import annotations

import os
from pathlib import Path

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


def worker_binding() -> tuple[str, str] | None:
    actor = os.environ.get("HYPRIAL_WORKER_ACTOR")
    session_ref = os.environ.get("HYPRIAL_WORKER_SESSION_REF")
    if actor and session_ref:
        return actor, session_ref
    marker = os.environ.get("HYPRIAL_MANAGED_WORKER")
    if actor or session_ref or marker:
        missing = [
            name
            for name, value in (
                ("HYPRIAL_WORKER_ACTOR", actor),
                ("HYPRIAL_WORKER_SESSION_REF", session_ref),
            )
            if not value
        ]
        raise PacError(
            PAC_PRINCIPAL_UNVERIFIED,
            "managed-worker context without its session binding "
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
