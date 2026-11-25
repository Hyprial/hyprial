# Decisions:
# - Design §3.3 (and review must-fix 7): every facade write entry produces
#   its update through the single commit helper, so the decoded state vector
#   of each document it modifies strictly advances; a pure-deletion edit
#   advances too because the helper bumps ``__orgfs__.c``.  Design §4.5
#   depends on it: "equal vectors, nothing to send" is only sound when every
#   write advances the vector.
# - Vectors are read through ``orgfs_impl.document_vector`` and compared only
#   after decoding to {client: clock} (plan §10).
# - Legacy has no commit clock; a Yjs pure deletion leaves the vector
#   unchanged (pycrdt 0.14.9, see the C4 report), so the deletion-only
#   entries are legacy_xfail.
"""State-vector advance contract for every facade write entry (design §3.3)."""

from __future__ import annotations

import pytest

from ..support import strictly_advanced

PURE_DELETION = pytest.mark.legacy_xfail(
    reason="design §3.3: the commit helper bumps __orgfs__.c so a pure deletion "
    "advances the content state vector; legacy has no commit clock"
)


def _seed(fs) -> str:
    space_id = fs.create_space("vectors").space_id
    fs.mkdir(space_id, "folder")
    fs.write_text(space_id, "folder/a.md", "alpha beta")
    fs.write_bytes(space_id, "folder/c.bin", b"before")
    trashed = fs.write_text(space_id, "trashed.md", "in trash")
    fs.remove(space_id, f"id:{trashed.node_id}")
    fs.invite(space_id, "user:guest", "rw")
    return space_id


ENTRIES = {
    "write_text-existing": ("folder/a.md", lambda impl, fs, sid: fs.write_text(sid, "folder/a.md", "alpha beta gamma")),
    "write_text-new": ("tree", lambda impl, fs, sid: fs.write_text(sid, "new.md", "new")),
    "write_bytes-new": ("tree", lambda impl, fs, sid: fs.write_bytes(sid, "new.bin", b"blob")),
    "write_bytes-existing": ("tree", lambda impl, fs, sid: fs.write_bytes(sid, "folder/c.bin", b"after")),
    "mkdir": ("tree", lambda impl, fs, sid: fs.mkdir(sid, "new-dir")),
    "move": ("tree", lambda impl, fs, sid: fs.move(sid, "folder/a.md", "renamed.md")),
    "remove": ("tree", lambda impl, fs, sid: fs.remove(sid, "folder/a.md")),
    "restore": ("tree", lambda impl, fs, sid: impl.restore_last(fs, sid, "trashed.md")),
    "update_space_meta": ("meta", lambda impl, fs, sid: fs.update_space_meta(sid, {"aclSpaceId": "acl-1"})),
    "invite": ("meta", lambda impl, fs, sid: fs.invite(sid, "user:new", "ro")),
    "invite-mode-change": ("meta", lambda impl, fs, sid: fs.invite(sid, "user:guest", "ro")),
    "remove_member": ("meta", lambda impl, fs, sid: fs.remove_member(sid, "user:guest")),
}

DELETIONS = {
    "clear-text": lambda fs, sid: fs.write_text(sid, "folder/a.md", ""),
    "delete-suffix": lambda fs, sid: fs.write_text(sid, "folder/a.md", "alpha"),
}


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_every_write_entry_advances_the_modified_document(orgfs_impl, entry: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space_id = _seed(fs)
    document, write = ENTRIES[entry]
    before = orgfs_impl.document_vector(fs, space_id, document)

    write(orgfs_impl, fs, space_id)

    after = orgfs_impl.document_vector(fs, space_id, document)
    assert strictly_advanced(before, after), (before, after)


@PURE_DELETION
@pytest.mark.parametrize("entry", sorted(DELETIONS))
def test_a_pure_deletion_edit_advances_the_content_vector(orgfs_impl, entry: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space_id = _seed(fs)
    before = orgfs_impl.document_vector(fs, space_id, "folder/a.md")

    DELETIONS[entry](fs, space_id)

    after = orgfs_impl.document_vector(fs, space_id, "folder/a.md")
    assert strictly_advanced(before, after), (before, after)


@pytest.mark.legacy_xfail(
    reason="design §3.3: creating a document goes through the commit helper, which "
    "bumps __orgfs__.c; legacy creates an empty text document with an empty vector"
)
def test_a_new_empty_document_is_born_with_a_nonempty_vector(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space_id = _seed(fs)

    fs.write_text(space_id, "empty.md", "")

    assert sum(orgfs_impl.document_vector(fs, space_id, "empty.md").values()) > 0


def test_create_space_initializes_meta_and_tree(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")

    space_id = fs.create_space("fresh").space_id

    assert sum(orgfs_impl.document_vector(fs, space_id, "meta").values()) > 0
    assert sum(orgfs_impl.document_vector(fs, space_id, "tree").values()) > 0
