"""A release wheel refuses `gui dev` without an explicit source, before any git.

Decision (2026-11-16): a wheel whose manifest names the public remote carries
an internal ``sourceCommit`` the public mirror does not hold, so the exact-SHA
fetch could only fail.  The installed-wheel fallback refuses instead, with
``INVALID_ARGUMENT``; any other manifest remote keeps the managed fetch path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.gui.install import launch as target_launch
from hyprial.shell.impl.gui.install.resources import PUBLIC_SOURCE_REMOTE

from .test_installed_gui import _assets, _env, _fake_project, _no_source, _which


def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: str) -> list[str]:
    assets = _assets(tmp_path, remote=remote)
    monkeypatch.setattr(target_launch, "installed_asset_root", lambda: assets.resolve())
    monkeypatch.setattr(launcher, "discover_source_repo", _no_source)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    calls: list[str] = []

    def fetch(*args, **kwargs):
        calls.append("ensure_installed_dev_worktree")
        raise GuiError("GUI_DEV_SOURCE_MISSING", "fetch attempted")

    monkeypatch.setattr(target_launch, "ensure_installed_dev_worktree", fetch)
    return calls


def _no_run(*args, **kwargs):
    pytest.fail("a subprocess ran")


LIVE_FORMS = pytest.mark.parametrize(
    ("action", "switches"),
    [("dev", {}), ("", {"live_source": True}), ("", {"live_source": True, "with_dev_tools": True})],
    ids=["dev", "live-source", "both-flags"],
)


@LIVE_FORMS
def test_release_wheel_dev_without_source_is_refused_before_any_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, switches: dict
) -> None:
    calls = _wire(tmp_path, monkeypatch, PUBLIC_SOURCE_REMOTE)
    work = tmp_path / "empty-cwd"
    work.mkdir()

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            action,
            environ=_env(tmp_path),
            cwd=work,
            inspect=lambda *a, **k: pytest.fail("inspect reached"),
            which=_which,
            run=_no_run,
            **switches,
        )

    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    message = str(caught.value)
    assert "release build" in message
    assert "--live-source" in message
    assert "--gui-home" in message
    assert "hyprial gui" in message
    assert calls == []


@LIVE_FORMS
def test_release_wheel_dev_with_gui_home_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, switches: dict
) -> None:
    calls = _wire(tmp_path, monkeypatch, PUBLIC_SOURCE_REMOTE)
    home = tmp_path / "explicit" / "gui"
    home.mkdir(parents=True)
    (home / "package.json").write_text("{}", encoding="utf-8")

    plan = launcher.build_plan(
        action,
        gui_home=home,
        environ=_env(tmp_path),
        cwd=tmp_path,
        inspect=lambda h, **k: _fake_project(h),
        which=_which,
        **switches,
    )

    assert plan.project.home == home.resolve()
    assert calls == []


@LIVE_FORMS
def test_release_wheel_dev_with_dev_home_env_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, switches: dict
) -> None:
    calls = _wire(tmp_path, monkeypatch, PUBLIC_SOURCE_REMOTE)
    home = tmp_path / "env-home" / "gui"
    home.mkdir(parents=True)
    (home / "package.json").write_text("{}", encoding="utf-8")

    plan = launcher.build_plan(
        action,
        environ={**_env(tmp_path), "HYPRIAL_DEV_GUI_HOME": str(home)},
        cwd=tmp_path,
        inspect=lambda h, **k: _fake_project(h),
        which=_which,
        **switches,
    )

    assert plan.project.home == home.resolve()
    assert calls == []


@LIVE_FORMS
def test_non_public_remote_wheel_keeps_the_exact_sha_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, switches: dict
) -> None:
    calls = _wire(tmp_path, monkeypatch, "https://example.invalid/hyprial")
    work = tmp_path / "empty-cwd"
    work.mkdir()

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            action,
            environ=_env(tmp_path),
            cwd=work,
            inspect=lambda home, **k: _fake_project(home),
            which=_which,
            **switches,
        )

    assert caught.value.code == "GUI_DEV_SOURCE_MISSING"
    assert calls == ["ensure_installed_dev_worktree"]
