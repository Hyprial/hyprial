"""CLI-level autoupdate behavior: headless env, last-run recording, config."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli

import hyprial.daemon.impl.updates as updates
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.upgrade.autoupdate as _cli_owner_1
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from urllib.parse import urlsplit as _urlsplit
from hyprial.daemon.impl.updates import FORGEJO_GIT_URL as _FORGEJO_GIT_URL
_FORGEJO_HOST = _urlsplit(_FORGEJO_GIT_URL).hostname


@pytest.fixture(autouse=True)
def _initialized_command_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "home").mkdir(mode=0o700)

    def export_constraints(
        url: str, commit: str, destination: Path, *, runner: object = None
    ) -> Path:
        destination.write_text("anyio==4.15.1\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(updates, "export_locked_constraints", export_constraints)


def _enable_auto_upgrade(home: Path) -> None:
    """Guard-1 (spec r1) opt-in for timer-entry tests: without an explicit
    ``autoUpgrade: true`` in settings.json, ``upgrade run`` skips."""

    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"autoUpgrade": True}) + "\n", encoding="utf-8"
    )


def test_post_install_import_failure_reports_daemon_recovery(monkeypatch, tmp_path):
    """A restart import failure after install reports daemon recovery."""
    from hyprial.shell.impl.cli.commands.upgrade import commands

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    old = updates.Installation(version="0.5.2", commit="a" * 40, requested_revision="v0.5.2", url="https://github.com/Hyprial/hyprial.git")
    new = updates.Installation(version="0.5.4", commit="b" * 40, requested_revision="v0.5.4", url=old.url)
    monkeypatch.setattr(updates, "read_installation", lambda: old)
    monkeypatch.setattr(updates, "resolve_remote", lambda *a, **k: updates.RemoteResolution(tag="v0.5.4", commit="b" * 40, version="0.5.4"))
    monkeypatch.setattr(updates, "uv_tool_dir_guard", lambda: {"allowed": True})
    monkeypatch.setattr(_cli_get_services(), "_running_daemon_before_upgrade", lambda _: {"pid": 123, "epoch": "old", "version": "0.5.2"})
    monkeypatch.setattr(_cli_get_services(), "_prime_post_install_restart_imports", lambda: None)
    monkeypatch.setattr(commands, "_install_locked_requirement", lambda **kw: (subprocess.CompletedProcess([], 0, "installed", ""), new))

    def broken_restart(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'hyprial.identity.impl.identity_transaction'")

    monkeypatch.setattr(commands, "_restart_daemon_onto_install", broken_restart)
    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._perform_upgrade(force=False)
    assert caught.value.code == "UPGRADE_RESTART_FAILED"
    assert "hyprial daemon restart" in str(caught.value)
    assert caught.value.data["upgradeCompleted"] is True
    assert caught.value.data["restartRequired"] is True


def test_post_install_path_is_primed_before_install(monkeypatch, tmp_path):
    """No old/new module mixture is deferred to the recovery catch path."""
    from hyprial.shell.impl.cli.commands.upgrade import commands

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    old = updates.Installation(version="0.5.2", commit="a" * 40, requested_revision="v0.5.2", url="https://github.com/Hyprial/hyprial.git")
    new = updates.Installation(version="0.5.4", commit="b" * 40, requested_revision="v0.5.4", url=old.url)
    monkeypatch.setattr(updates, "read_installation", lambda: old)
    monkeypatch.setattr(updates, "resolve_remote", lambda *a, **k: updates.RemoteResolution(tag="v0.5.4", commit="b" * 40, version="0.5.4"))
    monkeypatch.setattr(updates, "uv_tool_dir_guard", lambda: {"allowed": True})
    monkeypatch.setattr(_cli_get_services(), "_running_daemon_before_upgrade", lambda _: {"pid": 123, "epoch": "old", "version": "0.5.2"})
    events: list[str] = []
    monkeypatch.setattr(_cli_get_services(), "_prime_post_install_restart_imports", lambda: events.append("prime"))

    def install(**_kwargs):
        assert events == ["prime"]
        events.append("install")
        return subprocess.CompletedProcess([], 0, "installed", ""), new

    monkeypatch.setattr(commands, "_install_locked_requirement", install)
    monkeypatch.setattr(
        commands,
        "_restart_daemon_onto_install",
        lambda result, **_kwargs: {**result, "restartRequired": False},
    )

    result = _cli_get_services()._perform_upgrade(force=False)

    assert result["upgraded"] is True
    assert events == ["prime", "install"]


def test_usage_advertises_upgrade_commands() -> None:
    # Pins the top-level help inventory that gates the blackbox upgrade
    # check: if this line ever disappears, the contract suite would silently
    # stop validating the upgrade surface.
    result = CliRunner().invoke(_cli_root_app, ["help", "--json"])
    assert result.exit_code == 0
    assert "  hyprial daemon" in json.loads(result.stdout)["usage"].splitlines()
    assert "  hyprial daemon upgrade" not in json.loads(result.stdout)["usage"]


def test_perform_upgrade_injects_headless_git_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    # uv's default may name another minor version. An in-process upgrade
    # must retain the interpreter whose sys.path the caller is using.
    monkeypatch.setenv("UV_PYTHON", "3.99")
    # Guard 3's positive shape: this process sits inside the tool directory
    # uv would write (the production install's own environment).
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    # Like real uv, the install rewrites the receipt to the commit it fetched.
    receipt = {"commit": "a" * 40}
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit=receipt["commit"],
            requested_revision="dev",
            url="ssh://git@example.test/hyprial.git",
        ),
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *a, **k: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="0.4.0.dev2"
        ),
    )
    calls: dict[str, object] = {}

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls["args"] = args
        calls["kwargs"] = kwargs
        receipt["commit"] = "d" * 40
        return subprocess.CompletedProcess(args, 0, "installed\n", "")

    monkeypatch.setattr(_cli_subprocess, "run", fake_run)

    result = _cli_get_services()._perform_upgrade(force=False)

    assert result["upgraded"] is True and result["restartRequired"] is False
    assert result["restart"]["reason"] == "daemon was not running; restart skipped"
    install_argv = calls["args"]
    assert install_argv[:5] == [
        "uv", "tool", "install", "--force", "--compile-bytecode"
    ]
    assert install_argv[5:7] == ["--python", sys.executable]
    assert install_argv[7] == "--constraints"
    assert Path(install_argv[8]).name == "constraints.txt"
    assert install_argv[9] == (
        "git+https://github.com/Hyprial/hyprial.git@v0.4.0.dev2"
    )
    env = calls["kwargs"]["env"]  # type: ignore[index]
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # type: ignore[index]
    assert "BatchMode=yes" in env.get("GIT_SSH_COMMAND", "")  # type: ignore[index]


def test_perform_upgrade_noop_skips_uv_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit="c" * 40,
            requested_revision="dev",
            url="ssh://git@example.test/hyprial.git",
        ),
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *a, **k: updates.RemoteResolution(
            tag="v0.4.0.dev1", commit="c" * 40, version="0.4.0.dev1"
        ),
    )
    invoked: list[tuple] = []
    monkeypatch.setattr(
        _cli_subprocess,
        "run",
        lambda *a, **k: invoked.append((a, k))
        or subprocess.CompletedProcess(a[0], 0, "", ""),
    )

    result = _cli_get_services()._perform_upgrade(force=False)

    assert result["upgraded"] is False
    assert result["reason"] == "already at the resolved tag's commit"
    assert result["resolvedTag"] == "v0.4.0.dev1"
    assert result["resolvedCommit"] == "c" * 40
    assert invoked == []


def test_autoupdate_run_records_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD", "1")
    _enable_auto_upgrade(tmp_path / "home")
    # The lark-auth watchdog rides this timer; keep the unit test hermetic.
    monkeypatch.setattr(_cli_get_services(), "_lark_auth_timer_check", lambda: None)
    upgrade_kwargs: dict[str, object] = {}

    def perform(force: bool, **kwargs: object) -> dict[str, object]:
        upgrade_kwargs.update(kwargs)
        return {
            "ok": True,
            "upgraded": False,
            "resolvedTag": "v0.4.0.dev1",
            "resolvedCommit": "a" * 40,
            "restartRequired": False,
        }

    monkeypatch.setattr(_cli_get_services(), "_perform_upgrade", perform)

    _cli_owner_1.autoupdate_run(json_output=True)

    record = json.loads(
        (tmp_path / "state" / "autoupdate-last.json").read_text(encoding="utf-8")
    )
    assert record["ok"] is True and record["upgraded"] is False
    assert "upgradeSteps" in record
    assert record["resolvedTag"] == "v0.4.0.dev1"
    assert record["resolvedCommit"] == "a" * 40
    assert record["restartRequired"] is False
    assert isinstance(record["at"], str) and record["at"]
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == record
    # Allen 2026-09-23: the timer installs but never restarts on its own; a
    # person confirms with `hyprial daemon upgrade apply`.  No pre-restart gate.
    assert upgrade_kwargs["restart"] is False
    assert upgrade_kwargs["awaiting_confirmation"] is True
    assert upgrade_kwargs["source"] == "official"
    assert "before_restart" not in upgrade_kwargs


def test_legacy_timer_run_delegates_to_live_daemon_scheduler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    _enable_auto_upgrade(tmp_path / "home")
    monkeypatch.delenv("HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD", raising=False)
    calls: list[tuple[str, float]] = []

    def daemon_request(
        method: str,
        _params: object = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, object]:
        del restore_wait
        calls.append((method, timeout))
        return {"ok": True, "triggered": True, "reason": "accepted"}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(
        _cli_get_services(),
        "_perform_upgrade",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy timer executed upgrade outside daemon scheduler"
        ),
    )

    _cli_owner_1.autoupdate_run(json_output=True)

    assert calls == [("autoupdate.trigger", 2.0)]
    assert json.loads(capsys.readouterr().out)["triggered"] is True
    assert not (tmp_path / "state" / "autoupdate-last.json").exists()


def test_autoupdate_run_without_daemon_does_not_upgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD", raising=False)
    _enable_auto_upgrade(tmp_path / "home")

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise cli.ipc_errors.DaemonUnavailableError("daemon is not running", {})

    monkeypatch.setattr(_cli_support, "_daemon_request", unavailable)
    monkeypatch.setattr(
        _cli_get_services(),
        "_perform_upgrade",
        lambda *_args, **_kwargs: pytest.fail(
            "autoupdate run upgraded locally without its daemon owner"
        ),
    )

    _cli_owner_1.autoupdate_run(json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["skipped"] is True
    assert payload["skipReason"] == "daemon unavailable"
    assert not (tmp_path / "state" / "autoupdate-last.json").exists()


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_legacy_timer_commands_are_absent(command: str) -> None:
    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", command])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_autoupdate_child_records_ahead_of_official_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD", "1")
    _enable_auto_upgrade(tmp_path / "home")
    monkeypatch.setattr(_cli_get_services(), "_lark_auth_timer_check", lambda: None)

    def perform(force: bool, **kwargs: object) -> dict[str, object]:
        assert force is False
        assert kwargs["source"] == "official"
        return {
            "ok": True,
            "upgraded": False,
            "resolvedTag": "v1.0.0",
            "resolvedCommit": "a" * 40,
            "declinedDowngrade": True,
            "reason": "installed version is ahead; refusing to move backwards",
            "restartRequired": False,
        }

    monkeypatch.setattr(_cli_get_services(), "_perform_upgrade", perform)

    _cli_owner_1.autoupdate_run(json_output=True)

    record = json.loads(capsys.readouterr().out)
    assert record["upgraded"] is False
    assert record["declinedDowngrade"] is True
    assert "ahead" in record["reason"]


def test_autoupdate_run_records_failure_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD", "1")
    _enable_auto_upgrade(tmp_path / "home")
    monkeypatch.setattr(_cli_get_services(), "_lark_auth_timer_check", lambda: None)

    def boom(force: bool, **_kwargs: object) -> dict:
        raise _cli_get_services().CliError("UPGRADE_CHECK_FAILED", "remote offline")

    monkeypatch.setattr(_cli_get_services(), "_perform_upgrade", boom)

    with pytest.raises(typer.Exit):
        _cli_owner_1.autoupdate_run(json_output=True)

    record = json.loads(
        (tmp_path / "state" / "autoupdate-last.json").read_text(encoding="utf-8")
    )
    assert record["ok"] is False
    assert record["code"] == "UPGRADE_CHECK_FAILED" and record["error"] == "remote offline"
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["ok"] is False and emitted["code"] == "UPGRADE_CHECK_FAILED"


def test_autoupdate_run_records_lark_auth_watchdog_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The watchdog hangs on the timer (#142): its outcome lands in the
    last-run record on success AND on upgrade failure, and never fails the
    run itself."""

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD", "1")
    _enable_auto_upgrade(tmp_path / "home")
    monkeypatch.setattr(
        _cli_get_services(),
        "_lark_auth_timer_check",
        lambda: {"status": "awaiting_user", "notified": True},
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_perform_upgrade",
        lambda force, **_kwargs: {"ok": True, "upgraded": False},
    )

    _cli_owner_1.autoupdate_run(json_output=True)

    record = json.loads(
        (tmp_path / "state" / "autoupdate-last.json").read_text(encoding="utf-8")
    )
    assert record["larkAuth"] == {"status": "awaiting_user", "notified": True}
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == record

    def boom(force: bool, **_kwargs: object) -> dict:
        raise _cli_get_services().CliError("UPGRADE_CHECK_FAILED", "remote offline")

    monkeypatch.setattr(_cli_get_services(), "_perform_upgrade", boom)
    with pytest.raises(typer.Exit):
        _cli_owner_1.autoupdate_run(json_output=True)
    record = json.loads(
        (tmp_path / "state" / "autoupdate-last.json").read_text(encoding="utf-8")
    )
    assert record["ok"] is False
    assert record["larkAuth"]["status"] == "awaiting_user"


def test_lark_auth_timer_check_degrades_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook must be invisible on machines without lark-cli and must
    collapse any failure into the record instead of failing the timer."""

    from hyprial.daemon.impl.adapters.lark.credentials import reauth

    monkeypatch.setattr(reauth, "find_lark_cli", lambda: None)
    assert _cli_get_services()._lark_auth_timer_check() is None

    monkeypatch.setattr(reauth, "find_lark_cli", lambda: "/nonexistent/lark-cli")

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("daemon down")

    monkeypatch.setattr(reauth, "check", boom)
    assert _cli_get_services()._lark_auth_timer_check() == {"status": "error", "error": "RuntimeError"}


def test_git_env_augments_inherited_ssh_command_with_batchmode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /home/x/deploy-key")
    env = updates.git_env()
    assert "ssh -i /home/x/deploy-key" in env["GIT_SSH_COMMAND"]
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"

    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -i /home/x/k")
    assert updates.git_env()["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes -i /home/x/k"


def test_git_env_drops_a_stale_global_extra_header_for_the_public_mirror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Member retest 2026-09-28: a global ``http.extraHeader`` left from the
    internal forge was sent to GitHub, which rejected it; upgrade failed with
    invalid credentials.  git_env resets the header for github.com only."""

    config = tmp_path / "gitconfig"
    config.write_text(
        "[http]\n\textraHeader = Authorization: token stale-forge-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in [k for k in os.environ if k.startswith("GIT_CONFIG_") and k not in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}]:
        monkeypatch.delenv(name)

    def header_for(url: str) -> str:
        completed = subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraHeader", url],
            env=updates.git_env(),
            text=True,
            capture_output=True,
            check=False,
        )
        return completed.stdout.strip()

    assert header_for("https://github.com/Hyprial/hyprial") == ""
    # Every other host keeps its headers.
    assert header_for("https://code.hyprial.com/HyprialOS/harness-bridge") == (
        "Authorization: token stale-forge-token"
    )


def test_git_env_appends_after_inherited_command_scope_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.ssh://git@{_FORGEJO_HOST}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://code.hyprial.com/")
    env = updates.git_env()
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == f"url.ssh://git@{_FORGEJO_HOST}/.insteadOf"
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_1"] == ""


def test_perform_upgrade_maps_uv_timeout_to_stable_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit="a" * 40,
            requested_revision="dev",
            url="ssh://git@example.test/hyprial.git",
        ),
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *a, **k: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="0.4.0.dev2"
        ),
    )

    def hang(args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(_cli_subprocess, "run", hang)

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._perform_upgrade(force=False)
    assert caught.value.code == "UPGRADE_FAILED"
    assert "timed out after 300s" in str(caught.value)
    assert caught.value.data == {
        "resolvedTag": "v0.4.0.dev2",
        "resolvedCommit": "d" * 40,
        "source": "official",
        "persistentUpdateSource": "https://github.com/Hyprial/hyprial.git",
    }
    assert "resolvedTag=v0.4.0.dev2" in str(caught.value)
    assert f"resolvedCommit={'d' * 40}" in str(caught.value)


def test_perform_upgrade_maps_missing_uv_to_stable_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version="0.4.0.dev1",
            commit="a" * 40,
            requested_revision="dev",
            url="ssh://git@example.test/hyprial.git",
        ),
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *a, **k: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="0.4.0.dev2"
        ),
    )

    def no_uv(args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("No such file or directory: uv")

    monkeypatch.setattr(_cli_subprocess, "run", no_uv)

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._perform_upgrade(force=False)
    assert caught.value.code == "UPGRADE_FAILED"
    assert "cannot run uv" in str(caught.value)
    assert caught.value.data == {
        "resolvedTag": "v0.4.0.dev2",
        "resolvedCommit": "d" * 40,
        "source": "official",
        "persistentUpdateSource": "https://github.com/Hyprial/hyprial.git",
    }
    assert "resolvedTag=v0.4.0.dev2" in str(caught.value)
    assert f"resolvedCommit={'d' * 40}" in str(caught.value)


def test_autoupdate_run_records_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_AUTOUPDATE_IN_PROCESS_CHILD", "1")
    _enable_auto_upgrade(tmp_path / "home")
    monkeypatch.setattr(_cli_get_services(), "_lark_auth_timer_check", lambda: None)

    def boom(force: bool, **_kwargs: object) -> dict:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(_cli_get_services(), "_perform_upgrade", boom)

    with pytest.raises(typer.Exit):
        _cli_owner_1.autoupdate_run(json_output=True)

    record = json.loads(
        (tmp_path / "state" / "autoupdate-last.json").read_text(encoding="utf-8")
    )
    assert record["ok"] is False
    assert record["code"] == "UNEXPECTED" and "disk on fire" in record["error"]
