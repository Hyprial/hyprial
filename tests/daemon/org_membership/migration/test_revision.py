"""Persisted migration fences cannot use facade-local observation counters."""
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore


def test_content_revision_survives_reopen_and_changes_on_revision(tmp_path):
    sid = "00000000-0000-0000-0000-000000000022"
    disk = FileSpaceStore(tmp_path, sid, node_id="node")
    fs = LocalOrgFs(stores=disk, author="user:test")
    fs.create_space("test", space_id=sid)
    fs.write_text(sid, "row.json", '{"state":"active"}')
    before = fs.read_text_snapshot(sid, "row.json")
    revision = getattr(before, "content_revision", None)
    fs.close_effects()
    disk.close()
    assert revision, "migration needs a durable content revision, not local frontier counters"
    disk = FileSpaceStore(tmp_path, sid, node_id="node")
    fs = LocalOrgFs(stores=disk, author="user:test")
    try:
        fs.load_space(sid)
        assert fs.read_text_snapshot(sid, "row.json").content_revision == revision
        fs.write_text(sid, "row.json", '{"state":"removed"}')
        fs.write_text(sid, "row.json", '{"state":"active"}')
        assert fs.read_text_snapshot(sid, "row.json").content_revision != revision
    finally:
        fs.close_effects()
        disk.close()
