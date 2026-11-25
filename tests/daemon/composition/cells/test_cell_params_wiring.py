"""composition.cells.cell_params(app): the runtime's param snapshot, or None."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.cells.model import CellBinding, CellMember
from hyprial.daemon.impl.cells.params import (
    DELIVERY_BODY_RETENTION_MS,
    CellParamSnapshot,
    resolve_param,
)
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.composition.cells import (
    cell_params,
    cell_runtime,
    close_cell_runtime,
)
from hyprial.daemon.impl.composition.cells import params as params_wiring
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.identity import org_space_name
from tests.daemon.cells.runtime.cell_runtime_support import ALICE, uid_for

AGENT_ID = "a." + "w" * 26
AGENT_URI = "agent:alice:mac:worker"
HOUR_MS = 3_600_000
DEFAULT = 604_800_000
KEY = DELIVERY_BODY_RETENTION_MS.key


def _app(tmp_path: Path) -> Any:
    fs = LocalOrgFs(author="user:alice")
    fs.create_space(org_space_name("group-acme"))
    logged: list[str] = []
    return SimpleNamespace(
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        owner="alice",
        user_id=uid_for("user:alice"),
        user_issuer="",
        _require_orgfs_runtime=lambda: SimpleNamespace(facade=fs),
        _log=lambda _level, _component, event, **_fields: logged.append(event),
        logged=logged,
        fs=fs,
    )


def _retention(cells: Any, value: int) -> None:
    cells.ensure(
        CellSpec(
            kind="agent",
            binding=CellBinding.subject(AGENT_ID),
            members=(CellMember(AGENT_URI, "harness"),),
            params={KEY: value},
        )
    )


def test_cell_params_follows_the_composed_runtime(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        first = cell_params(app)
        assert isinstance(first, CellParamSnapshot)
        assert first.owner == ALICE
        assert resolve_param(first, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == DEFAULT
        cells = cell_runtime(app)
        _retention(cells, 2 * HOUR_MS)
        assert (
            resolve_param(cell_params(app), DELIVERY_BODY_RETENTION_MS, AGENT_URI)
            == 2 * HOUR_MS
        )
        view = app._cell_params
        assert view.runtime is cells
        held = cells.index().of_kind("agent")[0]
        cells.update(held.cell_id, params={KEY: 3 * HOUR_MS})
        cells.run_once()
        assert (
            resolve_param(view.current(), DELIVERY_BODY_RETENTION_MS, AGENT_URI)
            == 3 * HOUR_MS
        )
        assert cell_params(app) is view.current()
        assert app._cell_params is view
    finally:
        close_cell_runtime(app)


def test_cell_params_is_none_without_a_runtime_and_callers_get_defaults(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    close_cell_runtime(app)
    assert cell_params(app) is None
    assert (
        resolve_param(cell_params(app), DELIVERY_BODY_RETENTION_MS, AGENT_URI)
        == DEFAULT
    )


def test_cell_params_is_none_when_composition_fails(tmp_path: Path) -> None:
    app = _app(tmp_path)

    def unreachable() -> Any:
        raise RuntimeError("orgfs unreachable")

    app._require_orgfs_runtime = unreachable
    assert cell_params(app) is None
    assert "cell.runtime_unavailable" in app.logged
    close_cell_runtime(app)


def test_established_params_survive_an_unreachable_orgfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    try:
        cells = cell_runtime(app)
        _retention(cells, 2 * HOUR_MS)
        cells.run_once()
        assert (
            resolve_param(cell_params(app), DELIVERY_BODY_RETENTION_MS, AGENT_URI)
            == 2 * HOUR_MS
        )

        def offline(*_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionError("orgfs unreachable")

        for name in ("spaces", "listdir", "read_text_snapshot", "write_text"):
            monkeypatch.setattr(app.fs, name, offline)
        cells.run_once()
        assert (
            resolve_param(cell_params(app), DELIVERY_BODY_RETENTION_MS, AGENT_URI)
            == 2 * HOUR_MS
        )
    finally:
        monkeypatch.undo()
        close_cell_runtime(app)


def test_a_recomposed_runtime_gets_a_fresh_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    try:
        cells = cell_runtime(app)
        cell_params(app)
        old_view = app._cell_params
        replacement = SimpleNamespace(
            identity=cells.identity,
            index=cells.index,
            hooks=[],
        )
        replacement.on_index = lambda hook: (
            replacement.hooks.append(hook),
            lambda: None,
        )[1]
        monkeypatch.setattr(params_wiring, "cell_runtime", lambda _app: replacement)
        assert cell_params(app) is not None
        assert app._cell_params is not old_view
        assert app._cell_params.runtime is replacement
        assert replacement.hooks == [app._cell_params.refresh]
    finally:
        monkeypatch.undo()
        close_cell_runtime(app)
