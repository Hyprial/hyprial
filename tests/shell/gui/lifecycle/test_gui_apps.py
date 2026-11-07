"""The retired bundled-GUI lifecycle keeps failing closed with stable codes."""

from __future__ import annotations

import pytest

from hyprial.shell.impl.gui import apps as gui_apps
from hyprial.shell.impl.gui.errors import GuiError


@pytest.mark.parametrize("action", ["start", "status", "stop", "upgrade"])
def test_resolve_invocation_keeps_the_cli_grammar(action: str) -> None:
    assert gui_apps.resolve_invocation(action) == (action, None)


@pytest.mark.parametrize("selector", ["legacy", "dashboard", "all", "../gui"])
def test_retired_selectors_are_still_invalid(selector: str) -> None:
    with pytest.raises(GuiError) as caught:
        gui_apps.resolve_invocation(selector)
    assert caught.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("action", ["start", "status", "stop"])
def test_perform_reports_gui_unavailable_without_touching_home(tmp_path, action: str) -> None:
    with pytest.raises(GuiError) as caught:
        gui_apps.perform(tmp_path, action)
    assert caught.value.code == "GUI_UNAVAILABLE"
    assert "temporarily unavailable" in str(caught.value)
    assert not (tmp_path / "apps").exists()


def test_upgrade_reports_gui_unavailable_without_touching_home(tmp_path) -> None:
    with pytest.raises(GuiError) as caught:
        gui_apps.upgrade(tmp_path, check_only=True)
    assert caught.value.code == "GUI_UNAVAILABLE"
    assert not (tmp_path / "apps").exists()
