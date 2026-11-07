"""Pure state-root selection retains the legacy CLI's lazy precedence."""

from pathlib import Path

import pytest


def test_explicit_state_precedes_a_sibling_or_missing_home(tmp_path):
    from hyprial.kernel import configured_harness_state_dir

    state = tmp_path / "sibling/state"
    selected = configured_harness_state_dir({"HARNESS_STATE_DIR": str(state), "HYPRIAL_HOME": str(tmp_path / "other-home")},
                                           home_selector=lambda: pytest.fail("home selected despite explicit state"))
    assert selected == state.resolve()
    assert not state.exists()


def test_explicit_state_expands_user_before_resolution(tmp_path, monkeypatch):
    from hyprial.kernel import configured_harness_state_dir

    monkeypatch.setenv("HOME", str(tmp_path))
    assert configured_harness_state_dir({"HARNESS_STATE_DIR": "~/isolated/state"}) == tmp_path / "isolated/state"


@pytest.mark.parametrize("state", [None, ""])
def test_empty_or_absent_state_selects_uninitialized_home_without_creation(tmp_path, state):
    from hyprial.kernel import configured_harness_state_dir

    home = tmp_path / "uninitialized"
    env = {"HYPRIAL_HOME": str(home)}
    if state is not None:
        env["HARNESS_STATE_DIR"] = state
    assert configured_harness_state_dir(env) == home / "state"
    assert not home.exists()


def test_default_home_port_is_invoked_only_for_fallback(tmp_path):
    from hyprial.kernel import configured_harness_state_dir

    calls = []

    def selected_home():
        calls.append("selected")
        return tmp_path / "selected"

    assert configured_harness_state_dir({}, home_selector=selected_home) == tmp_path / "selected/state"
    assert calls == ["selected"]


def test_public_state_selector_is_the_single_native_definition():
    from hyprial.kernel import configured_harness_state_dir
    from hyprial.kernel.impl.configuration.home import configured_harness_state_dir as native

    assert configured_harness_state_dir is native
    assert Path(native.__code__.co_filename).name == "home.py"
