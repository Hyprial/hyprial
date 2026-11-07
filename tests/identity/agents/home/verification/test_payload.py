"""Read-only payload classification through the identity owner and FS lane."""
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.identity import AgentHomeAccessError
from hyprial.identity.impl.agents.home.provisioner import HomePayloadFile, snapshot_home_payload
from hyprial.identity.impl.agents.registry._base import AgentEntityConflict


def payload(tmp_path):
    stage = tmp_path / "stage"
    (stage / "state").mkdir(parents=True)
    (stage / "config").mkdir()
    (stage / "state/data").write_text("synthetic data")
    (stage / "config/test").write_text("synthetic config")
    return stage, snapshot_home_payload(stage)


def verify(actor, agent, expected):
    return actor.verify_landing_payload(agent.actor, expected_entity_token=agent.entity_token, expected_files=expected)


def test_absent_then_present_then_partial_are_fresh_reads(home_owner, tmp_path):
    registry, actor, agent = home_owner(configured=False)
    stage, expected = payload(tmp_path)
    assert verify(actor, agent, expected) == "absent"
    home = Path(actor.materialise_landing_home(agent.actor, str(stage), expected_entity_token=agent.entity_token, expected_files=expected))
    before = snapshot_home_payload(home)
    assert verify(actor, agent, expected) == "present"
    assert snapshot_home_payload(home) == before
    (home / "state/data").unlink()
    assert verify(actor, agent, expected) == "partial"
    assert registry.require(agent.actor).entity_token == agent.entity_token


@pytest.mark.parametrize("damage", ["digest", "mode", "missing-receipt", "resource", "symlink", "directory-link", "extra-only"])
def test_invalid_payload_or_mirror_never_present(home_owner, tmp_path, damage):
    registry, actor, agent = home_owner(configured=False)
    stage, expected = payload(tmp_path)
    home = Path(actor.materialise_landing_home(agent.actor, str(stage), expected_entity_token=agent.entity_token, expected_files=expected))
    if damage == "digest":
        (home / "state/data").write_text("X" * len((home / "state/data").read_text()))
    elif damage == "mode":
        (home / "state/data").chmod(0o400)
    elif damage == "missing-receipt":
        (home / "state/home-receipt.json").unlink()
    elif damage == "resource":
        path = home / "state/home-receipt.json"
        value = json.loads(path.read_text())
        value["resourceToken"] = "other"
        path.write_text(json.dumps(value))
    elif damage == "symlink":
        item = home / "state/data"
        item.rename(tmp_path / "external")
        item.symlink_to(tmp_path / "external")
    elif damage == "directory-link":
        item = home / "config"
        item.rename(tmp_path / "external")
        item.symlink_to(tmp_path / "external", target_is_directory=True)
    else:
        (home / "state/data").unlink()
        (home / "config/test").unlink()
        (home / "state/unexpected").write_text("residue")
    assert verify(actor, agent, expected) == "partial"
    assert registry.require(agent.actor).entity_token == agent.entity_token


def test_stale_token_rejected_in_owner_before_filesystem(home_owner, tmp_path, monkeypatch):
    registry, actor, agent = home_owner(configured=False)
    _stage, expected = payload(tmp_path)
    threads = []
    original = registry.home_receipt
    def observed(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(registry, "home_receipt", observed)
    with pytest.raises(AgentEntityConflict):
        verify(actor, replace(agent, entity_token="stale"), expected)
    assert threads and threading.get_ident() not in threads


@pytest.mark.parametrize("bad", [(), (HomePayloadFile("../escape", 1, "a" * 64, 0o600),), (HomePayloadFile("state/item", 1, "bad", 0o600),)])
def test_invalid_manifest_is_rejected(home_owner, bad):
    _registry, actor, agent = home_owner(configured=False)
    with pytest.raises((ValueError, AgentHomeAccessError)):
        verify(actor, agent, bad)


def test_source_receipt_is_excluded_but_target_receipt_is_checked(home_owner, tmp_path):
    _registry, actor, agent = home_owner(configured=False)
    stage, expected = payload(tmp_path)
    actor.materialise_landing_home(agent.actor, str(stage), expected_entity_token=agent.entity_token, expected_files=expected)
    source_receipt = HomePayloadFile("state/home-receipt.json", 999, "a" * 64, 0o600)
    assert verify(actor, agent, (*expected, source_receipt)) == "present"


@pytest.mark.parametrize("token", [None, "", 1])
def test_missing_token_cannot_disable_fence(home_owner, tmp_path, token):
    _registry, actor, agent = home_owner(configured=False)
    _stage, expected = payload(tmp_path)
    with pytest.raises(ValueError, match="expected_entity_token"):
        actor.verify_landing_payload(agent.actor, expected_entity_token=token, expected_files=expected)


def test_root_replacement_during_scan_is_partial(home_owner, tmp_path, monkeypatch):
    import shutil
    from hyprial.identity.impl.agents.home import provisioner as module
    registry, actor, agent = home_owner(configured=False)
    stage, expected = payload(tmp_path)
    home = Path(actor.materialise_landing_home(agent.actor, str(stage), expected_entity_token=agent.entity_token, expected_files=expected))
    snapshot = module.snapshot_home_payload
    def swap(root, **kwargs):
        result = snapshot(root, **kwargs)
        saved = tmp_path / "old-home"
        home.rename(saved)
        shutil.copytree(saved, home)
        return result
    monkeypatch.setattr(module, "snapshot_home_payload", swap)
    assert verify(actor, agent, expected) == "partial"
    assert registry.require(agent.actor).entity_token == agent.entity_token
