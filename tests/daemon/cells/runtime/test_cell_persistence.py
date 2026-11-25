"""Cell persistence to OrgFS: expect_version writes, verification, convergence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.codec import encode
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellScope
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.cells.runtime import CellCache
from hyprial.daemon.impl.cells.runtime.mirror import CellMirror, Rejection
from hyprial.daemon.impl.composition.cells.spaces import CellSpaces, OWNER_HOME_KIND
from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.identity import org_space_name
from hyprial.kernel import keyed_cell_id

from tests.daemon.cells.runtime.cell_runtime_support import (
    ORG,
    RETIRED_CONVERSATION_DOCUMENT,
    RETIRED_CONVERSATION_ID,
    RETIRED_CONVERSATION_PATH,
    RETIRED_GRAPH_DOCUMENT,
    RETIRED_GRAPH_ID,
    RETIRED_GRAPH_PATH,
    RETIRED_ROUTINE_DOCUMENT,
    RETIRED_ROUTINE_ID,
    RETIRED_ROUTINE_PATH,
    org_fs,
    runtime,
)
from tests.daemon.cells.runtime.cell_runtime_support import ALICE


def _spec(key: str = "eng", **overrides: Any) -> CellSpec:
    fields: dict[str, Any] = {
        "kind": "agent",
        "binding": CellBinding.keyed(key),
        "scope": CellScope.of_org(ORG),
        "params": {"delivery.bodyRetentionMs": 3_600_000},
    }
    fields.update(overrides)
    return CellSpec(**fields)


def _read(fs: Any, space: str, cell: Cell) -> dict[str, Any]:
    content, _ = fs.read_text(space, f"cells/{cell.kind}/{cell.cell_id}.json")
    return json.loads(content)


class _RecordingFs:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.writes: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def write_text(self, space: str, node: str, content: str, **options: Any) -> Any:
        self.writes.append(dict(options, node=node))
        return self._inner.write_text(space, node, content, **options)


def test_a_pass_writes_the_document_with_create_only_then_expect_version(tmp_path: Path) -> None:
    inner, space = org_fs()
    fs = _RecordingFs(inner)
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(_spec())
    cells.run_once()
    assert cells.dirty() == frozenset()
    assert _read(inner, space, cell)["revision"] == 1
    assert fs.writes[0]["create_only"] is True
    _, version = inner.read_text(space, f"cells/agent/{cell.cell_id}.json")
    cells.update(cell.cell_id, name="Eng")
    cells.run_once()
    assert fs.writes[1]["expect_version"] == version
    assert _read(inner, space, cell)["name"] == "Eng"
    cells.run_once()
    assert len(fs.writes) == 2


def test_lookups_answer_from_the_cache_while_orgfs_is_unreachable(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(_spec())
    cells.run_once()
    cells.close()

    class Down:
        author = "user:alice"

        def __getattr__(self, _name: str) -> Any:
            def fail(*_args: Any, **_kwargs: Any) -> Any:
                raise OrgFsError("unavailable")

            return fail

    offline, log = runtime(tmp_path, Down())
    assert offline.lookup(cell.cell_id) == cell
    revised = offline.update(cell.cell_id, name="offline edit")
    assert offline.run_once() == offline.index().generation
    assert offline.lookup(cell.cell_id) == revised
    assert offline.dirty() == frozenset({cell.cell_id})
    assert "cell.spaces_unavailable" in log.names()
    offline.close()
    back, _ = runtime(tmp_path, fs)
    assert back.dirty() == frozenset({cell.cell_id})
    back.run_once()
    assert back.dirty() == frozenset()


def test_a_second_device_of_the_same_owner_converges(tmp_path: Path) -> None:
    fs, space = org_fs()
    first, _ = runtime(tmp_path, fs, name="first.sqlite3")
    second, log = runtime(tmp_path, fs, name="second.sqlite3")
    a = first.ensure(_spec())
    first.run_once()
    first.update(a.cell_id, name="newer")
    first.run_once()
    b = second.ensure(_spec())
    assert b.revision == 1
    second.run_once()
    assert second.lookup(a.cell_id).name == "newer"
    assert second.dirty() == frozenset()
    assert "cell.intent_dropped" in log.names()
    assert _read(fs, space, a)["revision"] == 2


def test_a_newer_local_intent_rewrites_after_a_stale_write(tmp_path: Path) -> None:
    fs, space = org_fs()
    first, _ = runtime(tmp_path, fs, name="first.sqlite3")
    second, _ = runtime(tmp_path, fs, name="second.sqlite3")
    a = first.ensure(_spec())
    first.run_once()
    second.run_once()
    second.update(a.cell_id, name="s1")
    second.update(a.cell_id, name="s2")
    first.update(a.cell_id, name="f1")
    first.run_once()
    second.run_once()
    assert _read(fs, space, a)["name"] == "s2"
    assert second.dirty() == frozenset()
    first.run_once()
    assert first.lookup(a.cell_id).name == "s2"


def test_documents_not_authored_by_their_authority_are_ignored(tmp_path: Path) -> None:
    fs, space = org_fs()
    fs.invite(space, "user:mallory")
    forged = Cell(
        cell_id=keyed_cell_id("agent", ALICE, "eng"),
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org(ORG),
        binding=CellBinding.keyed("eng"),
        name="forged",
    )
    fs.mkdir(space, "cells")
    fs.mkdir(space, "cells/agent")
    fs.author = "user:mallory"
    fs.write_text(space, f"cells/agent/{forged.cell_id}.json", encode(forged))
    fs.author = "user:alice"
    cells, log = runtime(tmp_path, fs)
    cells.run_once()
    assert cells.lookup(forged.cell_id) is None
    assert "cell.document_rejected" in log.names()


def test_owner_scope_cells_go_to_a_tagged_owner_home_space(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    before = {info.space_id for info in fs.spaces()}
    homed = cells.ensure(
        CellSpec(
            kind="agent",
            binding=CellBinding.keyed("home"),
            scope=CellScope.owner(),
        )
    )
    assert homed.scope == CellScope.owner()
    cells.run_once()
    created = [info for info in fs.spaces() if info.space_id not in before]
    assert len(created) == 1
    home = created[0]
    assert home.owner == "user:alice"
    assert fs.space_meta(home.space_id)["kind"] == OWNER_HOME_KIND
    assert _read(fs, home.space_id, homed)["kind"] == "agent"
    cells.ensure(
        CellSpec(
            kind="agent", binding=CellBinding.keyed("chat"), scope=CellScope.owner()
        )
    )
    cells.run_once()
    assert len(fs.spaces()) == len(before) + 1


def _owner_home(fs: Any) -> Any:
    spaces = CellSpaces(fs)
    homes = spaces.home_spaces("user:alice")
    return homes[0] if homes else spaces.private_space("user:alice")


def _home_with_a_retired_graph_file(tmp_path: Path) -> tuple[Any, Any]:
    fs, _ = org_fs()
    home = _owner_home(fs)
    fs.mkdir(home.space_id, "cells")
    fs.mkdir(home.space_id, "cells/graph")
    fs.write_text(home.space_id, RETIRED_GRAPH_PATH, RETIRED_GRAPH_DOCUMENT)
    return fs, home


def test_a_graph_file_left_by_an_earlier_build_is_ignored_and_kept(tmp_path: Path) -> None:
    fs, home = _home_with_a_retired_graph_file(tmp_path)
    seeded = CellCache(tmp_path / "cells.sqlite3")
    seeded._connection.execute(
        "INSERT INTO cells (cell_id, kind, authority, revision, dirty, document)"
        " VALUES (?, 'graph', 'user:alice', 1, 1, ?)",
        (RETIRED_GRAPH_ID, RETIRED_GRAPH_DOCUMENT),
    )
    seeded.close()
    cells, log = runtime(tmp_path, fs)
    assert cells.lookup(RETIRED_GRAPH_ID) is None
    assert cells.dirty() == frozenset()
    cells.run_once()
    cells.run_once()
    assert RETIRED_GRAPH_ID not in cells.index().cells
    assert log.names() == ["cell.cache_rows_skipped"]
    content, _ = fs.read_text(home.space_id, RETIRED_GRAPH_PATH)
    assert content == RETIRED_GRAPH_DOCUMENT
    cells.close()


def test_reading_a_retired_graph_file_by_path_is_a_rejection_not_an_error(
    tmp_path: Path,
) -> None:
    fs, home = _home_with_a_retired_graph_file(tmp_path)
    result = CellMirror(fs).read(home, "graph", RETIRED_GRAPH_ID, {ALICE: "user:alice"}.get)
    assert isinstance(result, Rejection)
    assert result.reason == "invalid-cell"
    assert CellMirror(fs).scan(home, "graph", {ALICE: "user:alice"}.get) == (result,)
    content, _ = fs.read_text(home.space_id, RETIRED_GRAPH_PATH)
    assert content == RETIRED_GRAPH_DOCUMENT


def _org_with_a_retired_routine_file(tmp_path: Path) -> tuple[Any, str]:
    fs, space = org_fs()
    writer, _ = runtime(tmp_path, fs, name="writer.sqlite3")
    writer.ensure(_spec())
    writer.run_once()
    writer.close()
    fs.mkdir(space, "cells/routine")
    fs.write_text(space, RETIRED_ROUTINE_PATH, RETIRED_ROUTINE_DOCUMENT)
    return fs, space


def test_a_routine_file_left_by_an_earlier_build_is_ignored_and_kept(tmp_path: Path) -> None:
    fs, space = _org_with_a_retired_routine_file(tmp_path)
    seeded = CellCache(tmp_path / "cells.sqlite3")
    seeded._connection.execute(
        "INSERT INTO cells (cell_id, kind, authority, revision, dirty, document)"
        " VALUES (?, 'routine', 'user:alice', 2, 1, ?)",
        (RETIRED_ROUTINE_ID, RETIRED_ROUTINE_DOCUMENT),
    )
    seeded.close()
    cells, log = runtime(tmp_path, fs)
    assert cells.lookup(RETIRED_ROUTINE_ID) is None
    assert cells.dirty() == frozenset()
    cells.run_once()
    cells.run_once()
    assert [cell.kind for cell in cells.index().cells.values()] == ["agent"]
    assert log.names() == ["cell.cache_rows_skipped"]
    content, _ = fs.read_text(space, RETIRED_ROUTINE_PATH)
    assert content == RETIRED_ROUTINE_DOCUMENT
    cells.close()


def test_reading_a_retired_routine_file_by_path_is_a_rejection_not_an_error(
    tmp_path: Path,
) -> None:
    fs, space_id = _org_with_a_retired_routine_file(tmp_path)
    (space,) = CellSpaces(fs).org_spaces().values()
    result = CellMirror(fs).read(space, "routine", RETIRED_ROUTINE_ID, {ALICE: "user:alice"}.get)
    assert isinstance(result, Rejection)
    assert result.reason == "invalid-cell"
    assert CellMirror(fs).scan(space, "routine", {ALICE: "user:alice"}.get) == (result,)
    content, _ = fs.read_text(space_id, RETIRED_ROUTINE_PATH)
    assert content == RETIRED_ROUTINE_DOCUMENT


def _home_with_a_retired_conversation_file(tmp_path: Path) -> tuple[Any, Any]:
    fs, _ = org_fs()
    home = _owner_home(fs)
    fs.mkdir(home.space_id, "cells")
    fs.mkdir(home.space_id, "cells/conversation")
    fs.write_text(home.space_id, RETIRED_CONVERSATION_PATH, RETIRED_CONVERSATION_DOCUMENT)
    return fs, home


def test_a_conversation_file_left_by_an_earlier_build_is_ignored_and_kept(
    tmp_path: Path,
) -> None:
    fs, home = _home_with_a_retired_conversation_file(tmp_path)
    seeded = CellCache(tmp_path / "cells.sqlite3")
    seeded._connection.execute(
        "INSERT INTO cells (cell_id, kind, authority, revision, dirty, document)"
        " VALUES (?, 'conversation', 'user:alice', 1, 1, ?)",
        (RETIRED_CONVERSATION_ID, RETIRED_CONVERSATION_DOCUMENT),
    )
    seeded.close()
    cells, log = runtime(tmp_path, fs)
    assert cells.lookup(RETIRED_CONVERSATION_ID) is None
    assert cells.dirty() == frozenset()
    cells.run_once()
    cells.run_once()
    assert RETIRED_CONVERSATION_ID not in cells.index().cells
    assert log.names() == ["cell.cache_rows_skipped"]
    content, _ = fs.read_text(home.space_id, RETIRED_CONVERSATION_PATH)
    assert content == RETIRED_CONVERSATION_DOCUMENT
    cells.close()


def test_reading_a_retired_conversation_file_by_path_is_a_rejection_not_an_error(
    tmp_path: Path,
) -> None:
    fs, home = _home_with_a_retired_conversation_file(tmp_path)
    result = CellMirror(fs).read(home, "conversation", RETIRED_CONVERSATION_ID, {ALICE: "user:alice"}.get)
    assert isinstance(result, Rejection)
    assert result.reason == "invalid-cell"
    content, _ = fs.read_text(home.space_id, RETIRED_CONVERSATION_PATH)
    assert content == RETIRED_CONVERSATION_DOCUMENT


def test_reading_spaces_never_creates_an_owner_home(tmp_path: Path) -> None:
    fs, _ = org_fs()
    count = len(fs.spaces())
    cells, _ = runtime(tmp_path, fs)
    cells.run_once()
    assert len(fs.spaces()) == count


def test_any_org_cells_reach_a_newly_joined_org(tmp_path: Path) -> None:
    fs, first_space = org_fs()
    cells, _ = runtime(tmp_path, fs)
    agent = cells.ensure(
        CellSpec(kind="agent", binding=CellBinding.subject("a." + "b" * 26))
    )
    cells.run_once()
    assert _read(fs, first_space, agent)["kind"] == "agent"
    second = fs.create_space(org_space_name("group-beta")).space_id
    cells.run_once()
    cells.run_once()
    assert _read(fs, second, agent)["cellId"] == agent.cell_id
    assert cells.dirty() == frozenset()


def test_left_orgs_are_not_written(tmp_path: Path) -> None:
    fs, space = org_fs()
    cells, _ = runtime(tmp_path, fs, spaces=CellSpaces(fs, left_orgs=lambda: {ORG}))
    cell = cells.ensure(_spec())
    cells.run_once()
    assert cells.dirty() == frozenset({cell.cell_id})
    try:
        fs.read_text(space, f"cells/agent/{cell.cell_id}.json")
    except OrgFsError as error:
        assert error.code == "unknown-doc"
    else:
        raise AssertionError("a left org received a cell")


def test_the_cache_file_is_the_one_named_by_the_runtime(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(_spec())
    cells.close()
    assert CellCache(tmp_path / "cells.sqlite3").load()[cell.cell_id][1] is True
