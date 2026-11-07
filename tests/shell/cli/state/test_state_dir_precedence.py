"""Card 85dd41e2: a managed worker's inherited HARNESS_STATE_DIR must not
outrank a child's explicit HYPRIAL_HOME.

2026-09-04 22:51 CST a card-259 worker's rig set HYPRIAL_HOME to a temp root and
still wrote 77 fixture actors into the production registry: the worker's
launcher had exported the production HARNESS_STATE_DIR into its environment,
and the CLI reads that variable first.  The two values alone cannot tell
that shape from the repo's own isolated layout (``root/home`` beside
``root/state`` -- the unit conftest and the E2E IsolatedDaemon), so the leak
is closed on the exporting side: launchers hand a child HARNESS_STATE_DIR
only when the state really lives outside <home>/state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.kernel import child_state_environment
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_sibling_home_and_state_layout_is_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The conftest / IsolatedDaemon shape: state beside the home, both set."""
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    assert _cli_get_services()._state_dir() == (tmp_path / "state").resolve()
    assert _cli_get_services()._socket_path() == (tmp_path / "state").resolve() / "daemon.sock"


def test_a_child_with_only_an_inherited_home_is_re_pointed_by_its_own_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What a rig inside a managed worker sees once the launcher stops
    exporting the state dir: one explicit HYPRIAL_HOME moves everything."""
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "rig"))
    assert _cli_get_services()._state_dir() == (tmp_path / "rig").resolve() / "state"
    assert _cli_get_services()._socket_path() == (tmp_path / "rig").resolve() / "state" / "daemon.sock"


def test_launcher_exports_the_state_dir_only_when_it_lives_outside_the_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert child_state_environment(home, home / "state") == {
        "HYPRIAL_HOME": str(home.resolve())
    }
    elsewhere = child_state_environment(home, tmp_path / "state")
    assert elsewhere == {
        "HYPRIAL_HOME": str(home.resolve()),
        "HARNESS_STATE_DIR": str((tmp_path / "state").resolve()),
    }


def test_worker_channel_env_does_not_carry_the_production_state_dir(
    tmp_path: Path,
) -> None:
    """A production daemon keeps its state at <home>/state, so its managed
    workers must inherit HYPRIAL_HOME alone (red on base: both were exported)."""
    home = tmp_path / "home"
    channel = build_worker_channel(
        actor="agent:op:node-1:worker",
        session_ref="ref-1",
        hyprial_home=home,
        state_dir=home / "state",
    )
    assert "HARNESS_STATE_DIR" not in channel.pi_environment()
    assert channel.pi_environment()["HYPRIAL_HOME"] == str(home.resolve())
    server_env = channel.mcp_server["env"]
    assert isinstance(server_env, dict) and "HARNESS_STATE_DIR" not in server_env
