# Decisions:
# - Phase D, package T2.  The two loopback twins that failed on the state
#   runtime are deleted: ``test_concurrent_text_edits_from_same_base_do_not_duplicate_base``
#   is contract ``multinode/test_convergence.py::test_concurrent_edits_from_one_base_keep_both_changes``
#   (C4 report §5); ``test_review_fix_6_fetches_missing_update_blob_and_retries_once``
#   runs on ``StateOrgFsRuntime`` over loopback Zenoh in
#   ``tests/daemon/orgfs/mesh/replication/blobs/test_blobs.py`` (its ``replica``
#   reference reason belonged to the journal's update-blob recovery; protocol
#   3 pins a temporary update blob under ``sync``).
# - The ``_ContentDocument`` tests are unchanged.
"""actor_preserved_orgfs_mesh_rework: document merge scenarios."""

from __future__ import annotations

import pytest
from hyprial.daemon.impl.orgfs.document.model import _ContentDocument


@pytest.mark.parametrize(
    ("initial", "updated"),
    [
        ("", "new text"),
        ("old text", ""),
        ("甲😀乙", "甲🐍中乙"),
        ("甲😀乙猫丙", "甲🐍乙犬丙"),
    ],
)
def test_content_document_set_handles_edge_edits(initial: str, updated: str) -> None:
    document = _ContentDocument(initial)
    if initial == "甲😀乙":
        assert len(document.text) == len(initial.encode())

    document.set(updated)

    assert document.value() == updated


def test_content_document_set_handles_large_text() -> None:
    prefix = "a" * 450_000
    suffix = "z" * 450_000
    document = _ContentDocument(prefix + "before" + suffix)

    document.set(prefix + "after" + suffix)

    assert document.value() == prefix + "after" + suffix


def test_content_document_set_no_op_preserves_crdt_bytes() -> None:
    document = _ContentDocument("unchanged 中文 😀")
    before = document.export()

    document.set(document.value())

    assert document.export() == before
