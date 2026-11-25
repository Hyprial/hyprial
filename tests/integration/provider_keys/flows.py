"""The two end-to-end owner-key flows, each asserting its outcomes step by step.

``cli_flow``: the operator CLI through the IPC registry into the real service and
the managed-worker launch composition. ``gui_flow``: the legacy GUI-style
``agent.secret.*`` calls through ``DaemonApplication._handle_agent``, a restart
and the record-only recovery of the projected rows.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from hyprial.identity import SecretResolutionError
from hyprial.kernel import ipc_errors
from .support import (
    CLI_SECRET,
    ENV,
    GUI_SECRET,
    LEGACY_SECRET,
    close_daemon,
    cli_json,
    compose,
    ipc_app,
    legacy_user_grant,
    owner_daemon,
)

GRANT_ID = "gui-first-run-pi-deepseek"


def _env(world: Any, app: Any) -> dict[str, str]:
    return dict(compose(world, app).environment.for_exec())


def cli_flow(world: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """login -> list -> allow -> status ok -> launch injects -> revoke -> launch refuses -> logout."""
    app = ipc_app(world)
    legacy_user_grant(world, "deepseek", "deepseek")
    before = _env(world, app)
    assert before[ENV] == LEGACY_SECRET

    login = cli_json(app, monkeypatch, ["user", "provider", "login", "deepseek", "--api-key-stdin",
                                        "--label", "e2e", "--env", ENV], stdin=CLI_SECRET + "\n")
    assert login["_sent"] == [("user.provider.login", {
        "provider": "deepseek", "kind": "api-key", "secret": CLI_SECRET, "label": "e2e",
        "environmentNames": [ENV]})]
    key = login["key"]
    assert login["ok"] is True and key["state"] == "active" and key["provider"] == "deepseek"
    assert key["dekVersion"] == 1 and key["environmentNames"] == [ENV] and key["grants"] == []

    listed = cli_json(app, monkeypatch, ["user", "provider", "list"])
    assert [(row["keyId"], row["state"], row["label"]) for row in listed["keys"]] == \
        [(key["keyId"], "active", "e2e")]
    assert listed["invalid"] == []

    allowed = cli_json(app, monkeypatch, ["agent", "provider", "allow", "--actor", "worker",
                                          "--provider", "deepseek"])
    assert allowed["_sent"] == [("agent.provider.allow", {"agent": "worker", "provider": "deepseek"})]
    grant = allowed["grant"]
    assert allowed["changed"] is True and grant["state"] == "active" and grant["keyId"] == key["keyId"]
    assert grant["grantId"] == "deepseek" and grant["actor"] == world.agent.actor

    status = cli_json(app, monkeypatch, ["agent", "provider", "status", "--actor", "worker"])
    assert [(row["grantId"], row["state"], row["code"], row["unsealable"]) for row in status["grants"]] == \
        [("deepseek", "ok", None, True)]

    launch = compose(world, app)
    environment = dict(launch.environment.for_exec())
    assert environment[ENV] == CLI_SECRET
    assert LEGACY_SECRET not in environment.values()
    assert launch.grants == (("deepseek", grant["seq"]),)

    revoked = cli_json(app, monkeypatch, ["agent", "provider", "revoke", "--actor", "worker",
                                          "--provider", "deepseek"])
    assert revoked["changed"] is True and revoked["grant"]["state"] == "revoked"
    with pytest.raises(SecretResolutionError) as refused:
        compose(world, app)
    assert refused.value.category == ipc_errors.PROVIDER_GRANT_REVOKED
    assert CLI_SECRET not in str(refused.value) and LEGACY_SECRET not in str(refused.value)
    assert world.registry.secret_grant("worker", "deepseek") is not None

    logout = cli_json(app, monkeypatch, ["user", "provider", "logout", key["keyId"], "--yes"])
    assert logout["_sent"] == [("user.provider.logout", {"keyId": key["keyId"]})]
    assert (logout["state"], logout["revokedGrants"], logout["alreadyDeleted"]) == ("deleted", [], False)

    final = cli_json(app, monkeypatch, ["agent", "provider", "status", "--actor", "worker",
                                        "--provider", "deepseek"])
    assert [(row["state"], row["code"], row["keyId"], row["unsealable"]) for row in final["grants"]] == \
        [("revoked", ipc_errors.PROVIDER_GRANT_REVOKED, key["keyId"], False)]
    assert cli_json(app, monkeypatch, ["user", "provider", "list"])["keys"] == []
    again = cli_json(app, monkeypatch, ["agent", "provider", "allow", "--actor", "worker",
                                        "--provider", "deepseek"], exit_code=1)
    assert again["ok"] is False and again["code"] == ipc_errors.PROVIDER_KEY_NOT_FOUND
    with pytest.raises(SecretResolutionError) as refused_after_logout:
        compose(world, app)
    assert refused_after_logout.value.category == ipc_errors.PROVIDER_GRANT_REVOKED
    return {"key": key, "app": app}


def _projection_bytes(listed: Any) -> bytes:
    return json.dumps(listed, sort_keys=True, separators=(",", ":")).encode()


def gui_flow(world: Any) -> dict[str, Any]:
    """provider-write -> grant(expectedAgent) -> list -> restart -> identical list, single authority."""
    state_dir = world.tmp / "daemon-state"
    daemon = owner_daemon(world, state_dir)
    try:
        agent = daemon.agents.create("pi-deepseek")
        expected = {"uri": str(agent.uri), "createdAtMs": agent.created_at_ms}
        written = daemon._handle_agent("agent.secret.provider-write",
                                       {"entryId": "deepseek", "fieldName": "key", "value": GUI_SECRET})
        assert written == {"ok": True, "entryId": "deepseek", "fieldNames": ["key"]}
        granted = daemon._handle_agent("agent.secret.grant", {
            "actor": "pi-deepseek", "grantId": GRANT_ID, "source": "user-provider", "entryId": "deepseek",
            "fieldName": "key", "environmentNames": [ENV], "revision": 1, "expectedAgent": expected})
        row = {"actor": "pi-deepseek", "grantId": GRANT_ID, "source": "user-provider", "entryId": "deepseek",
               "fieldName": "key", "environmentNames": [ENV], "revision": 1, "ownerActor": None,
               "ownerEntityToken": None}
        assert granted == {"ok": True, "grant": row}
        listed = daemon._handle_agent("agent.secret.list", {"actor": "pi-deepseek", "expectedAgent": expected})
        assert listed == {"ok": True, "grants": [row]} and len(listed["grants"][0]) == 9
        first = _projection_bytes(listed)
        _single_authority(world, daemon, "pi-deepseek")
    finally:
        close_daemon(daemon)

    restarted = owner_daemon(world, state_dir)
    try:
        assert not hasattr(restarted, "_provider_key_high_water")
        again = restarted._handle_agent("agent.secret.list", {"actor": "pi-deepseek", "expectedAgent": expected})
        assert _projection_bytes(again) == first
        _single_authority(world, restarted, "pi-deepseek")
    finally:
        close_daemon(restarted)

    shutil.rmtree(state_dir / "provider-keys")
    recovered = owner_daemon(world, state_dir)
    try:
        lost = recovered._handle_agent("agent.secret.list", {"actor": "pi-deepseek", "expectedAgent": expected})
        assert _projection_bytes(lost) == first
    finally:
        close_daemon(recovered)
    return {"row": row, "state_dir": state_dir}


def _single_authority(world: Any, daemon: Any, actor: str) -> None:
    assert not (Path(world.home) / "secrets" / "providers").exists()
    assert daemon._agent_registry.secret_grant(actor, GRANT_ID) is None
    assert daemon._agent_registry.secret_inventory(actor) == ()
