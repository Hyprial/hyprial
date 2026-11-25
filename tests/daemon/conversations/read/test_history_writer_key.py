"""Conversation keys stay on the OrgFS writer after the uid authority (contract OC0 §1.6, I-1, I-9)."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon.impl.application.messaging.inbox_surface.history import (
    _conversation_authority,
)
from hyprial.daemon.impl.cells.runtime import CellRuntime
from hyprial.daemon.impl.composition.conversations import OwnerHomePlacement
from hyprial.daemon.impl.orgfs import LocalOrgFs

from tests.daemon.cells.runtime.cell_runtime_support import ALICE, identity_for

SRC = Path(__file__).resolve().parents[4] / "src" / "hyprial"


def test_the_history_key_is_the_composed_runtime_writer_never_the_uid() -> None:
    composed = SimpleNamespace(owner="alice", _cell_runtime=SimpleNamespace(
        identity=identity_for("user:alice")))
    assert _conversation_authority(composed) == "user:alice"
    assert _conversation_authority(composed) != ALICE


def test_a_facade_author_other_than_the_owner_keeps_the_facade_key() -> None:
    composed = SimpleNamespace(owner="alice", _cell_runtime=SimpleNamespace(
        identity=identity_for("user:alice-device")))
    assert _conversation_authority(composed) == "user:alice-device"


def test_without_a_composed_runtime_the_history_key_is_the_owner() -> None:
    assert _conversation_authority(SimpleNamespace(owner="alice", _cell_runtime=None)) == (
        "user:alice"
    )
    assert _conversation_authority(SimpleNamespace(owner="user:alice")) == "user:alice"


def test_the_runtime_has_no_author_and_nothing_reads_one() -> None:
    assert not hasattr(CellRuntime, "author")
    reads = re.compile(r"getattr\([^)]*_cell_runtime[^)]*\)[^\n]*[\"']author[\"']"
                       r"|getattr\(runtime, [\"']author[\"']")
    hits = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if reads.search(line)
    ]
    assert hits == []


def test_conversation_placement_never_needs_the_cell_authority() -> None:
    fs = LocalOrgFs(author="user:alice")
    app = SimpleNamespace(owner="alice", user_id=None)
    assert OwnerHomePlacement(app, fs=fs).authority() == "user:alice"
    placement = (SRC / "daemon/impl/composition/conversations/placement.py").read_text(
        encoding="utf-8"
    )
    assert "local_cell_authority" not in placement
