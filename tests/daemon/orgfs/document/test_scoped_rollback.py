"""Rollback exports only declared content participants, never unrelated text."""

from dataclasses import replace

import pytest

from hyprial.daemon.impl.orgfs import LocalOrgFs, OrgFsError
from hyprial.daemon.impl.orgfs.document.model import _ContentDocument


class Store:
    fail = False

    def commit(self, doc_id, mutate, *, author, actor):
        if self.fail:
            raise RuntimeError("injected durable failure")
        mutate(None)

    def commit_many(self, mutations, *, author, actor):
        if self.fail:
            raise RuntimeError("injected durable failure")
        return ()


@pytest.fixture
def setup():
    store = Store()
    fs = LocalOrgFs(stores=store)
    try:
        sid = fs.create_space("rollback").space_id
        fs.mkdir(sid, "folder")
        a = fs.write_text(sid, "folder/a", "before-a")
        b = fs.write_text(sid, "folder/b", "before-b")
        fs.write_text(sid, "unrelated", "unrelated text")
        fs.invite(sid, "user:guest")
        version = b.version
        fs.write_text(sid, "folder/a", "after-a")
        fs.write_text(sid, "folder/b", "after-b")
        yield fs, store, sid, a, b, version
    finally:
        assert fs.close_effects()


def action(fs, sid, kind, version):
    if kind == "write":
        fs.write_text(sid, "folder/a", "changed")
    elif kind == "create":
        fs.write_text(sid, "new", "new text")
    elif kind == "structured":
        fs.open_doc(sid, "folder/a").transact(lambda root: root.set("key", "value"))
    elif kind == "restore":
        fs.restore(sid, "folder", version)
    elif kind == "mkdir":
        fs.mkdir(sid, "new-dir")
    elif kind == "move":
        fs.move(sid, "folder/a", "renamed")
    elif kind == "remove":
        fs.remove(sid, "folder")
    elif kind == "blob-create":
        fs.write_bytes(sid, "new-blob", b"blob")
    elif kind == "blob-replace":
        fs.write_bytes(sid, "folder/a", b"blob")
    elif kind == "invite":
        fs.invite(sid, "user:new")
    elif kind == "remove-member":
        fs.remove_member(sid, "user:guest")
    elif kind == "meta":
        fs.update_space_meta(sid, {"custom": "changed"})
    elif kind == "heal":
        fs.write_text(sid, "folder/a", "after-a")
    else:
        raise AssertionError(kind)


KINDS = ["write", "create", "structured", "restore", "mkdir", "move", "remove",
         "blob-create", "blob-replace", "invite", "remove-member", "meta", "heal"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fail", [False, True])
def test_exports_only_participants(setup, monkeypatch, kind, fail):
    fs, store, sid, a, b, version = setup
    space = fs._spaces[sid]
    if kind == "heal":
        space.nodes[a.node_id].required_content_frontier = None
    untouched = space.contents[fs.stat(sid, "unrelated").doc_id]
    allowed = set()
    if kind in {"write", "structured", "restore"}:
        allowed.add(id(space.contents[a.doc_id]))
    if kind == "restore":
        allowed.add(id(space.contents[b.doc_id]))
    existing = {id(doc) for doc in space.contents.values()}
    original = _ContentDocument.export
    exported = []

    def spy(doc):
        exported.append(id(doc))
        return original(doc)

    monkeypatch.setattr(_ContentDocument, "export", spy)
    store.fail = fail
    if fail:
        with pytest.raises(OrgFsError, match="injected durable failure"):
            action(fs, sid, kind, version)
    else:
        action(fs, sid, kind, version)
    assert set(exported) & existing <= allowed
    assert space.contents[fs.stat(sid, "unrelated").doc_id] is untouched


def state(space):
    return (
        space.tree.get_update(), space.meta.get_update(),
        {key: replace(node) for key, node in space.nodes.items()},
        {key: doc.export() for key, doc in space.contents.items()},
        dict(space.members), set(space.removed_members), space.revision,
        dict(space.snapshots), {key: list(value) for key, value in space.history.items()},
    )


@pytest.mark.parametrize("kind", KINDS)
def test_failed_commit_restores_exact_state(setup, kind):
    fs, store, sid, a, _, version = setup
    space = fs._spaces[sid]
    if kind == "heal":
        space.nodes[a.node_id].required_content_frontier = None
    before = state(space)
    events = fs.changes_since(sid)
    store.fail = True
    with pytest.raises(OrgFsError, match="injected durable failure"):
        action(fs, sid, kind, version)
    assert state(space) == before
    assert fs.changes_since(sid) == events


@pytest.mark.parametrize("batch", [False, True])
def test_content_commit_failure_preserves_live_document_handle(setup, batch):
    fs, store, sid, a, _, _ = setup
    space = fs._spaces[sid]
    document = space.contents[a.doc_id]
    before = state(space)
    store.fail = True
    with pytest.raises(OrgFsError, match="injected durable failure"):
        if batch:
            fs._commit_many(space, [(a.doc_id, lambda: document.set("changed"))])
        else:
            fs._commit(space, a.doc_id, lambda: document.set("changed"))
    assert state(space) == before
    assert space.contents[a.doc_id] is document
    assert document.value() == "after-a"


def test_batch_callback_failure_restores_only_touched_content(setup):
    fs, _, sid, a, _, _ = setup
    space = fs._spaces[sid]
    before = state(space)
    document = space.contents[a.doc_id]

    def fail():
        document.set("changed")
        raise RuntimeError("mutation failed")

    with pytest.raises(OrgFsError, match="mutation failed"):
        fs._commit_many(space, [(a.doc_id, fail)])
    assert state(space) == before


def test_failed_space_creation_removes_provisional_space():
    store = Store()
    store.fail = True
    fs = LocalOrgFs(stores=store)
    try:
        with pytest.raises(OrgFsError, match="injected durable failure"):
            fs.create_space("failed")
        assert fs.spaces() == ()
    finally:
        assert fs.close_effects()
