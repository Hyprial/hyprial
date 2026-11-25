"""CellRuntimePort semantics: ensure/update/retire/lookup/index/hooks."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.cells.model import CellBinding, CellError, CellMember, CellScope
from hyprial.daemon.impl.cells.ports import CellRuntimePort, CellSpec
from hyprial.kernel import keyed_cell_id, mint_cell_id

from tests.daemon.cells.runtime.cell_runtime_support import ORG, org_fs, runtime
from tests.daemon.cells.runtime.cell_runtime_support import ALICE


def _agent_spec(key: str = "eng") -> CellSpec:
    return CellSpec(
        kind="agent",
        binding=CellBinding.keyed(key),
        scope=CellScope.of_org(ORG),
        name="Engineering",
        members=(CellMember("agent:alice:mac:a"),),
        params={"delivery.bodyRetentionMs": 3_600_000},
    )


def test_the_runtime_implements_the_port(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    port: CellRuntimePort = cells
    assert port.index().generation == 1


def test_ensure_is_idempotent_for_a_keyed_binding(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    first = cells.ensure(_agent_spec())
    assert first.cell_id == keyed_cell_id("agent", ALICE, "eng")
    assert first.authority == ALICE and first.revision == 1
    again = cells.ensure(_agent_spec())
    assert again is first
    assert cells.lookup(first.cell_id) is first
    assert cells.index().of_kind("agent") == (first,)


def test_minted_specs_always_create(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    spec = CellSpec(kind="agent", binding=CellBinding.minted())
    assert cells.ensure(spec).cell_id != cells.ensure(spec).cell_id


def test_update_revises_params_and_honours_expect_revision(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(_agent_spec())
    generation = cells.index().generation
    updated = cells.update(cell.cell_id, params={"delivery.bodyRetentionMs": 7_200_000},
                           expect_revision=1)
    assert updated.revision == 2
    assert updated.param("delivery.bodyRetentionMs") == 7_200_000
    assert cells.index().generation == generation + 1
    with pytest.raises(CellError) as raised:
        cells.update(cell.cell_id, name="x", expect_revision=1)
    assert raised.value.code == "stale-revision"
    assert cells.update(cell.cell_id, name="Engineering") is updated


def test_retire_is_a_tombstone_revision(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(_agent_spec())
    retired = cells.retire(cell.cell_id)
    assert retired.retired and retired.revision == 2
    assert cells.index().of_kind("agent") == ()
    assert cells.index().of_kind("agent", include_retired=True) == (retired,)
    for call in (
        lambda: cells.update(cell.cell_id, name="y"),
        lambda: cells.retire(cell.cell_id),
        lambda: cells.ensure(_agent_spec()),
    ):
        with pytest.raises(CellError) as raised:
            call()
        assert raised.value.code == "retired"


def test_unknown_cells_are_refused(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    with pytest.raises(CellError) as raised:
        cells.update("c." + "a" * 26, name="x")
    assert raised.value.code == "unknown-cell"


def test_index_hooks_run_after_a_pass_and_are_fenced(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, log = runtime(tmp_path, fs)
    seen: list[int] = []

    def broken(_generation: int) -> None:
        raise RuntimeError("boom")

    cells.on_index(broken)
    unregister = cells.on_index(seen.append)
    cells.ensure(_agent_spec())
    generation = cells.run_once()
    assert seen == [generation]
    assert "cell.index_hook_failed" in log.names()
    cells.run_once()
    assert seen == [generation]
    unregister()
    cells.ensure(_agent_spec("ops"))
    cells.run_once()
    assert seen == [generation]


def test_the_runtime_thread_flushes_on_kick_and_closes(tmp_path: Path) -> None:
    import threading

    fs, space = org_fs()
    cells, _ = runtime(tmp_path, fs, interval_s=30.0)
    flushed = threading.Event()
    cells.on_index(lambda _generation: flushed.set() if not cells.dirty() else None)
    cells.start()
    try:
        cell = cells.ensure(_agent_spec())
        assert flushed.wait(5.0)
        assert fs.read_text_snapshot(space, f"cells/agent/{cell.cell_id}.json")
    finally:
        assert cells.close(5.0)
    assert not any(thread.name == "cell-runtime" for thread in threading.enumerate())


def test_lookup_only_answers_held_cells_and_follows_aliases(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    held = cells.ensure(_agent_spec())
    cells.run_once()
    assert cells.lookup(held.cell_id) is held
    assert cells.lookup(mint_cell_id()) is None
    assert cells.lookup("not-a-cell-id") is None
