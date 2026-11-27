# Decisions:
# - Migrated from storage/tree/test_orgfs_tree.py (``expect_version``
#   stale-write) and the base_version half of its tree-API test; create-only
#   follows the facade semantics consumed by cells and provider_keys
#   (triage §4.3, §4.8).  Originals stay until phase E.
# - ``version`` is opaque (design §1.1): tests only compare it for equality
#   and pass back what the facade returned.
# - Phase F4 (real-node finding R2-2): a ``base_version`` write merges the two
#   edits of one base three-way.  Edits that do not overlap are both kept
#   wherever they sit and whatever their lengths (the earlier test only had
#   same-length edits, which hid the misplaced offsets); where both writes
#   changed the same text, the later write's text wins, which is the rule the
#   merge has always stated ("apply the requested diff to the current
#   document").
"""Compare-and-set and three-way merge contracts of facade writes."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError


def test_expect_version_mismatch_is_stale_write_and_keeps_content(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first = fs.write_text(space.space_id, "file.txt", "one")
    fs.write_text(space.space_id, "file.txt", "two")

    for stale in ("stale", first.version):
        with pytest.raises(OrgFsError) as error:
            fs.write_text(space.space_id, "file.txt", "three", expect_version=stale)
        assert error.value.code == "stale-write"
    assert fs.read_text(space.space_id, "file.txt")[0] == "two"


def test_expect_version_current_succeeds(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "file.txt", "one")
    _content, current = fs.read_text(space.space_id, "file.txt")

    written = fs.write_text(space.space_id, "file.txt", "two", expect_version=current)

    assert fs.read_text(space.space_id, "file.txt") == ("two", written.version)


def test_create_only_refuses_an_existing_node(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")

    created = fs.write_text(space.space_id, "only.txt", "first", create_only=True)
    with pytest.raises(OrgFsError) as error:
        fs.write_text(space.space_id, "only.txt", "second", create_only=True)

    assert error.value.code == "stale-write"
    assert fs.read_text(space.space_id, "only.txt") == ("first", created.version)


def test_base_version_merges_concurrent_edits_three_way(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "merge.txt", "alpha\nbeta\ngamma\n")
    _content, base = fs.read_text(space.space_id, "merge.txt")

    fs.write_text(space.space_id, "merge.txt", "ALPHA\nbeta\ngamma\n", base_version=base)
    merged = fs.write_text(space.space_id, "merge.txt", "alpha\nbeta\nGAMMA\n", base_version=base)

    assert fs.read_text(space.space_id, "merge.txt") == ("ALPHA\nbeta\nGAMMA\n", merged.version)


def _merge_from_one_base(orgfs_impl, base: str, first: str, second: str) -> str:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "merge.txt", base)
    _content, version = fs.read_text(space.space_id, "merge.txt")
    fs.write_text(space.space_id, "merge.txt", first, base_version=version)
    merged = fs.write_text(space.space_id, "merge.txt", second, base_version=version)
    text, current = fs.read_text(space.space_id, "merge.txt")
    assert current == merged.version
    return text


@pytest.mark.parametrize(
    ("base", "first", "second", "merged"),
    [
        pytest.param(
            "aaa\nbbb\nccc\n", "aaa\nX\nccc\n", "aaa\nbbb\nY\n", "aaa\nX\nY\n",
            id="adjacent-lines-of-other-lengths",
        ),
        pytest.param(
            "line1\nline2\nline3\nline4\n",
            "line1\nline2 edited on A\nline3\nline4\n",
            "line1\nline2\nline3\nline4 edited on B\n",
            "line1\nline2 edited on A\nline3\nline4 edited on B\n",
            id="separate-lines-growing",
        ),
        pytest.param(
            "one\ntwo\nthree\nfour\n", "one\nthree\nfour\n", "one\ntwo\nthree\nFOUR!\n",
            "one\nthree\nFOUR!\n",
            id="a-deleted-line-before-an-edit",
        ),
        pytest.param(
            "one\ntwo\n", "zero\none\ntwo\n", "one\ntwo\nthree\n", "zero\none\ntwo\nthree\n",
            id="insertions-at-both-ends",
        ),
        pytest.param(
            "a\nb", "a\nb\n", "A\nb", "A\nb\n",
            id="a-final-newline-added-beside-an-edit",
        ),
        pytest.param(
            "a\nb\n", "A\nb\n", "a\nb", "A\nb",
            id="a-final-newline-removed-beside-an-edit",
        ),
    ],
)
def test_base_version_keeps_both_edits_that_do_not_overlap(
    orgfs_impl, base: str, first: str, second: str, merged: str
) -> None:
    assert _merge_from_one_base(orgfs_impl, base, first, second) == merged


def test_base_version_lets_the_later_write_win_where_both_changed_the_same_text(orgfs_impl) -> None:
    merged = _merge_from_one_base(
        orgfs_impl, "keep\nsame line\nkeep too\n", "keep\nfirst wrote this\nkeep too\n",
        "keep\nsecond\nkeep too\n",
    )

    assert merged == "keep\nsecond\nkeep too\n"


def test_base_version_current_is_a_plain_overwrite(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first = fs.write_text(space.space_id, "notes.md", "before")

    second = fs.write_text(space.space_id, "notes.md", "after", base_version=first.version)

    assert fs.read_text(space.space_id, "notes.md") == ("after", second.version)


def test_unknown_base_version_is_stale_write(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "notes.md", "kept")

    with pytest.raises(OrgFsError) as error:
        fs.write_text(space.space_id, "notes.md", "lost", base_version="bogus")

    assert error.value.code == "stale-write"
    assert error.value.details.get("baseVersion") == "bogus"
    assert fs.read_text(space.space_id, "notes.md")[0] == "kept"


def test_bytes_expect_version_mismatch_is_stale_write(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first = fs.write_bytes(space.space_id, "data.bin", b"one")
    fs.write_bytes(space.space_id, "data.bin", b"two")

    with pytest.raises(OrgFsError) as error:
        fs.write_bytes(space.space_id, "data.bin", b"three", expect_version=first.version)

    assert error.value.code == "stale-write"
    assert fs.read_bytes(space.space_id, "data.bin") == b"two"
    current = fs.stat(space.space_id, "data.bin").version
    fs.write_bytes(space.space_id, "data.bin", b"four", expect_version=current)
    assert fs.read_bytes(space.space_id, "data.bin") == b"four"
