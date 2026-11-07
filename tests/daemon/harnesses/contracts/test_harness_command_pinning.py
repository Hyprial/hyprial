"""Registration-time absolute command pinning (task D) + service PATH (E).

Postmortem 2026-08-23: the launchd-spawned daemon had a PATH without
~/.local/bin, so the bare command name ``codex`` was unresolvable exactly at
autoupdate-restart time.  Task D pins the absolute binary path into desired
state at registration; task E adds ~/.local/bin to the rendered unit PATH as
the band-aid layer (legacy entries registered before D exist).
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel.impl.contracts.daemon.lifecycle_budgets import (
    LIFECYCLE_IPC_MARGIN_SECONDS,
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.kernel import DesiredStateError
from hyprial.kernel import HarnessLaunchSpec
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _spec_json(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "provider": "codex",
        "name": "worker",
        "headless": True,
    }
    record.update(overrides)
    return record


def test_command_round_trips_through_desired_state_json() -> None:
    spec = HarnessLaunchSpec.from_json(
        _spec_json(command=["/Users/x/.local/bin/codex"]), "providers[0]"
    )
    assert spec.command == ("/Users/x/.local/bin/codex",)
    encoded = spec.to_json()
    assert encoded["command"] == ["/Users/x/.local/bin/codex"]
    reread = HarnessLaunchSpec.from_json(encoded, "providers[0]")
    assert reread.command == spec.command


def test_command_is_omitted_when_absent_and_unknown_keys_are_tolerated() -> None:
    # Skew contract: a file written by a newer build must still load on an
    # older one (tolerant reader), and an old file has no command key.
    spec = HarnessLaunchSpec.from_json(
        _spec_json(command=["/abs/codex"], futureField={"anything": 1}),
        "providers[0]",
    )
    assert "futureField" not in spec.to_json()
    legacy = HarnessLaunchSpec.from_json(_spec_json(), "providers[0]")
    assert legacy.command == ()
    assert "command" not in legacy.to_json()


def test_relative_command_is_rejected() -> None:
    with pytest.raises(DesiredStateError, match="absolute"):
        HarnessLaunchSpec.from_json(_spec_json(command=["codex"]), "providers[0]")


def test_non_string_command_entries_are_rejected() -> None:
    with pytest.raises(DesiredStateError, match="command"):
        HarnessLaunchSpec.from_json(
            _spec_json(command=["/abs/codex", 3]), "providers[0]"
        )


def test_resolved_command_prefers_the_pinned_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = HarnessLaunchSpec("codex", "w", True, command=("/pinned/codex",))
    monkeypatch.setattr(shutil, "which", lambda name: "/other/codex")
    assert spec.resolved_command(("codex",)) == ("/pinned/codex",)


def test_resolved_command_legacy_entry_uses_live_path_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = HarnessLaunchSpec("codex", "w", True)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/live/path/codex" if name == "codex" else None
    )
    assert legacy.resolved_command(("codex",)) == ("/live/path/codex",)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert legacy.resolved_command(("codex",)) == ("codex",)


def test_resolved_command_preserves_default_tail_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = HarnessLaunchSpec("pi", "w", True)
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/homebrew/bin/pi")
    assert legacy.resolved_command(("pi", "--rpc")) == (
        "/opt/homebrew/bin/pi",
        "--rpc",
    )


def test_launcher_default_factory_resolves_the_pinned_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default codex app-server factory must hand the pinned absolute
    command to the process constructor.  Injecting a fake factory would
    bypass the very wiring under test, so the fake sits at the
    CodexAppServerProcess symbol the default lambda constructs."""

    import hyprial.daemon.impl.harnesses.runtime.launcher as launcher_mod

    seen: list[object] = []

    class _CapturingProcess:
        running = True
        pid = None
        last_error = None

        def __init__(self, spec: HarnessLaunchSpec, **kwargs: object) -> None:
            seen.append(kwargs.get("command"))

        def wait_ready(self, *, timeout: float | None = None) -> bool:
            return True

        def stop(self) -> None:
            pass

    monkeypatch.setattr(launcher_mod, "CodexAppServerProcess", _CapturingProcess)
    launcher = launcher_mod.HarnessLauncher()
    pinned = HarnessLaunchSpec(
        "codex", "w", True, command=("/Users/x/.local/bin/codex",)
    )
    launcher.start(pinned)
    assert seen == [("/Users/x/.local/bin/codex",)]


def test_connector_build_argv_prefers_spec_command() -> None:
    from hyprial.daemon.impl.harnesses.codex import CodexConnector

    connector = CodexConnector()
    spec = HarnessLaunchSpec(
        "codex", "w", True, args=("--flag",), command=("/pinned/codex",)
    )
    assert connector.build_argv(spec) == ("/pinned/codex", "--flag")


def test_cli_start_pins_the_absolute_binary_into_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "home").mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")
    calls: list[tuple[str, Any, dict[str, Any]]] = []

    def request(method: str, params: Any = None, **kwargs: Any) -> dict[str, Any]:
        calls.append((method, params, kwargs))
        if method == "lifecycle.start":
            return {"ok": True, "id": "codex:w", "changed": True, "actor": "a"}
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["agent", "start", "codex", "--name", "w", "--headless", "--json"]
    )

    assert result.exit_code == 0, result.output
    start_calls = [(p, kwargs) for m, p, kwargs in calls if m == "lifecycle.start"]
    assert len(start_calls) == 1
    assert start_calls[0][0]["command"] == ["/fake/bin/codex"]
    # The start wait is composed from the one lifecycle deadline (deadline +
    # daemon wait margin + IPC margin), exactly like `down` -- never a bare
    # literal that can drift shorter than the daemon's own budget.
    assert start_calls[0][1] == {
        "timeout": (
            LIFECYCLE_OPERATION_DEADLINE_SECONDS
            + LIFECYCLE_WAIT_MARGIN_SECONDS
            + LIFECYCLE_IPC_MARGIN_SECONDS
        )
    }


def test_cli_start_fails_loud_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Registration-time resolution doubles as the fail-loud gate: no binary
    on the operator's PATH is a clear error at `hyprial agent start`, not a daemon that
    wedges at the next autoupdate restart."""

    (tmp_path / "home").mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls: list[str] = []

    def request(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(method)
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["agent", "start", "codex", "--name", "w", "--headless", "--json"]
    )

    assert result.exit_code != 0
    assert "not found on PATH" in result.output
    assert calls == []  # refused before any daemon-side trace is written


def test_service_config_default_path_env_includes_user_local_bin() -> None:
    from hyprial.daemon.impl.operations.service import ServiceConfig

    config = ServiceConfig(
        executable=Path("/x/hyprial"),
        home=Path("/x"),
        hyprial_home=Path("/x/.hyprial"),
        state_dir=Path("/x/.hyprial/state"),
    )
    assert str(Path.home() / ".local" / "bin") in config.path_env.split(":")


def test_pinned_absolute_binary_spawns_without_path(tmp_path: Path) -> None:
    """End of the D chain: an executable pinned by absolute path runs even
    with an empty PATH (the launchd condition)."""

    binary = tmp_path / "fake-codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [str(binary)], env={"PATH": "/nonexistent"}, capture_output=True
    )
    assert result.returncode == 0


def test_bare_binary_does_not_spawn_without_path() -> None:
    """The control for the test above: the bare name really is unresolvable
    under the launchd PATH condition (keeps the positive test honest)."""

    with pytest.raises(FileNotFoundError):
        subprocess.run(
            ["definitely-not-a-real-binary-hyprial"],
            env={"PATH": "/nonexistent"},
            capture_output=True,
        )
