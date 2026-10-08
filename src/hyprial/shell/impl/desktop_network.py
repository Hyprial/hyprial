"""Desktop onboarding adapter; credentials stay in the private Hyprial home.

Tailnet cutover (§4.4): the join stage and the tsnet sidecar consent flow
are gone.  ``status`` reports identity + device readiness; ``onboard`` runs
the OIDC identity stage (action ``login``) or only the device-key stage
(action ``device``).  A missing/failing Tailcat sidecar never rolls the
identity back (D12): the login action still succeeds and reports
``device.ready: false`` with the typed error.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from hyprial.kernel import resolve_node_id

from hyprial.shell.impl.login.flow import LoginError, read_login_credential, run_login
from hyprial.daemon import resolve_profile
from hyprial.identity import read_device_record
from hyprial.daemon import (
    TailcatSidecarError,
    ensure_device_key,
    locate_tailcat_sidecar,
)

_SIDECAR_NEXT_STEP = (
    "install the hyprial-tailcat sidecar (build sidecar/hyprial-tailcat "
    "into $HYPRIAL_HOME/bin or set HYPRIAL_TAILCAT_BINARY), then retry the "
    "device step; the identity stays committed"
)


def _device_id() -> str:
    """The same value the daemon announces as its node id."""

    return resolve_node_id()


def _settings_owner(home: Path) -> str | None:
    try:
        settings = json.loads((home / "settings.json").read_text())
        owner = settings.get("owner") if isinstance(settings, dict) else None
        if isinstance(owner, str) and owner.strip():
            return owner
    except (OSError, ValueError):
        pass
    return None


def status(home: Path) -> dict:
    profile, source = resolve_profile(hyprial_home=home)
    owner = _settings_owner(home)
    authenticated = False
    try:
        credential = read_login_credential(hyprial_home=home)
        authenticated = bool(owner and credential.issuer == profile.issuer)
    except LoginError:
        pass
    try:
        locate_tailcat_sidecar(home)
        sidecar_error = None
    except TailcatSidecarError as error:
        sidecar_error = error.code
    record = read_device_record(home)
    device_ready = record is not None and (owner is None or record.owner == owner)
    return {
        "owner": owner,
        "authenticated": authenticated,
        "deviceReady": device_ready,
        "sidecarError": sidecar_error,
        "ready": authenticated and device_ready and sidecar_error is None,
        "profile": profile.as_record(),
        "profileSource": source,
    }


def onboard(home: Path, action: str, emit: Callable[[str, dict], None]) -> dict:
    if action not in {"login", "device"}:
        raise ValueError("Unsupported desktop onboarding action")
    if os.environ.get("HYPRIAL_OWNER"):
        raise ValueError("Desktop login must not inherit an owner override")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile, source = resolve_profile(hyprial_home=home)
    if action == "login":
        run_login(profile, profile_source=source, hyprial_home=home,
                  state_dir=home / "state", identity_mode="casdoor",
                  identity_issuer=profile.issuer, open_browser=False, emit=emit)
    current = status(home)
    if not current["authenticated"]:
        return {"ok": False, "code": "NOT_LOGGED_IN", "state": current}
    try:
        record = ensure_device_key(
            home, owner=current["owner"], device_id=_device_id()
        )
    except TailcatSidecarError as error:
        device = {
            "ready": False,
            "error": {"code": error.code, "message": str(error)},
            "nextStep": _SIDECAR_NEXT_STEP,
        }
        # D12: the identity commit is never rolled back; only the explicit
        # device action reports the device failure as its own result.
        return {"ok": action == "login", "device": device, "state": status(home)}
    device = {
        "ready": True,
        "deviceId": record.device_id,
        "keyGeneration": record.key_generation,
    }
    return {"ok": True, "device": device, "state": status(home)}
