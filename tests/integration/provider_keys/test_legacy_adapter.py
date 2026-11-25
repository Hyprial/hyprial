"""Scenarios 2 and 3: legacy agent.secret.* through a real DaemonApplication, gate open and closed."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from hyprial.daemon.impl.provider_keys import api, storage
from hyprial.kernel import ipc_errors
from .flows import GRANT_ID, gui_flow
from .support import ENV, GUI_SECRET, close_daemon, invoke, open_daemon, owner_daemon, refused

USER_GRANT = {"actor": "pi-deepseek", "grantId": GRANT_ID, "source": "user-provider", "entryId": "deepseek",
              "fieldName": "key", "environmentNames": [ENV], "revision": 1}
SEVEN = (
    (api.USER_PROVIDER_LOGIN, {"provider": "deepseek", "kind": "api-key", "secret": GUI_SECRET}),
    (api.USER_PROVIDER_LIST, {}),
    (api.USER_PROVIDER_LOGOUT, {"keyId": "pk." + "a" * 26}),
    (api.USER_PROVIDER_ROTATE, {"keyId": "pk." + "a" * 26, "replaceSecret": GUI_SECRET}),
    (api.AGENT_PROVIDER_ALLOW, {"agent": "pi-deepseek", "provider": "deepseek"}),
    (api.AGENT_PROVIDER_REVOKE, {"agent": "pi-deepseek", "provider": "deepseek"}),
    (api.AGENT_PROVIDER_STATUS, {"agent": "pi-deepseek"}),
)


def test_gui_style_legacy_flow_projects_and_recovers_from_signed_records(world):
    outcome = gui_flow(world)
    agents = world.store.listdir(storage.GRANTS_ROOT)
    assert len(agents) == 1
    assert world.store.listdir(storage.grants_dir(agents[0])) == (f"{GRANT_ID}.json",)
    state = world.read_json(storage.grant_path(agents[0], GRANT_ID))["payload"]
    assert (state["state"], state["legacyRef"]) == ("active", {"entryId": "deepseek", "fieldName": "key",
                                                               "revision": 1})
    assert outcome["row"]["revision"] == state["legacyRef"]["revision"]


def _legacy_calls(daemon) -> dict:
    agent = daemon.agents.create("pi-deepseek")
    expected = {"uri": str(agent.uri), "createdAtMs": agent.created_at_ms}
    written = daemon._handle_agent("agent.secret.provider-write",
                                   {"entryId": "deepseek", "fieldName": "key", "value": GUI_SECRET})
    granted = daemon._handle_agent("agent.secret.grant", {**USER_GRANT, "expectedAgent": expected})
    listed = daemon._handle_agent("agent.secret.list", {"actor": "pi-deepseek", "expectedAgent": expected})
    providers = Path(daemon.hyprial_home) / "secrets" / "providers"
    entry = providers / "deepseek.json"
    return {
        "written": written, "granted": granted, "listed": listed,
        "entry": json.loads(entry.read_text(encoding="utf-8")),
        "modes": (stat.S_IMODE(providers.stat().st_mode), stat.S_IMODE(entry.stat().st_mode)),
        "stored": daemon._agent_registry.secret_grant("pi-deepseek", GRANT_ID) is not None,
        "replay": daemon._handle_agent("agent.secret.list", {"actor": "pi-deepseek"}),
    }


@pytest.mark.parametrize("gate", [None, {"authority": "legacy"}], ids=["absent", "legacy"])
def test_closed_gate_keeps_legacy_behavior_and_refuses_the_seven_methods(world, tmp_path, gate):
    world.settings({"owner": "x", "userId": "casdoor-sub-synthetic",
                    **({} if gate is None else {"providerKeys": gate})})
    baseline_home = tmp_path / "baseline-home"
    baseline_home.mkdir(mode=0o700)
    baseline = open_daemon(baseline_home, tmp_path / "baseline-state")
    try:
        expected = _legacy_calls(baseline)
    finally:
        close_daemon(baseline)

    daemon = owner_daemon(world, tmp_path / "daemon-state")
    try:
        observed = _legacy_calls(daemon)
        for method, params in SEVEN:
            error = refused(daemon, method, dict(params))
            assert error.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE, method
            assert GUI_SECRET not in str(error) and GUI_SECRET not in repr(error.data)
        assert daemon._handle_agent("agent.secret.revoke", {"actor": "pi-deepseek", "grantId": GRANT_ID}) == \
            {"ok": True, "revoked": True}
        assert invoke(daemon, "agent.secret.list", {"actor": "pi-deepseek"}) == {"ok": True, "grants": []}
    finally:
        close_daemon(daemon)

    assert observed == expected
    assert observed["entry"] == {"key": GUI_SECRET} and observed["stored"] is True
    assert observed["modes"] == (0o700, 0o600)
    assert world.store.listdir(storage.KEYS_ROOT) == () and world.store.listdir(storage.GRANTS_ROOT) == ()
    assert not (tmp_path / "daemon-state" / "provider-keys").exists()
