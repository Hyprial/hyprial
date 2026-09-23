"""Desktop onboarding adapter; credentials stay in the private Hyprial home."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from hyprial.login import LoginError, read_login_credential, run_login
from hyprial.network_profile import resolve_profile
from hyprial.tsnet_join import run_join
from hyprial.tsnet_sidecar import SidecarError, verify_installed_sidecar


def status(home: Path) -> dict:
    profile, source = resolve_profile(hyprial_home=home)
    owner = None
    try:
        settings = json.loads((home / "settings.json").read_text())
        owner = settings.get("owner") if isinstance(settings, dict) else None
        if not isinstance(owner, str) or not owner.strip():
            owner = None
    except (OSError, ValueError):
        pass
    authenticated = False
    try:
        credential = read_login_credential(hyprial_home=home)
        authenticated = bool(owner and credential.issuer == profile.issuer)
    except LoginError:
        pass
    try:
        _, sidecar_error = verify_installed_sidecar(home)
    except SidecarError as error:
        sidecar_error = error.code
    joined = False
    try:
        node = json.loads((home / "state/tsnet/node.json").read_text())
        expected_node = os.environ.get("HYPRIAL_NODE_ID")
        joined = bool(isinstance(node, dict) and isinstance(node.get("hostname"), str)
            and node["hostname"] and (not expected_node or node["hostname"].split(".")[0] == expected_node)
            and node.get("controlUrl") == (
            "" if profile.control_plane_kind == "tailscale" else profile.control_plane_url
        ) and (home / "state/tsnet/node").is_dir())
    except (OSError, ValueError):
        pass
    return {"owner": owner, "authenticated": authenticated, "joined": joined,
            "ready": authenticated and joined and sidecar_error is None,
            "sidecarError": sidecar_error, "profile": profile.as_record(), "profileSource": source}


def onboard(home: Path, action: str, emit: Callable[[str, dict], None]) -> dict:
    if action not in {"login", "join"}:
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
    if current["sidecarError"]:
        return {"ok": False, "code": current["sidecarError"], "state": current}
    outcome = run_join(profile, hyprial_home=home, open_browser=False,
                       emit=lambda kind, data: emit("network-" + kind, data))
    return {"ok": outcome.status == "joined", "network": outcome.as_network(), "state": status(home)}
