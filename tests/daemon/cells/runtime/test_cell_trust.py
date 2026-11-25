"""Who may occupy a cell id, and how the authority treats documents it cannot adopt.

Since OC1a (contract OC0 D1) nothing repairs: an unadoptable document at a
held cell's path blocks the local write to that space and keeps its bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hyprial.daemon.impl.cells.codec import encode
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellScope
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.cells.runtime.agents import register_agents
from hyprial.identity import org_space_name
from hyprial.kernel import keyed_cell_id, subject_cell_id

from tests.daemon.cells.runtime.cell_runtime_support import (
    ALICE,
    MALLORY,
    ORG,
    authority_for,
    org_fs,
    runtime,
)

BOB = authority_for("user:bob")

AGENT_ID = "a." + "b" * 26


def _plant(fs: Any, space: str, cell: Cell, author: str) -> None:
    for path in ("cells", f"cells/{cell.kind}"):
        try:
            fs.mkdir(space, path)
        except Exception:  # noqa: BLE001 - directory already there
            pass
    fs.author, previous = author, fs.author
    try:
        fs.write_text(space, f"cells/{cell.kind}/{cell.cell_id}.json", encode(cell))
    finally:
        fs.author = previous


def _document(fs: Any, space: str, kind: str, cell_id: str) -> dict[str, Any]:
    content, _ = fs.read_text(space, f"cells/{kind}/{cell_id}.json")
    return json.loads(content)


def _agent(owner: str = "alice") -> Any:
    return SimpleNamespace(
        agent_id=AGENT_ID,
        cell_id=subject_cell_id("agent", AGENT_ID),
        uri=f"agent:{owner}:mac:a",
        actor="a",
        owner=owner,
    )


def _team_cell() -> CellSpec:
    return CellSpec(kind="agent", binding=CellBinding.keyed("eng"),
                    scope=CellScope.of_org(ORG))


def test_a_foreign_agent_cell_at_the_local_path_blocks_only_the_local_write(
    tmp_path: Path,
) -> None:
    fs, space = org_fs()
    fs.invite(space, "user:mallory")
    cell_id = subject_cell_id("agent", AGENT_ID)
    forged = Cell(cell_id=cell_id, kind="agent", authority=MALLORY,
                  scope=CellScope.of_org(), binding=CellBinding.subject(AGENT_ID),
                  name="hijack", params={"delivery.bodyRetentionMs": 3_600_000})
    _plant(fs, space, forged, "user:mallory")
    path = f"cells/agent/{cell_id}.json"
    before = fs.read_text(space, path)
    cells, log = runtime(tmp_path, fs)
    cells.run_once()
    assert cells.lookup(cell_id) is None
    (registered,) = register_agents(cells, [_agent()])
    assert registered.authority == ALICE and registered.cell_id == cell_id
    cells.run_once()
    cells.run_once()
    assert cells.lookup(cell_id) == registered
    assert cells.dirty() == frozenset({cell_id})
    assert fs.read_text(space, path) == before
    assert fs.document_authors(space, path) == ("user:mallory",)
    assert log.names().count("cell.document_blocked") == 1
    assert "cell.document_repaired" not in log.names()


def test_subject_cells_of_another_authority_are_not_indexed(tmp_path: Path) -> None:
    fs, space = org_fs()
    fs.invite(space, "user:bob")
    bob = Cell(cell_id=subject_cell_id("agent", AGENT_ID), kind="agent",
               authority=BOB, scope=CellScope.of_org(),
               binding=CellBinding.subject(AGENT_ID))
    _plant(fs, space, bob, "user:bob")
    cells, _ = runtime(tmp_path, fs)
    cells.run_once()
    cells.run_once()
    assert cells.index().of_kind("agent") == ()


def test_a_removed_document_is_recreated_on_the_next_write(tmp_path: Path) -> None:
    fs, space = org_fs()
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(_team_cell())
    cells.run_once()
    fs.remove(space, f"cells/agent/{cell.cell_id}.json")
    cells.update(cell.cell_id, name="x")
    cells.run_once()
    assert cells.dirty() == frozenset()
    assert _document(fs, space, "agent", cell.cell_id)["revision"] == 2


def test_a_foreign_edit_blocks_the_write_and_keeps_its_bytes(tmp_path: Path) -> None:
    fs, space = org_fs()
    fs.invite(space, "user:mallory")
    cells, log = runtime(tmp_path, fs)
    cell = cells.ensure(_team_cell())
    cells.run_once()
    path = f"cells/agent/{cell.cell_id}.json"
    text, version = fs.read_text(space, path)
    fs.author = "user:mallory"
    fs.write_text(space, path, text.replace('"name":""', '"name":"owned"'),
                  expect_version=version)
    fs.author = "user:alice"
    edited = fs.read_text(space, path)
    cells.run_once()
    cells.run_once()
    assert fs.read_text(space, path) == edited
    assert fs.document_authors(space, path) == ("user:alice", "user:mallory")
    assert _document(fs, space, "agent", cell.cell_id)["name"] == "owned"
    assert cells.lookup(cell.cell_id) == cell
    assert cells.dirty() == frozenset({cell.cell_id})
    assert log.names().count("cell.document_blocked") == 1
    assert "cell.document_repaired" not in log.names()


def test_a_blocked_copy_is_logged_once_per_document_version(tmp_path: Path) -> None:
    fs, space = org_fs()
    cells, log = runtime(tmp_path, fs)
    cell = cells.ensure(_team_cell())
    fs.invite(space, "user:mallory")
    other = Cell(cell_id=cell.cell_id, kind="agent", authority=ALICE,
                 scope=CellScope.of_org(ORG), binding=CellBinding.keyed("eng"))
    _plant(fs, space, other, "user:mallory")
    for _ in range(3):
        cells.run_once()
    assert cells.dirty() == frozenset({cell.cell_id})
    blocked = [fields for _, event, fields in log.events if event == "cell.document_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "author-mismatch" and blocked[0]["blocked"] == 1
    path = f"cells/agent/{cell.cell_id}.json"
    text, version = fs.read_text(space, path)
    fs.author = "user:mallory"
    fs.write_text(space, path, text.replace('"name":""', '"name":"again"'),
                  expect_version=version)
    fs.author = "user:alice"
    cells.run_once()
    cells.run_once()
    assert [event for _, event, _ in log.events].count("cell.document_blocked") == 2


def test_same_revision_copies_converge_on_one_winner(tmp_path: Path) -> None:
    fs, _first = org_fs()
    writer, _ = runtime(tmp_path, fs, name="writer.sqlite3")
    agent = writer.ensure(CellSpec(kind="agent", binding=CellBinding.subject(AGENT_ID)))
    writer.run_once()
    reader, _ = runtime(tmp_path, fs, name="reader.sqlite3")
    reader.run_once()
    assert reader.lookup(agent.cell_id) == agent
    second = fs.create_space(org_space_name("group-beta")).space_id
    rival = Cell(cell_id=agent.cell_id, kind="agent", authority=ALICE,
                 scope=CellScope.of_org(), binding=CellBinding.subject(AGENT_ID),
                 name="rival", updated_at_ms=agent.updated_at_ms + 10)
    _plant(fs, second, rival, "user:alice")
    reader.run_once()
    writer.run_once()
    assert reader.lookup(agent.cell_id) == rival
    assert writer.lookup(agent.cell_id) == rival


def test_keyed_ids_of_another_authority_cannot_collide(tmp_path: Path) -> None:
    assert keyed_cell_id("org", ALICE, "sp-1") != keyed_cell_id(
        "org", MALLORY, "sp-1"
    )
