"""Real source adapters and declared method composition without personal orgs."""
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.org.document import parse_document
from hyprial.daemon.impl.org.store import OrgContextStore
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from tests.daemon.org_membership.migration.test_migration import OWNER, implementation
from tests.daemon.organizations.context.test_org_context_orgfs_migration import _document


@pytest.mark.parametrize("segment", ["worker", "a." + "b" * 26], ids=["name-form", "id-form"])
def test_runtime_declared_run_reads_live_ports_and_existing_context(tmp_path, monkeypatch, segment):
    """Rows key on the immutable agent id, so #1886 id-form record URIs migrate too."""
    implementation()
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
    fs = LocalOrgFs(author="user:" + OWNER)
    work = fs.create_space("group-team")
    fs.update_space_meta(work.space_id, {"org": "group-team", "kind": "work"})
    fs.mkdir(work.space_id, "directory")
    fs.write_text(work.space_id, "directory/org.json", '{"state":"active"}')
    text = _document(1).replace("residents: []", "residents:\n  - {name: worker, node: node, role: worker}")
    OrgContextStore(tmp_path).adopt(parse_document(text))
    fs.write_text(work.space_id, "org-context.md", text)
    agent = SimpleNamespace(uri=f"agent:{OWNER}:node:{segment}", owner=OWNER, machine="node",
                            actor="worker", entity_token="a" * 32, agent_id="a." + "a" * 26)
    events = []
    app = SimpleNamespace(owner=OWNER, node_id="node", hyprial_home=tmp_path, state_dir=tmp_path / "state",
        _agent_registry=SimpleNamespace(list=lambda: (agent,)),
        _require_orgfs_runtime=lambda: SimpleNamespace(facade=fs), _log=lambda *a, **kw: events.append((a, kw)))
    monkeypatch.setattr("hyprial.daemon.impl.upgrade.steps.mcp_entry.legacy_processes", lambda **kw: [])
    result = METHOD_REGISTRY.invoke(app, "upgrade.steps.run", {"apply": True})
    row = next(step for step in result["steps"] if step["id"] == "org-membership-v1")
    assert row["state"] == "done", row
    assert row["data"]["verified"]
    assert fs.read_text(work.space_id, "org-context.md")[0] == text
    assert any(args[2] == "org.migration.report" for args, _ in events)


def test_context_without_explicit_containing_org_is_pending_not_inferred(tmp_path):
    implementation()
    from hyprial.daemon.impl.org.membership.migration.inventory import accepted_contexts
    fs = LocalOrgFs(author="user:" + OWNER)
    space = fs.create_space("group-do-not-infer")
    text = _document(1, publisher="publisher-not-an-org")
    OrgContextStore(tmp_path).adopt(parse_document(text))
    fs.write_text(space.space_id, "org-context.md", text)
    contexts, problems = accepted_contexts(tmp_path, fs)
    assert not problems and contexts[0]["org"] is None


def test_runtime_failure_produces_report_without_raising(tmp_path):
    from hyprial.daemon.impl.org.membership.migration.runtime import run

    app = SimpleNamespace(
        owner=OWNER, node_id="node", hyprial_home=tmp_path,
        _require_orgfs_runtime=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        _log=lambda *a, **kw: None,
    )
    report = run(app)
    assert report["failed"][0]["entity"] == "runtime"
