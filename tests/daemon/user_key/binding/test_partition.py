"""Reserved key documents cannot bypass the unavailable publication boundary."""
import pytest
from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore, StoreError
from hyprial.identity import protected_directory_doc_id


@pytest.mark.parametrize("space_name", ["group-synthetic", "personal-synthetic"])
@pytest.mark.parametrize("author", ["user:alice", "user:bob"])
def test_real_store_refuses_unsigned_key_document_even_from_path_owner(tmp_path, author, space_name):
    store = FileSpaceStore(tmp_path / "store", "test-space", node_id="test-node")
    try:
        def meta(doc):
            doc["space"]["owner"] = "user:alice"
            doc["space"]["name"] = space_name
            doc["space"]["spaceId"] = "test-space"
            doc["members"]["user:alice"] = Map({"mode": "rw"})
            doc["members"]["user:bob"] = Map({"mode": "rw"})
        store.commit("meta", meta, author="user:alice", actor=None)
        # Control: this is an authorized writer in an initialized local space.
        store.commit("doc-00000000-0000-0000-0000-000000000001", lambda doc: doc.get("text", type=Text).insert(0, "control"),
                     author=author, actor=None)
        key_doc = protected_directory_doc_id("test-space", "directory/user-keys/alice/revocations.jwt",
                                             space_owner="alice")
        with pytest.raises(StoreError) as caught:
            store.commit(key_doc, lambda doc: doc.get("text", type=Text).insert(0, "unsigned"),
                         author=author, actor=None)
        assert caught.value.code == "not-a-member"
    finally:
        store.close()
