# Decisions:
# - Phase D, package T2: ``test_concurrent_text_edits_from_same_base_do_not_duplicate_base``
#   is deleted.  Its user-visible result is contract
#   ``multinode/test_convergence.py::test_concurrent_edits_from_one_base_keep_both_changes``
#   (C4 report §5); the three-node form with the frontier repair stopping is
#   ``runtime_state/test_multinode.py::test_concurrent_edits_from_one_base_converge_and_frontier_repair_stops``.
#   The remaining ``_ContentDocument`` tests are unchanged.
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
