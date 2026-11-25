"""Daemon close always runs the ``cell-runtime`` step, composed or not."""

from __future__ import annotations

from pathlib import Path

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.shutdown import _CLOSE_UNBUDGETED_STEPS
from hyprial.daemon.impl.composition.cells import CELL_RUNTIME_CLOSE_TIMEOUT_S, cell_runtime
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.runtime.environment import UNIT_USER_ID


def _app(tmp_path: Path) -> DaemonApplication:
    return DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="cell-runtime-close-test",
        hyprial_home=tmp_path / "home",
        owner="owner", user_id=UNIT_USER_ID,
        **daemon_dependencies(),
    )


def test_daemon_close_stops_a_composed_cell_runtime(tmp_path: Path) -> None:
    calls: list[float] = []

    class Runtime:
        def close(self, timeout: float) -> bool:
            calls.append(timeout)
            return True

    app = _app(tmp_path)
    app._cell_runtime = Runtime()  # type: ignore[attr-defined]  # noqa: SLF001
    app._close()  # noqa: SLF001
    assert calls == [CELL_RUNTIME_CLOSE_TIMEOUT_S]
    assert app._cell_runtime is None  # type: ignore[attr-defined]  # noqa: SLF001


def test_daemon_close_without_a_cell_runtime_blocks_a_late_composition(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    app._close()  # noqa: SLF001
    assert getattr(app, "_cell_runtime", None) is None
    assert cell_runtime(app) is None
    assert getattr(app, "_orgfs_runtime", None) is None


def test_the_cell_runtime_step_is_classified() -> None:
    assert "cell-runtime" in dict(_CLOSE_UNBUDGETED_STEPS)
