"""Purge is phased: rejection restores the current commit, not earlier phases."""

from contextlib import closing

import pytest
from pycrdt import Map

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from tests.daemon.orgfs.document.test_scoped_rollback import setup, state  # noqa: F401


@pytest.mark.parametrize("step", [1, 2, 3, 4])
def test_purge_commit_failure_restores_phase(tmp_path, monkeypatch, step):
    with closing(OrgFsRuntime(tmp_path / "state", node_id="owner", author="user:owner")) as runtime:
        fs = runtime.facade
        sid = fs.create_space("purge").space_id
        node = fs.write_text(sid, "target", "old")
        fs.write_text(sid, "target", "retained")
        unrelated = fs.write_text(sid, "unrelated", "untouched")
        plan = fs.purge_plan(sid, [{"kind": "doc-range", "node": f"id:{node.node_id}",
                                   "from": node.version, "to": node.version}])
        space = fs._spaces[sid]
        original_document = space.contents[unrelated.doc_id]
        store = runtime.stores.get(sid)
        original = fs._commit
        attempts = []
        before = []

        def reject(*args, **kwargs):
            raise RuntimeError("injected purge failure")

        def commit(current, doc_id, operation, **kwargs):
            attempts.append(doc_id)
            if len(attempts) == step:
                before.append(state(current))
                monkeypatch.setattr(store, "commit_with_outbox", reject)
            return original(current, doc_id, operation, **kwargs)

        monkeypatch.setattr(fs, "_commit", commit)
        with pytest.raises(OrgFsError, match="injected purge failure"):
            fs.purge(sid, plan.plan_id)
        assert len(attempts) == step
        assert state(space) == before[0]
        assert space.contents[unrelated.doc_id] is original_document


@pytest.mark.parametrize("kind", ["acknowledge", "unban"])
def test_purge_meta_commands_restore_on_failure(setup, kind):  # noqa: F811
    fs, store, sid, _, _, _ = setup
    space = fs._spaces[sid]
    digest = "a" * 64
    if kind == "unban":
        space.meta.get("purgeList", type=Map)[digest] = Map({"bannedAt": "now"})
    # Acknowledge intentionally forgets history before its commit boundary.
    fs._forget_history(space)
    before = state(space)
    store.fail = True
    with pytest.raises(OrgFsError, match="injected durable failure"):
        if kind == "acknowledge":
            fs.acknowledge_purge(sid, "plan")
        else:
            fs.unban(sid, digest)
    assert state(space) == before

