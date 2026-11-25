"""Land a bundle through a daemon application's own agent registry.

Decision: the retired ``transfer.land`` handler is not reproduced.  This is the
call the teleport target makes (``land_bundle_into_registry`` with the daemon's
registry, home and a fresh staging directory under the state dir), without its
wire error mapping: a refusal is the landing code's own exception, one of
``LANDING_REFUSALS``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.daemon.impl.transfer.archive.bundle import BundleError
from hyprial.daemon.impl.transfer.landing.credentials import CredentialLandingError
from hyprial.daemon.impl.transfer.landing.registry import (
    LandingError,
    land_bundle_into_registry,
)
from hyprial.kernel import canonical_user_uri

LANDING_REFUSALS = (
    LandingError,
    BundleError,
    CredentialLandingError,
)

STAGING_DIRNAME = "transfer-landing"


def staging_root(app: Any) -> Path:
    return Path(app.state_dir) / STAGING_DIRNAME


def land_in_daemon(
    app: Any, bundle_dir: Path | str, *, actor: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    result = land_bundle_into_registry(
        str(bundle_dir),
        registry=app.agents,
        home_root=app.hyprial_home / "agents",
        machine_home=app.hyprial_home,
        staging=staging_root(app) / uuid4().hex,
        granted_by=canonical_user_uri(app.owner),
        expected_actor=actor,
        dry_run=dry_run,
    )
    return {"ok": True, **result.as_dict()}
