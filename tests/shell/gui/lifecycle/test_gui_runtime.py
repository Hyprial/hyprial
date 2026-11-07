"""Retired GUI lifecycle entry points fail closed and never mutate the home."""

from __future__ import annotations

import pytest

from hyprial.shell.impl.gui import runtime as gui_runtime
from hyprial.shell.impl.gui.errors import GuiError


@pytest.mark.parametrize(
    "operation",
    [
        lambda home: gui_runtime.gui_status(home),
        lambda home: gui_runtime.start_gui_background(home),
        lambda home: gui_runtime.stop_gui(home),
    ],
)
def test_lifecycle_entry_points_report_gui_unavailable(tmp_path, operation) -> None:
    with pytest.raises(GuiError) as caught:
        operation(tmp_path)
    assert caught.value.code == "GUI_UNAVAILABLE"
    assert "temporarily unavailable" in str(caught.value)
    assert not (tmp_path / "apps").exists()
