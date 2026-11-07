"""Unsupported durable operations reject rather than silently using a test store."""

import pytest

from hyprial.daemon.impl.orgfs import LocalOrgFs, OrgFsError


class CommitOnlyStore:
    def __init__(self):
        self.commits = []

    def commit(self, doc_id, mutate, *, author, actor):
        self.commits.append(doc_id)
        mutate(None)

    def commit_many(self, mutations, *, author, actor):
        self.commits.extend(doc_id for doc_id, _ in mutations)
        return ()

    def purge_inventory(self, doc_id):
        raise AssertionError("unsupported store must be rejected before inventory IO")

    def frontier(self, doc_id):
        raise AssertionError("unsupported store must be rejected before frontier IO")


@pytest.mark.parametrize("operation", ["purge_plan", "purge", "replacement"])
def test_advanced_operation_requires_durable_owner(operation):
    store = CommitOnlyStore()
    facade = LocalOrgFs(stores=store)
    try:
        space = facade.create_space("test")
        # The intentionally supported commit-only test fallback stays usable.
        facade.write_text(space.space_id, "note.md", "content")
        assert store.commits
        old_tree = facade._spaces[space.space_id].tree_doc_id
        before = tuple(store.commits)
        with pytest.raises(OrgFsError) as caught:
            if operation == "purge_plan":
                facade.purge_plan(space.space_id, [{"kind": "blob", "sha": "0" * 64}])
            elif operation == "purge":
                facade.purge(space.space_id, "unknown-plan")
            else:
                facade.install_replacement_snapshot(
                    space.space_id, old_tree, "replacement-tree",
                    facade._spaces[space.space_id].tree.get_update(),
                )
        assert caught.value.code == "invalid-argument"
        assert "durable authority" in str(caught.value)
        assert facade._spaces[space.space_id].tree_doc_id == old_tree
        assert tuple(store.commits) == before
    finally:
        assert facade.close_effects()
