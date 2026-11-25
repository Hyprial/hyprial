"""Installed wheel launch: daemon admission and the Python host plan, without source inspection."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend.admission import SourceAdmission
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.gui.install import launch as target_launch

from .test_installed_gui import _admission, _assets


def _installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _assets(tmp_path)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(target_launch, "installed_asset_root", lambda: root.resolve())
    return root


def test_installed_plan_goes_through_admission_and_the_python_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _installed(tmp_path, monkeypatch)
    admitted = _admission(tmp_path)
    seen: list[dict[str, str]] = []

    def admit(*, environ: dict[str, str]) -> SourceAdmission:
        seen.append(environ)
        return admitted

    plan = launcher.build_plan(
        "",
        port=4999,
        environ={"HYPRIAL_GUI_BACKEND_DRIVER": "daemon"},
        cwd=tmp_path,
        inspect=lambda *a, **k: pytest.fail("installed assets reached source inspection"),
        admit=admit,
    )

    assert len(seen) == 1
    assert plan.driver == "daemon"
    assert plan.admission is admitted
    assert plan.backend_owned is False
    assert plan.build_argv is None
    assert plan.installed_assets is not None
    assert plan.project.installed is True
    assert plan.runtime_dir == tmp_path / "state" / "gui" / "daemon"
    assert plan.frontend_argv == (
        sys.executable,
        "-P",
        "-m",
        "hyprial.shell.impl.gui.host",
        "serve",
        "--root",
        str(root.resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        "4999",
    )


def _must_not_admit(**kwargs: object) -> SourceAdmission:
    pytest.fail("a refused installed launch reached admission")


def test_installed_driver_defaults_to_the_daemon_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(tmp_path, monkeypatch)
    seen: list[dict[str, str]] = []

    def admit(*, environ: dict[str, str]) -> SourceAdmission:
        seen.append(dict(environ))
        return _admission(tmp_path)

    caller_environ: dict[str, str] = {}
    plan = launcher.build_plan(
        "", environ=caller_environ, cwd=tmp_path, admit=admit
    )

    assert seen[0]["HYPRIAL_GUI_BACKEND_DRIVER"] == "daemon"
    assert "HYPRIAL_GUI_BACKEND_DRIVER" not in caller_environ
    assert plan.driver == "daemon"


def test_installed_unset_driver_reaches_daemon_admission_for_a_missing_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(tmp_path, monkeypatch)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={"HYPRIAL_HOME": str(tmp_path / "absent-home")},
            cwd=tmp_path,
        )

    assert caught.value.code == "GUI_HOME_NOT_INITIALIZED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows access control")
@pytest.mark.parametrize("mode", [0o755, 0o700])
def test_installed_entry_checks_existing_home_before_runtime_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int,
) -> None:
    _installed(tmp_path, monkeypatch)
    home = tmp_path / "hyprial-home"
    home.mkdir(mode=mode)
    home.chmod(mode)
    environment = {"HYPRIAL_HOME": str(home), "HYPRIAL_NODE_ID": "private-home-test"}
    if mode == 0o755:
        with pytest.raises(GuiError) as caught:
            launcher.build_plan("", environ=environment, cwd=tmp_path)
        assert caught.value.code == "GUI_HOME_UNSAFE"
    else:
        with pytest.raises(GuiError) as caught:
            launcher.build_plan("", environ=environment, cwd=tmp_path)
        assert caught.value.code == "GUI_DAEMON_UNAVAILABLE"
    assert home.stat().st_mode & 0o777 == mode
    assert list(home.iterdir()) == []


def test_installed_blank_driver_is_still_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(tmp_path, monkeypatch)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={
                "HYPRIAL_GUI_BACKEND_DRIVER": " ",
                "HYPRIAL_HOME": str(tmp_path / "absent-home"),
            },
            cwd=tmp_path,
        )

    assert caught.value.code == "GUI_BACKEND_DRIVER"


def test_installed_contract_mock_is_refused_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(tmp_path, monkeypatch)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={"HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock"},
            cwd=tmp_path,
            admit=_must_not_admit,
        )

    assert caught.value.code == "GUI_MOCK_UNAVAILABLE"


def test_installed_launch_without_getuid_reaches_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A platform without ``os.getuid`` (Windows) is not refused before admission."""
    _installed(tmp_path, monkeypatch)
    monkeypatch.delattr(os, "getuid", raising=False)
    admitted = _admission(tmp_path)
    seen: list[dict[str, str]] = []

    def admit(*, environ: dict[str, str]) -> SourceAdmission:
        seen.append(environ)
        return admitted

    plan = launcher.build_plan("", port=4999, environ={}, cwd=tmp_path, admit=admit)

    assert len(seen) == 1
    assert plan.admission is admitted
