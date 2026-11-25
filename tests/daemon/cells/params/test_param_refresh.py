"""CellParamView follows the runtime's index generations through the S3 hook."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.model import CellBinding, CellMember
from hyprial.daemon.impl.cells.params import (
    DELIVERY_BODY_RETENTION_MS,
    CellParamView,
    resolve_param,
)
from hyprial.daemon.impl.cells.ports import CellIndex, CellSpec

from tests.daemon.cells.params.param_support import AGENT_ID, AGENT_URI, OWNER, STRANGER, agent
from tests.daemon.cells.runtime.cell_runtime_support import org_fs, runtime

HOUR_MS = 3_600_000
KEY = DELIVERY_BODY_RETENTION_MS.key


def _agent(cells: Any, value: int) -> Any:
    return cells.ensure(
        CellSpec(
            kind="agent",
            binding=CellBinding.subject(AGENT_ID),
            members=(CellMember(AGENT_URI, "harness"),),
            params={KEY: value},
        )
    )


def test_the_view_rebuilds_from_the_index_hook_on_a_new_generation(
    tmp_path: Path,
) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    view = CellParamView(cells, owner=cells.identity.authority).bind()
    try:
        before = view.current()
        assert (
            resolve_param(before, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 604_800_000
        )
        _agent(cells, 2 * HOUR_MS)
        assert view.current() is before
        cells.run_once()
        after = view.current()
        assert after.generation == cells.index().generation
        assert after.generation > before.generation
        assert (
            resolve_param(after, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 2 * HOUR_MS
        )
    finally:
        view.close()
        cells.close()


def test_snapshot_reads_catch_up_with_a_local_write_before_the_next_pass(
    tmp_path: Path,
) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    view = CellParamView(cells, owner=cells.identity.authority)
    try:
        first = view.snapshot()
        assert view.snapshot() is first
        _agent(cells, 3 * HOUR_MS)
        fresh = view.snapshot()
        assert fresh is not first
        assert (
            resolve_param(fresh, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 3 * HOUR_MS
        )
    finally:
        cells.close()


def test_closing_the_view_unregisters_its_hook(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    view = CellParamView(cells, owner=cells.identity.authority).bind()
    view.close()
    try:
        before = view.current()
        _agent(cells, 2 * HOUR_MS)
        cells.run_once()
        assert view.current() is before
    finally:
        cells.close()


class _FlakyRuntime:
    def __init__(self, index: CellIndex) -> None:
        self.indexed = index
        self.failing = False
        self.hooks: list[Any] = []

    def index(self) -> CellIndex:
        if self.failing:
            raise RuntimeError("index unavailable")
        return self.indexed

    def on_index(self, hook: Any) -> Any:
        self.hooks.append(hook)
        return lambda: self.hooks.remove(hook)


def test_a_failing_refresh_keeps_the_last_known_good_snapshot_and_logs() -> None:
    owned = agent({KEY: 2 * HOUR_MS})
    flaky = _FlakyRuntime(CellIndex(4, {owned.cell_id: owned}))
    logged: list[str] = []
    view = CellParamView(
        flaky,
        owner=owned.authority,
        logger=lambda _lvl, _dom, event, **_f: logged.append(event),
    )
    good = view.snapshot()
    flaky.failing = True
    assert view.snapshot() is good
    assert "cell.params_refresh_failed" in logged
    assert resolve_param(good, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 2 * HOUR_MS


def test_untrusted_sources_are_logged_once_per_change() -> None:
    foreign = agent({KEY: 2 * HOUR_MS}, authority=STRANGER)
    flaky = _FlakyRuntime(CellIndex(4, {foreign.cell_id: foreign}))
    logged: list[tuple[str, str, dict]] = []
    view = CellParamView(
        flaky,
        owner=OWNER,
        logger=lambda lvl, _dom, event, **f: logged.append((lvl, event, f)),
    )
    view.snapshot()
    flaky.indexed = CellIndex(5, {foreign.cell_id: foreign})
    view.snapshot()
    rejected = [
        (lvl, fields)
        for lvl, event, fields in logged
        if event == "cell.params_untrusted"
    ]
    assert len(rejected) == 1
    assert rejected[0][0] == "info"
    assert rejected[0][1]["cells"] == [foreign.cell_id]
