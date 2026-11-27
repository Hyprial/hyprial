# Decision record (OrgFS package S, 2026-11-25): the journal key builders
# (``orgfs_log*``, ``orgfs_sync``, ``orgfs_snapshot``) are deleted with the
# journal mesh.  Generic query tests use the protocol 3 sync key; the journal
# key layout is spelled literally where a test still proves it is unused.
"""Key layout of the OrgFS protocol 3 sync and broadcast channels."""

from __future__ import annotations

from fnmatch import fnmatch

import pytest

from hyprial.daemon.impl.transport import KeySpace


def test_orgfs3_keys_are_canonical() -> None:
    keys = KeySpace()

    assert keys.orgfs3_sync("space", "node-a") == "hyprial/v1/org/fs3/sync/space/node-a"
    assert keys.orgfs3_update("space") == "hyprial/v1/org/fs3/update/space"


@pytest.mark.parametrize(
    ("space_id", "node"),
    [("space/x", "node"), ("space", "node/x"), ("", "node"), ("space", ""), ("space", "*")],
)
def test_orgfs3_keys_reject_unsafe_segments(space_id: str, node: str) -> None:
    keys = KeySpace()

    with pytest.raises(ValueError):
        keys.orgfs3_sync(space_id, node)
    if space_id != "space":
        with pytest.raises(ValueError):
            keys.orgfs3_update(space_id)


def test_orgfs3_keys_are_disjoint_from_the_journal_keys() -> None:
    keys = KeySpace()
    legacy = (
        f"{keys.prefix}/org/fs/space/log/**",
        f"{keys.prefix}/org/fs/space/sync/node",
        keys.orgfs_announce("node"),
    )
    for expression in legacy:
        for key in (keys.orgfs3_sync("space", "node"), keys.orgfs3_update("space")):
            assert not fnmatch(key, expression.replace("**", "*"))


def test_orgfs_blob_any_is_the_handler_wildcard_of_orgfs_blob() -> None:
    keys = KeySpace()

    assert keys.orgfs_blob_any() == "hyprial/v1/org/fs/blob/*"
    assert fnmatch(keys.orgfs_blob("a" * 64), keys.orgfs_blob_any())
