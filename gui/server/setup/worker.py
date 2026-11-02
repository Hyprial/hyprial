"""Trusted first-run adapter for the public daemon request port.

The GUI host chooses this file, Python executable and HYPRIAL_HOME. Browser
input can select only one of the fixed actions below; it cannot select a daemon
method, actor, instance home or command line. stdout contains public receipts
only. In particular, a provider key is never included in a result or error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _request(action: str, payload: dict[str, object]) -> dict[str, object]:
    from hyprial.shell import daemon_request, daemon_request_once

    def read(method: str, params: dict[str, object]) -> dict[str, object]:
        result = daemon_request(method, params)
        if not isinstance(result, dict):
            raise ValueError("invalid daemon response")
        return result

    def write(method: str, params: dict[str, object]) -> dict[str, object]:
        result = daemon_request_once(method, params)
        if not isinstance(result, dict):
            raise ValueError("invalid daemon response")
        return result

    if action == "inspect":
        agents = read("agent.list", {}).get("agents")
        if not isinstance(agents, list):
            raise ValueError("invalid agent list")
        return {"ok": True, "agents": [agent for agent in agents if isinstance(agent, dict) and agent.get("actor") == "squire"]}

    if action == "save-key":
        entry = payload.get("entry")
        key = payload.get("key")
        if not isinstance(entry, str) or not re.fullmatch(r"gui-deepseek-[0-9a-f-]{36}", entry):
            raise ValueError("invalid entry")
        if not isinstance(key, str) or not 0 < len(key) <= 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in key):
            raise ValueError("invalid key")
        write("agent.secret.provider-write", {"entryId": entry, "fieldName": "key", "value": key})
        return {"ok": True}

    if action == "create":
        result = write("agent.create", {"name": "squire", "provider": "deepseek", "model": "deepseek-flash", "preferredHarness": "pi"})
        return {"ok": True, "created": result.get("created"), "agent": result.get("agent")}

    if action in {"configure", "start"}:
        from hyprial.daemon import SquireRegistryResult, SquireSetup, derive_setup_identity, resolve_node_owner

        class Port:
            def ensure_squire(self, command):
                result = write("management.squire.ensure", command.to_payload())
                return SquireRegistryResult.from_payload(result)

        home = Path(os.environ["HYPRIAL_HOME"])
        if not home.is_absolute():
            raise ValueError("invalid instance home")
        identity = derive_setup_identity(resolve_node_owner(hyprial_home=home))
        setup = SquireSetup(hyprial_home=home, state_dir=home / "state", management_port=Port())
        result = setup.run(identity, squire_home=home / "legacy-squire-disabled", provider="deepseek", model="deepseek-flash", preferred_harness="pi", start=action == "start", step="create-squire")
        return {"ok": True, "squire": result.get("squire"), "started": result.get("worker", {}).get("started", False)}

    if action == "grant":
        entry = payload.get("entry")
        if not isinstance(entry, str) or not re.fullmatch(r"gui-deepseek-[0-9a-f-]{36}", entry):
            raise ValueError("invalid entry")
        grants = read("agent.secret.list", {"actor": "squire"}).get("grants")
        if not isinstance(grants, list):
            raise ValueError("invalid grant list")
        wanted = {"source": "user-provider", "entryId": entry, "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"]}
        for grant in grants:
            if isinstance(grant, dict) and grant.get("grantId") == "gui-first-run-pi-deepseek":
                if all(grant.get(k) == v for k, v in wanted.items()):
                    return {"ok": True, "alreadyGranted": True}
                return {"ok": False, "code": "PROVIDER_CREDENTIAL_EXISTS", "definitive": True}
        write("agent.secret.grant", {"actor": "squire", "grantId": "gui-first-run-pi-deepseek", **wanted, "revision": 1})
        return {"ok": True}

    return {"ok": False, "code": "INVALID_ACTION"}


def main() -> None:
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action not in {"inspect", "save-key", "create", "configure", "grant", "start"}:
        result = {"ok": False, "code": "INVALID_ACTION"}
    else:
        raw = sys.stdin.read(8193)
        if len(raw) > 8192:
            result = {"ok": False, "code": "INVALID_INPUT"}
        else:
            payload = json.loads(raw or "{}")
            result = _request(action, payload) if isinstance(payload, dict) else {"ok": False, "code": "INVALID_INPUT"}
    print(json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = getattr(error, "code", "SETUP_COMMAND_FAILED")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
            code = "SETUP_COMMAND_FAILED"
        print(json.dumps({"ok": False, "code": code}), flush=True)
        sys.exit(1)
