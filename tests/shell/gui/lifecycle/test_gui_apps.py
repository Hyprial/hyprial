"""The retired paired-GUI upgrade keeps failing closed with a stable code."""

from __future__ import annotations

import pytest

from hyprial.shell.impl.gui import apps as gui_apps
from hyprial.shell.impl.gui.errors import GuiError


def test_upgrade_reports_gui_unavailable_without_touching_home(tmp_path) -> None:
    with pytest.raises(GuiError) as caught:
        gui_apps.upgrade(tmp_path, check_only=True)
    assert caught.value.code == "GUI_UNAVAILABLE"
    assert not (tmp_path / "apps").exists()
