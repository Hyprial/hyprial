"""Private stdio adapter. OIDC and durable credentials remain owned by Hyprial.

Launched only by the local GUI host with a fixed executable/home, never with
browser-supplied arguments. stdout is an allowlisted protocol; stderr is not
exposed to the browser. No daemon lifecycle or account switching occurs here.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def write(kind: str, value: dict) -> None:
    print(json.dumps({"type": kind, "data": value}), flush=True)


def main() -> None:
    from hyprial.shell import desktop_network

    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action not in {"status", "login", "device"}:
        write("result", {"ok": False, "code": "INVALID_ACTION"})
        return
    home = Path(os.environ["HYPRIAL_HOME"])
    if not home.is_absolute():
        raise ValueError("A host-selected absolute home is required")
    current = desktop_network.status(home)
    if action == "status" or (action == "login" and current["authenticated"]):
        write("result", {"ok": True, "state": current})
        return
    if action == "device" and not current["authenticated"]:
        write("result", {"ok": False, "code": "NOT_LOGGED_IN", "state": current})
        return
    profile = current.get("profile") if isinstance(current, dict) else None
    issuer = profile.get("issuer") if isinstance(profile, dict) else None

    def emit(event: str, data: dict) -> None:
        # The configured issuer is public profile metadata, not a token. The
        # GUI host uses its host to prove an authorization URL is trusted.
        payload = {"event": event, **data}
        if isinstance(issuer, str):
            payload["issuer"] = issuer
        write("progress", payload)

    result = desktop_network.onboard(home, action, emit)
    write("result", result)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # No exception text/traceback: provider errors can contain credentials.
        value = getattr(error, "code", "AUTH_FAILED")
        write("result", {"ok": False, "code": value if isinstance(value, str) else "AUTH_FAILED"})
        sys.exit(1)
