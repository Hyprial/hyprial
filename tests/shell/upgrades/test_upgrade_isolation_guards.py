"""Acceptance tests for the three upgrade isolation guards.

Spec autoupdate-isolated-home-no-global-install-2026-09-15, ⭐ revision r1
(Allen 2026-09-15: autoupdate defaults OFF in code; an operator opts in with
``hyprial config set autoUpgrade true``).  The three guards, in firing order:

1. autoUpgrade switch OFF (the default) -> the daemon scheduler records
   ``autoupdate.run.skipped`` (reason ``disabled``) and never spawns the
   upgrade child, AND the hidden ``autoupdate run`` timer entry point
   (which the legacy launchd/systemd timer executes directly, falling
   through to a local upgrade when the daemon is unreachable) skips the
   same way.  Isolated homes are off too, with no special-casing: the
   default covers them.
2. local install source   -> ``_perform_upgrade`` refuses before any remote
   probe (``UPGRADE_LOCAL_SOURCE``);
3. tool-dir backstop      -> the single ``uv tool install`` call site refuses
   unless THIS process's ``sys.prefix`` lives inside the directory uv is
   about to write (``UPGRADE_TOOL_DIR_MISMATCH``).

⛔ None of these tests may invoke a real ``uv`` (let alone ``uv tool
install``/``uv tool upgrade``) or a real ``hyprial daemon upgrade``: every ``uv``
binary on PATH is a shim confined to the fake HOME, and the install path is
exercised in-process via ``cli._perform_upgrade`` with faked resolution.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest


import hyprial.daemon.impl.updates as updates
from hyprial.daemon.impl.autoupdate import LOG_FILENAME, InProcessAutoUpdateScheduler
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.upgrade.autoupdate as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

GIT_SOURCE = "ssh://git@example.test/hyprial.git"


@pytest.fixture(autouse=True)
def _locked_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    def export_constraints(
        url: str, commit: str, destination: Path, *, runner: object = None
    ) -> Path:
        destination.write_text("anyio==4.15.1\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(updates, "export_locked_constraints", export_constraints)


def _install_fake_uv(shim_dir: Path, fake_home: Path) -> None:
    """Put a PATH-fronted fake ``uv`` shim into ``shim_dir``.

    The shim mirrors the real uv semantics this guard relies on (measured
    read-only on uv 0.11.7): ``uv tool dir`` is a pure read that prints the
    tool directory and touches nothing; every other invocation simulates a
    tool install by writing ``INSTALLED`` under the FAKE home's global tool
    directory.  All argv is appended to a log next to the fake home, so the
    tests can assert "shim was (not) called" with the exact argv shape.
    """

    tools_dir = fake_home / ".local" / "share" / "uv" / "tools"
    shim = shim_dir / "uv"
    shim.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "# fake uv for guard acceptance tests (au-iso); never",
                "# touches anything outside the fake HOME.",
                f'TOOLS_DIR="{tools_dir}"',
                f'ARGV_LOG="{fake_home}/uv-shim-argv.log"',
                'printf "%s\\n" "$*" >> "$ARGV_LOG"',
                'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then',
                "    # Real uv tool dir is a pure read (verified: no mkdir).",
                '    printf "%s\\n" "$TOOLS_DIR"',
                "    exit 0",
                "fi",
                'mkdir -p "$TOOLS_DIR/hyprial"',
                'echo fake-uv > "$TOOLS_DIR/hyprial/INSTALLED"',
                'printf "installed by fake uv\\n"',
                "exit 0",
            ]
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _fake_tool_install_dir(fake_home: Path) -> Path:
    return fake_home / ".local" / "share" / "uv" / "tools" / "hyprial"


def _shim_argv(fake_home: Path) -> list[str]:
    log = fake_home / "uv-shim-argv.log"
    if not log.exists():
        return []
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _front_fake_uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fake HOME + PATH-fronted fake uv; returns the fake HOME."""

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir()
    _install_fake_uv(shim_dir, fake_home)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
    return fake_home


def _no_daemon(*_args: Any, **_kwargs: Any) -> Any:
    raise ipc_errors.DaemonUnavailableError("no daemon in guard tests")


# --------------------------------------------------------------------------
# Guard 1: autoUpgrade defaults OFF -> the scheduler never spawns the child.
# --------------------------------------------------------------------------


def _write_settings(home: Path, record: dict[str, Any]) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


@pytest.mark.parametrize("home_kind", ["isolated", "default"])
def test_disabled_scheduler_skips_without_touching_global_tool_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home_kind: str
) -> None:
    """No explicit opt-in (no settings.json at all) = OFF, in an isolated
    home AND in the default home alike (r1: no isolated-home special case)."""

    fake_home = _front_fake_uv(monkeypatch, tmp_path)
    if home_kind == "default":
        home = fake_home / ".hyprial"
        monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    else:
        home = tmp_path / "isolated-home"
        monkeypatch.setenv("HYPRIAL_HOME", str(home))
    home.mkdir(mode=0o700)
    state = tmp_path / "state"
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)

    events: list[tuple[str, str, dict[str, Any]]] = []
    # Deliberately the REAL runner (subprocess.run): if the guard is removed
    # the scheduler must actually spawn the child, and the assertions below
    # (autoupdate.log, shim argv, tool dir) must catch that spawn.
    scheduler = InProcessAutoUpdateScheduler(
        state_dir=state,
        hyprial_home=home,
        logger=lambda level, event, **fields: events.append(
            (level, event, dict(fields))
        ),
    )
    scheduler.start()
    try:
        assert scheduler.trigger("manual-test") is True
        deadline = time.monotonic() + 10.0
        while scheduler.status()["lastSkipReason"] is None:
            assert time.monotonic() < deadline, "skip was never recorded"
            time.sleep(0.01)
        while scheduler.status()["active"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        scheduler.stop()

    status = scheduler.status()
    assert status["autoUpgradeEnabled"] is False
    assert status["lastSkipReason"] == "disabled"
    assert status["lastExitCode"] is None
    assert ("warning", "autoupdate.run.skipped", {"trigger": "manual-test", "reason": "disabled"}) in events
    assert all(event != "autoupdate.run.started" for _level, event, _f in events)
    # No child: no scheduler-side log file was opened for a subprocess.
    assert not (state / LOG_FILENAME).exists()
    # No uv at all: the shim was never invoked, nothing written under the
    # fake global tool directory.
    assert _shim_argv(fake_home) == []
    assert not _fake_tool_install_dir(fake_home).exists()


def test_enabled_scheduler_runs_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control for guard 1: explicit ``autoUpgrade: true`` in the
    home's settings.json lets the scheduler spawn its child."""

    fake_home = _front_fake_uv(monkeypatch, tmp_path)
    home = tmp_path / "home"
    _write_settings(home, {"autoUpgrade": True})
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)

    called = threading.Event()

    def runner(
        command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        called.set()
        return subprocess.CompletedProcess(command, 0, "{}\n", "")

    scheduler = InProcessAutoUpdateScheduler(
        state_dir=tmp_path / "state",
        hyprial_home=home,
        runner=runner,
    )
    scheduler.start()
    try:
        assert scheduler.trigger("manual-test") is True
        assert called.wait(5.0), "enabled scheduler skipped its run"
        deadline = time.monotonic() + 5.0
        while scheduler.status()["active"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        scheduler.stop()

    status = scheduler.status()
    assert status["autoUpgradeEnabled"] is True
    assert status["lastSkipReason"] is None
    assert status["lastExitCode"] == 0
    assert _shim_argv(fake_home) == []  # the runner was faked; no real child


def test_daemon_application_wires_home_into_scheduler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The daemon composition root is the one constructor that must not
    forget the home: the scheduler reads the autoUpgrade switch from THAT
    home's settings.json."""

    del monkeypatch
    state = tmp_path / "state"
    state.mkdir()
    home = tmp_path / "home"
    _write_settings(home, {"autoUpgrade": True})
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="au-iso-wiring",
        hyprial_home=home,
    **_deep_module_daemon_dependencies())
    try:
        assert app._autoupdate._hyprial_home == home
        assert app._autoupdate._auto_upgrade_enabled() is True
        updates.write_auto_upgrade(False, home)
        assert app._autoupdate._auto_upgrade_enabled() is False
    finally:
        app._close()


# --------------------------------------------------------------------------
# Guard 1b: the ``autoupdate run`` timer entry point skips too.
# --------------------------------------------------------------------------


def test_autoupdate_run_entry_skips_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The daemon-owned child still enforces the default-off policy itself.

    Everything below the guard is shaped POSITIVE (remote git source, prefix
    inside the tool dir uv would write): with the guard removed this run would
    reach the fake uv's ``tool install``.
    """

    fake_home = _front_fake_uv(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)  # ⭐ no settings.json: the default must be OFF
    state = tmp_path / "state"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD", "1")
    # Guard 3's condition deliberately PASSES (this test is about guard 1).
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit="a" * 40,
            requested_revision="dev",
            url=GIT_SOURCE,
        ),
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *_a, **_k: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="v0.4.0.dev2"
        ),
    )
    monkeypatch.setattr(_cli_get_services(), "_lark_auth_timer_check", lambda: None)

    _cli_owner_1.autoupdate_run(json_output=True)

    record = json.loads(
        (state / "autoupdate-last.json").read_text(encoding="utf-8")
    )
    assert record["ok"] is True
    assert record["skipped"] is True
    assert record["skipReason"] == "disabled"
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == record
    # No upgrade was attempted: uv was never even consulted.
    assert _shim_argv(fake_home) == []
    assert not _fake_tool_install_dir(fake_home).exists()


# --------------------------------------------------------------------------
# Guard 2: local (file:// / bare path / git+file) install source refuses.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "local_url",
    [
        "file:///tmp/au-iso-worktree",
        "git+file:///tmp/au-iso-worktree",
        "/tmp/au-iso-worktree",
    ],
)
def test_upgrade_refuses_local_install_source_before_any_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_url: str
) -> None:
    fake_home = _front_fake_uv(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    # Guard 3's condition deliberately PASSES here (this test is about
    # guard 2 alone): this process sits inside the tool dir uv would write.
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit="a" * 40,
            requested_revision="dev",
            url=local_url,
        ),
    )

    probe_calls: list[str] = []

    def record_probe(url: str, tag: str | None = None) -> updates.RemoteResolution:
        probe_calls.append(url)
        return updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="v0.4.0.dev2"
        )

    monkeypatch.setattr(updates, "resolve_remote", record_probe)

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._perform_upgrade(force=True)

    assert caught.value.code == "UPGRADE_LOCAL_SOURCE"
    assert caught.value.data == {
        "guard": "local-install-source",
        "installationUrl": local_url,
    }
    assert "local source" in str(caught.value)
    # Refused before any remote probe and before uv was ever consulted.
    assert probe_calls == []
    assert _shim_argv(fake_home) == []
    assert not _fake_tool_install_dir(fake_home).exists()


def test_upgrade_from_remote_git_source_passes_the_local_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Negative control inside guard 2's own test: a remote git source must
    not trip the local-source refusal (it proceeds to guard 3)."""

    fake_home = _front_fake_uv(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit="a" * 40,
            requested_revision="dev",
            url=GIT_SOURCE,
        ),
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *_a, **_k: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="v0.4.0.dev2"
        ),
    )
    monkeypatch.setattr(_cli_support, "_daemon_request", _no_daemon)

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._perform_upgrade(force=True)
    # Guard 2 passed (remote source); guard 3 refused (probe-path branch:
    # the fake uv answers `tool dir` with the FAKE global dir).
    assert caught.value.code == "UPGRADE_TOOL_DIR_MISMATCH"
    assert caught.value.data["toolDirSource"] == "uv-tool-dir"
    assert _shim_argv(fake_home) == ["tool dir"]


# --------------------------------------------------------------------------
# Guard 3: prefix outside the uv tool directory refuses (backstop).
# --------------------------------------------------------------------------


def test_upgrade_refuses_when_prefix_outside_uv_tool_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guards 1/2 conditions deliberately NOT hit (default home, git
    source): the backstop must still refuse."""

    fake_home = _front_fake_uv(monkeypatch, tmp_path)
    default_home = fake_home / ".hyprial"
    default_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit="a" * 40,
            requested_revision="dev",
            url=GIT_SOURCE,
        ),
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *_a, **_k: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="v0.4.0.dev2"
        ),
    )
    monkeypatch.setattr(_cli_support, "_daemon_request", _no_daemon)

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._perform_upgrade(force=True)

    assert caught.value.code == "UPGRADE_TOOL_DIR_MISMATCH"
    data = caught.value.data
    assert data["toolDir"] == str(fake_home / ".local" / "share" / "uv" / "tools")
    assert data["toolDirSource"] == "uv-tool-dir"
    assert data["sysPrefix"] == str(Path(sys.prefix).resolve())
    assert data["resolvedTag"] == "v0.4.0.dev2"
    assert data["resolvedCommit"] == "d" * 40
    assert "refusing to run uv tool install" in str(caught.value)
    # The probe happened (realistic uv consultation) but no install ran:
    # the fake global tool directory was never written.
    assert _shim_argv(fake_home) == ["tool dir"]
    assert not _fake_tool_install_dir(fake_home).exists()


def test_upgrade_proceeds_from_default_home_git_source_and_tool_dir_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control for the whole chain: default home + git source +
    prefix inside the tool directory uv would write => uv IS invoked."""

    fake_home = _front_fake_uv(monkeypatch, tmp_path)
    default_home = fake_home / ".hyprial"
    default_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    monkeypatch.setattr(
        updates,
        "read_installation",
        # Like real uv, the shim's install rewrites the receipt's commit.
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit="d" * 40 if _shim_argv(fake_home) else "a" * 40,
            requested_revision="dev",
            url=GIT_SOURCE,
        ),
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *_a, **_k: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="v0.4.0.dev2"
        ),
    )
    monkeypatch.setattr(_cli_support, "_daemon_request", _no_daemon)

    result = _cli_get_services()._perform_upgrade(force=False)

    assert result["upgraded"] is True
    argv = _shim_argv(fake_home)
    assert len(argv) == 1
    assert argv[0].startswith(
        "tool install --force --compile-bytecode --python "
    )
    assert argv[0].endswith(
        " git+https://github.com/Hyprial/hyprial.git@v0.4.0.dev2"
    )
    install_marker = _fake_tool_install_dir(fake_home) / "INSTALLED"
    assert install_marker.exists(), "positive control never reached uv"
    assert not (fake_home / ".local" / "share" / "uv" / "tools" / "unrelated").exists()


# --------------------------------------------------------------------------
# Resolution unit tests (pure; no subprocess unless injected).
# --------------------------------------------------------------------------


def _dead_runner(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("no uv on PATH")


def _answered(stdout: str) -> Any:
    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout, "")

    return runner


def test_resolve_uv_tool_dir_env_precedence() -> None:
    explicit = updates.resolve_uv_tool_dir(
        {"UV_TOOL_DIR": "/tmp/au-iso-explicit"}, runner=_dead_runner
    )
    assert explicit == {"toolDir": "/tmp/au-iso-explicit", "toolDirSource": "UV_TOOL_DIR"}

    probed = updates.resolve_uv_tool_dir({}, runner=_answered("/tmp/au-iso-probed\n"))
    assert probed == {"toolDir": "/tmp/au-iso-probed", "toolDirSource": "uv-tool-dir"}

    garbage = updates.resolve_uv_tool_dir(
        {"HOME": "/tmp/au-iso-fh"}, runner=_answered("installed\n")
    )
    assert garbage == {
        "toolDir": "/tmp/au-iso-fh/.local/share/uv/tools",
        "toolDirSource": "convention",
    }

    relative = updates.resolve_uv_tool_dir(
        {"HOME": "/tmp/au-iso-fh"}, runner=_answered("rel/probe\n")
    )
    assert relative["toolDirSource"] == "convention"

    xdg = updates.resolve_uv_tool_dir(
        {"XDG_DATA_HOME": "/tmp/au-iso-xdg"}, runner=_dead_runner
    )
    assert xdg == {"toolDir": "/tmp/au-iso-xdg/uv/tools", "toolDirSource": "convention"}

    failed = updates.resolve_uv_tool_dir(
        {}, runner=_answered("")
    )
    assert failed == {"toolDir": None, "toolDirSource": "unresolved"}

    unavailable = updates.resolve_uv_tool_dir({}, runner=_dead_runner)
    assert unavailable == {"toolDir": None, "toolDirSource": "unresolved"}


def test_uv_tool_dir_guard_prefix_semantics() -> None:
    prefix_parent = str(Path(sys.prefix).resolve().parent)
    inside = updates.uv_tool_dir_guard(
        {"UV_TOOL_DIR": prefix_parent}, runner=_dead_runner
    )
    assert inside["allowed"] is True
    assert inside["sysPrefix"] == str(Path(sys.prefix).resolve())

    outside = updates.uv_tool_dir_guard(
        {"UV_TOOL_DIR": "/tmp/au-iso-elsewhere"}, runner=_dead_runner
    )
    assert outside["allowed"] is False
    assert outside["toolDir"] == "/tmp/au-iso-elsewhere"

    unresolved = updates.uv_tool_dir_guard({}, runner=_dead_runner)
    assert unresolved["allowed"] is False
    assert unresolved["toolDirSource"] == "unresolved"


def test_prefix_within_normalises_symlinked_spellings(tmp_path: Path) -> None:
    # The mechanism under test: symlinked spellings of the same directory
    # must compare equal.  The production case is macOS /tmp -> /private/tmp,
    # but the test builds its OWN link so it holds on every platform (Linux
    # CI has no such alias, and hardcoding /tmp would silently stop testing
    # anything there).
    real_root = tmp_path / "real-root"
    (real_root / "env").mkdir(parents=True)
    link = tmp_path / "link-root"
    os.symlink(real_root, link)
    assert updates._prefix_within(str(link / "env"), real_root)
    assert updates._prefix_within(str(real_root / "env"), link)
    assert updates._prefix_within(str(real_root), real_root)
    assert not updates._prefix_within(str(tmp_path / "real-root-other"), real_root)


# --------------------------------------------------------------------------
# Guard 1 settings surface: default off, fail-closed, opt-in CLI.
# --------------------------------------------------------------------------


def test_auto_upgrade_enabled_defaults_off_and_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    # No file, missing key, explicit false, non-bool lookalikes: all OFF.
    assert updates.auto_upgrade_enabled(home) is False
    _write_settings(home, {"owner": "someone"})
    assert updates.auto_upgrade_enabled(home) is False
    _write_settings(home, {"autoUpgrade": False})
    assert updates.auto_upgrade_enabled(home) is False
    _write_settings(home, {"autoUpgrade": "true"})
    assert updates.auto_upgrade_enabled(home) is False
    _write_settings(home, {"autoUpgrade": 1})
    assert updates.auto_upgrade_enabled(home) is False

    # Only the literal boolean true opts in.
    _write_settings(home, {"autoUpgrade": True})
    assert updates.auto_upgrade_enabled(home) is True

    # Fail-closed: corrupt or wrongly-shaped settings.json means OFF, never
    # an exception out of the scheduler's per-fire question.
    (home / "settings.json").write_text("{not json", encoding="utf-8")
    assert updates.auto_upgrade_enabled(home) is False
    (home / "settings.json").write_text("[true]", encoding="utf-8")
    assert updates.auto_upgrade_enabled(home) is False


def test_write_auto_upgrade_roundtrip_preserves_unrelated_keys(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_settings(home, {"owner": "someone", "updateTrack": "dev"})

    path = updates.write_auto_upgrade(True, home)
    assert path == home / "settings.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record == {
        "owner": "someone",
        "updateTrack": "dev",
        "autoUpgrade": True,
    }
    assert updates.auto_upgrade_enabled(home) is True

    updates.write_auto_upgrade(False, home)
    assert updates.auto_upgrade_enabled(home) is False
    assert json.loads(path.read_text(encoding="utf-8"))["owner"] == "someone"


def test_write_auto_upgrade_refuses_malformed_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    settings = home / "settings.json"
    settings.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot parse"):
        updates.write_auto_upgrade(True, home)
    # The corrupt file is left untouched for the operator to repair.
    assert settings.read_text(encoding="utf-8") == "{not json"


def test_config_set_cli_roundtrip_and_status_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    home = tmp_path / "home"
    _write_settings(home, {"owner": "someone"})
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    runner = CliRunner()

    assert updates.auto_upgrade_enabled(home) is False
    result = runner.invoke(
        _cli_root_app, ["config", "set", "autoUpgrade", "true", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["key"] == "autoUpgrade"
    assert payload["value"] is True
    assert updates.auto_upgrade_enabled(home) is True

    # Allen's dashed spelling is accepted for the same canonical key.
    result = runner.invoke(
        _cli_root_app, ["config", "set", "auto-upgrade", "false", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["value"] is False
    assert updates.auto_upgrade_enabled(home) is False


def test_config_set_cli_rejects_unknown_key_and_bad_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    home = tmp_path / "home"
    _write_settings(home, {"owner": "someone"})
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    runner = CliRunner()

    result = runner.invoke(_cli_root_app, ["config", "set", "nope", "true", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == "INVALID_CONFIGURATION"

    result = runner.invoke(
        _cli_root_app, ["config", "set", "autoUpgrade", "maybe", "--json"]
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == "INVALID_CONFIGURATION"
    # Nothing was written by either refusal.
    assert updates.auto_upgrade_enabled(home) is False
