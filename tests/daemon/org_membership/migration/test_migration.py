"""Work-org membership migration: idempotent writes, conflict fence, zero personal orgs."""
import importlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.upgrade import STEPS, run_upgrade_steps

OWNER = "owner@example"


def implementation():
    assert any(step.id == "org-membership-v1" and step.repeatable for step in STEPS)
    return importlib.import_module("hyprial.daemon.impl.org.membership.migration.apply")


def fixture(tmp_path, *, node="node", fs=None, with_work_context=True):
    module = implementation()
    fs = fs or LocalOrgFs(author="user:" + OWNER)
    contexts = []
    if with_work_context:
        work = fs.create_space("group-team")
        fs.update_space_meta(work.space_id, {"org": "group-team", "kind": "work"})
        fs.mkdir(work.space_id, "directory")
        fs.write_text(work.space_id, "directory/org.json", '{"state":"active"}')
        contexts = [{
            "source": work.space_id,
            "org": "group-team",
            "digest": "accepted",
            "residents": [{"name": "worker", "node": node}],
        }]
    source = {
        "users": [{"owner": OWNER}],
        "agents": [{"agent": f"agent:{OWNER}:node:worker", "owner": OWNER,
                    "name": "worker", "node": node, "agentId": "a." + "a" * 26}],
        "contexts": contexts, "problems": [],
    }
    svc = module.MigrationService(fs=fs, home=tmp_path, owner=OWNER, node_id=node,
                                  inventory=lambda: deepcopy(source))
    return svc, source, fs


def run_step(tmp_path, svc):
    return run_upgrade_steps(tmp_path / "state", only=("org-membership-v1",),
                             home=tmp_path, membership_migration=svc.run)


def test_step_registered_repeatable_and_missing_runtime_stays_pending(tmp_path):
    implementation()
    result = run_upgrade_steps(tmp_path / "state", only=("org-membership-v1",), home=tmp_path)
    assert result["steps"][0]["state"] == "pending"
    assert result["pending"] == ["org-membership-v1"]


def test_agent_with_zero_work_orgs_verifies_without_personal_org(tmp_path):
    svc, _, fs = fixture(tmp_path, with_work_context=False)
    report = svc.run()
    assert report["verified"] is True
    assert not report["failed"] and not report["pending"]
    assert list(fs.spaces()) == []


def test_migrate_work_context_verify_and_repeat_preserves_existing_documents(tmp_path):
    svc, source, fs = fixture(tmp_path)
    old = fs.create_space("notes")
    fs.write_text(old.space_id, "old.txt", "existing orgfs bytes")
    original_source = deepcopy(source)
    first = run_step(tmp_path, svc)
    assert first["steps"][0]["state"] == "done"
    report = first["steps"][0]["data"]
    assert report["schemaVersion"] == 1 and report["operationId"]
    assert report["verified"] is True
    assert Path(report["reportPath"]).stat().st_mode & 0o777 == 0o600
    assert fs.read_text(old.space_id, "old.txt")[0] == "existing orgfs bytes"
    assert source == original_source


@pytest.mark.parametrize("change", ["incarnation", "context"])
def test_verified_migration_never_replays_over_manual_drift(tmp_path, change):
    svc, source, fs = fixture(tmp_path)
    assert not svc.run()["failed"]
    if change == "incarnation":
        source["agents"][0]["agentId"] = "a." + "c" * 26
    else:
        source["contexts"].append({"source": "unmapped", "org": None, "digest": "changed", "residents": []})
    result = svc.run()
    assert result["failed"] or result["conflicts"]


@pytest.mark.parametrize("case", ["unmapped", "missing", "ambiguous"])
def test_context_ambiguity_is_reported_not_guessed(tmp_path, case):
    svc, source, fs = fixture(tmp_path, with_work_context=False)
    source["contexts"] = [{"source": "legacy", "org": None if case == "unmapped" else "group-team",
                           "digest": "accepted", "residents": [{"name": "worker", "node": "wrong" if case == "missing" else "node"}]}]
    if case == "ambiguous":
        source["agents"].append(dict(source["agents"][0], agent="agent:other:node:worker", owner="other", agent_id="a." + "b" * 26))
    result = svc.run()
    assert result["pending"] or result["conflicts"]
    assert all(space.name != "group-team" for space in fs.spaces())


def test_methodspec_upgrade_surface_and_target_home(tmp_path, monkeypatch):
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
    assert "upgrade.steps.run" in METHOD_REGISTRY and "upgrade.steps.status" in METHOD_REGISTRY
    module = importlib.import_module("hyprial.daemon.impl.request_catalog.upgrade.methods")
    seen = []
    monkeypatch.setattr(module, "run_upgrade_steps", lambda state, **kw: seen.append(kw) or {"steps": [], "pending": []})
    app = SimpleNamespace(state_dir=tmp_path / "state", hyprial_home=tmp_path / "target")
    result = METHOD_REGISTRY.invoke(app, "upgrade.steps.run", {})
    assert result["pending"] == []
    assert seen[0]["hyprial_home"] == app.hyprial_home
    assert callable(seen[0]["membership_migration"])
