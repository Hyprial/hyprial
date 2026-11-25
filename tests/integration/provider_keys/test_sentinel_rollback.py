"""Scenarios 4 and 5: no plaintext anywhere on disk, and a replayed grant never survives a restart [security]."""
from __future__ import annotations

import pytest

from hyprial.daemon.impl.provider_keys import api, storage
from hyprial.identity import SecretResolutionError
from hyprial.kernel import ipc_errors
from tests.daemon.provider_keys.service.conftest import HOUR, NOW
from .flows import cli_flow, gui_flow
from .support import CLI_SECRET, ENV, GUI_SECRET, compose, invoke, ipc_app

AFTER_TTL = NOW + api.GRANT_TTL_MS + HOUR


def test_no_owner_key_plaintext_on_disk_after_the_cli_and_gui_flows(worlds, tmp_path, monkeypatch, launch_clock):
    cli_world, gui_world = worlds
    cli_flow(cli_world, monkeypatch)
    gui = gui_flow(gui_world)
    scanned = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_file()}
    assert any(name.startswith("cli/orgfs/") for name in scanned)
    assert any(name.startswith("gui/orgfs/") for name in scanned)
    assert "cli/state/provider-keys/receipts.jsonl" in scanned
    assert any(name.startswith("cli/state/provider-keys/high-water-") for name in scanned)
    assert any(name.startswith(str(gui["state_dir"].relative_to(tmp_path)) + "/provider-keys/high-water-")
               for name in scanned)
    assert cli_world.sentinel_hits(CLI_SECRET, GUI_SECRET) == []
    assert gui_world.sentinel_hits(CLI_SECRET, GUI_SECRET) == []
    leftovers = [name for name in scanned if not name.startswith(("cli/", "gui/"))]
    for name in leftovers:
        data = (tmp_path / name).read_bytes()
        assert CLI_SECRET.encode() not in data and GUI_SECRET.encode() not in data, name


def _put(world, path: str, text: str) -> None:
    _, version = world.store.read(path)
    world.store.write(path, text, expect_version=version, create_only=False)


def _refused_launch(world, app) -> SecretResolutionError:
    with pytest.raises(SecretResolutionError) as caught:
        compose(world, app)
    assert CLI_SECRET not in str(caught.value) and CLI_SECRET not in repr(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    return caught.value


def test_replayed_grant_after_revoke_and_restart_is_refused_at_launch(world, launch_clock):
    app = ipc_app(world)
    key = invoke(app, api.USER_PROVIDER_LOGIN, {"provider": "deepseek", "kind": "api-key", "secret": CLI_SECRET,
                                                "environmentNames": [ENV]})["key"]
    invoke(app, api.AGENT_PROVIDER_ALLOW, {"agent": "worker", "provider": "deepseek", "keyId": key["keyId"]})
    assert dict(compose(world, app).environment.for_exec())[ENV] == CLI_SECRET
    path = storage.grant_path(world.ref().agent, "deepseek")
    active = world.store.read(path)[0]
    invoke(app, api.AGENT_PROVIDER_REVOKE, {"agent": "worker", "provider": "deepseek"})
    _put(world, path, active)

    restarted = ipc_app(world, fresh_signer=True)
    assert not hasattr(restarted, "_provider_key_high_water")
    launch_clock[0] = lambda: AFTER_TTL
    error = _refused_launch(world, restarted)
    assert error.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert error.category == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    assert world.store.read(path)[0] == active

    launch_clock[0] = lambda: NOW + 1
    assert _refused_launch(world, ipc_app(world, fresh_signer=True)).category == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    status = invoke(ipc_app(world, fresh_signer=True), api.AGENT_PROVIDER_STATUS,
                    {"agent": "worker", "provider": "deepseek"})
    assert [(row["state"], row["unsealable"]) for row in status["grants"]] == [("signature-invalid", False)]
