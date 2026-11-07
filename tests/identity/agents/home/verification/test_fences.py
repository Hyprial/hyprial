"""Completion fences reject stale incarnation, receipt and payload observations."""
from dataclasses import replace
import threading

import pytest

from hyprial.identity import AgentHomeAccessError
from .test_payload import payload, verify


@pytest.mark.parametrize("damage", ["entity", "resource", "manifest", "state"])
def test_completion_is_bound_to_the_original_observation(home_owner, tmp_path, monkeypatch, damage):
    _registry, actor, agent = home_owner(configured=False)
    _stage, expected = payload(tmp_path)
    complete = actor._home_effects._complete
    def corrupt(event):
        if damage == "entity":
            event = replace(event, receipt=replace(event.receipt, entity_token="new-entity"))
        elif damage == "resource":
            event = replace(event, receipt=replace(event.receipt, resource_token="new-resource"))
        elif damage == "manifest":
            event = replace(event, payload_files=())
        elif damage == "state":
            event = replace(event, payload_state="gone")
        return complete(event)
    monkeypatch.setattr(actor._home_effects, "_complete", corrupt)
    with pytest.raises((AgentHomeAccessError, RuntimeError)):
        verify(actor, agent, expected)


def test_current_authority_is_rechecked_after_filesystem(home_owner, tmp_path, monkeypatch):
    registry, actor, agent = home_owner(configured=False)
    _stage, expected = payload(tmp_path)
    original = registry.home_provisioner.verify_payload
    def replace_after_scan(*args):
        result = original(*args)
        with registry._lock, registry._db:
            import json
            row = registry._db.execute("SELECT payload FROM lifecycle_resources WHERE resource_key=?", ("agent-home:worker",)).fetchone()
            data = json.loads(row["payload"])
            data["resourceToken"] = "replaced-resource"
            registry._db.execute("UPDATE lifecycle_resources SET payload=?, resource_token=? WHERE resource_key=?", (json.dumps(data), "replaced-resource", "agent-home:worker"))
        return result
    monkeypatch.setattr(registry.home_provisioner, "verify_payload", replace_after_scan)
    with pytest.raises(RuntimeError, match="receipt-mismatch"):
        verify(actor, agent, expected)


def test_payload_io_runs_on_home_lane_not_owner_mailbox(home_owner, tmp_path, monkeypatch):
    registry, actor, agent = home_owner(configured=False)
    _stage, expected = payload(tmp_path)
    owner_threads, fs_threads = [], []
    get = registry.home_receipt
    scan = registry.home_provisioner.verify_payload
    def owner(*args, **kwargs):
        owner_threads.append(threading.get_ident())
        return get(*args, **kwargs)
    def effect(*args):
        fs_threads.append(threading.get_ident())
        return scan(*args)
    monkeypatch.setattr(registry, "home_receipt", owner)
    monkeypatch.setattr(registry.home_provisioner, "verify_payload", effect)
    assert verify(actor, agent, expected) == "absent"
    assert owner_threads and fs_threads
    assert set(owner_threads).isdisjoint(fs_threads)
    assert threading.get_ident() not in owner_threads + fs_threads
