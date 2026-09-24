"""Shared verified-identity and selected-home helpers for workflow CLI work."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from hyprial.home import HYPRIALHomeNotInitialized, require_initialized_hyprial_home
from hyprial.pac.errors import PAC_OWNER_UNKNOWN, PAC_PRINCIPAL_UNVERIFIED, PacError
from hyprial.uri import canonical_user_uri


def state_dir() -> Path:
    configured = os.environ.get("HARNESS_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    from hyprial.home import configured_hyprial_home

    home, _source = configured_hyprial_home()
    return home / "state"


def actor_owner() -> str:
    from hyprial.daemon.identity import resolve_node_owner
    from hyprial.home import configured_hyprial_home

    home, _source = configured_hyprial_home()
    try:
        return canonical_user_uri(resolve_node_owner(hyprial_home=home))
    except ValueError as error:
        raise PacError(PAC_OWNER_UNKNOWN, str(error)) from error


def worker_binding(json_output: bool) -> tuple[str, str] | None:
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
        fail(
            PacError(
                PAC_PRINCIPAL_UNVERIFIED,
                "managed-worker context without its session binding "
                f"(missing {', '.join(missing)}); refusing to write under the "
                "human identity -- the carrier must inject the full binding",
            ),
            json_output,
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


def guard(json_output: bool) -> None:
    try:
        require_initialized_hyprial_home()
    except HYPRIALHomeNotInitialized as error:
        if json_output:
            typer.echo(json.dumps({"ok": False, "code": error.code, "error": str(error)}))
        else:
            typer.echo(f"hyprial: {error}", err=True)
        raise typer.Exit(code=1) from None


def emit(document: dict[str, Any], json_output: bool, human: str | None = None) -> None:
    if json_output:
        typer.echo(json.dumps(document, ensure_ascii=False))
    elif human:
        typer.echo(human)
    else:
        typer.echo(json.dumps(document, ensure_ascii=False, indent=2))


def fail(error: PacError, json_output: bool) -> None:
    document: dict[str, Any] = {"ok": False, "code": error.code, "error": str(error)}
    if error.data:
        document["data"] = error.data
    if json_output:
        typer.echo(json.dumps(document, ensure_ascii=False))
    else:
        typer.echo(f"hyprial workflow: {error.code}: {error}", err=True)
    raise typer.Exit(code=1)
