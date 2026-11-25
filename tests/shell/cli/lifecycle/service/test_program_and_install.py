"""Launchd acceptance fixes D1/D3/F2 (2026-11-16): program choice, install revert, doctor.

A venv's ``bin/python`` is a symlink to a base CPython shared by every venv
built on it, so program attribution must use the unresolved interpreter.
"""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.commands.daemon.service as service_module
from hyprial.daemon.impl.operations.service import ServiceConfig, ServiceManager
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

MODULE_ARGS = ("-m", "hyprial.cli", "daemon", "run")


@pytest.fixture
def venvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    base = tmp_path / "base" / "bin" / "python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("#!stub\n")
    paths: dict[str, Path] = {"base": base}
    for name in ("mine", "foreign"):
        python = tmp_path / name / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.symlink_to(base)
        script = python.parent / "hyprial"
        script.write_text(f"#!{python}\nimport sys\n")
        paths[f"{name}-python"] = python
        paths[f"{name}-script"] = script
    monkeypatch.setattr(sys, "executable", str(paths["mine-python"]))
    return paths


def _render(tmp_path: Path, executable: Path, args: tuple[str, ...], platform: str) -> str:
    manager = ServiceManager(
        ServiceConfig(
            executable=executable,
            home=tmp_path / "account",
            hyprial_home=tmp_path / "account" / ".hyprial",
            state_dir=tmp_path / "state",
            path_env="/usr/bin",
            proxy_env={},
            daemon_args=args,
        ),
        platform=platform,
        service_home=tmp_path / "account",
        uid=501,
    )
    return manager._rendered()


def test_a_foreign_venv_script_first_on_path_is_not_written_into_the_unit(
    monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        service_module.shutil, "which", lambda name: str(venvs["foreign-script"])
    )

    executable, args = service_module._daemon_program()

    assert (executable, args) == (venvs["mine-python"], MODULE_ARGS)
    systemd = _render(tmp_path, executable, args, "systemd")
    launchd = _render(tmp_path, executable, args, "launchd")
    assert f'ExecStart="{venvs["mine-python"]}" -m hyprial.cli daemon run' in systemd
    assert plistlib.loads(launchd.encode())["ProgramArguments"] == [
        str(venvs["mine-python"]),
        *MODULE_ARGS,
    ]
    for rendered in (systemd, launchd):
        assert str(venvs["foreign-script"]) not in rendered
        assert str(venvs["base"]) not in rendered


def test_this_installations_own_console_script_is_accepted(
    monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path]
) -> None:
    monkeypatch.setattr(
        service_module.shutil, "which", lambda name: str(venvs["mine-script"])
    )

    assert service_module._daemon_program() == (venvs["mine-script"], ("daemon", "run"))


def test_a_symlinked_own_script_resolves_to_the_venv_script(
    monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path], tmp_path: Path
) -> None:
    link = tmp_path / "local" / "bin" / "hyprial"
    link.parent.mkdir(parents=True)
    link.symlink_to(venvs["mine-script"])
    monkeypatch.setattr(service_module.shutil, "which", lambda name: str(link))

    assert service_module._daemon_program() == (venvs["mine-script"], ("daemon", "run"))


def test_own_script_is_accepted_when_the_interpreter_is_reached_through_a_symlinked_directory(
    monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path], tmp_path: Path
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "mine", target_is_directory=True)
    monkeypatch.setattr(sys, "executable", str(alias / "bin" / "python"))
    monkeypatch.setattr(
        service_module.shutil, "which", lambda name: str(venvs["mine-script"])
    )

    executable, args = service_module._daemon_program()

    assert (executable, args) == (venvs["mine-script"], ("daemon", "run"))


def test_own_script_whose_shebang_spells_the_venv_through_a_symlink_is_accepted(
    monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path], tmp_path: Path
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "mine", target_is_directory=True)
    venvs["mine-script"].write_text(f"#!{alias / 'bin' / 'python'}\nimport sys\n")
    monkeypatch.setattr(
        service_module.shutil, "which", lambda name: str(venvs["mine-script"])
    )

    assert service_module._daemon_program() == (venvs["mine-script"], ("daemon", "run"))


def test_a_script_in_the_same_bin_directory_is_this_installations_own(
    monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path], tmp_path: Path
) -> None:
    venvs["mine-script"].write_text("#!/usr/bin/env hyprial-no-such-interpreter\n")
    monkeypatch.setattr(
        service_module.shutil, "which", lambda name: str(venvs["mine-script"])
    )

    assert service_module._daemon_program() == (venvs["mine-script"], ("daemon", "run"))


def test_a_foreign_script_is_still_rejected_through_a_symlinked_directory(
    monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path], tmp_path: Path
) -> None:
    alias = tmp_path / "foreign-alias"
    alias.symlink_to(tmp_path / "foreign", target_is_directory=True)
    monkeypatch.setattr(
        service_module.shutil, "which", lambda name: str(alias / "bin" / "hyprial")
    )

    assert service_module._daemon_program() == (venvs["mine-python"], MODULE_ARGS)


def test_the_interpreter_is_never_resolved_to_the_base_python(
    monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path]
) -> None:
    monkeypatch.setattr(service_module.shutil, "which", lambda name: None)

    executable, args = service_module._daemon_program()

    assert executable == venvs["mine-python"]
    assert executable != venvs["base"]
    assert args == MODULE_ARGS


@pytest.fixture
def lost_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # type: ignore[no-untyped-def]
    services = _cli_get_services()
    monkeypatch.setattr(services, "require_initialized_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(services, "_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(services, "_state_dir", lambda: tmp_path / "state")

    def build(revert):  # type: ignore[no-untyped-def]
        calls: list[str] = []

        def lost(_serving_pid):  # type: ignore[no-untyped-def]
            calls.append("wait")
            return SimpleNamespace(
                platform="launchd",
                unit="com.hyprial.hyprial.daemon",
                unit_path=tmp_path / "x.plist",
                installed=True,
                loaded=True,
                running=True,
                pid=4202,
                boot_persistent=None,
                changed=None,
                supervision="lost",
                serving_pid=None,
            )

        manager = SimpleNamespace(
            capture_unit_state=lambda: calls.append("capture") or ("old", True),
            install=lambda: calls.append("install") or SimpleNamespace(changed=True),
            wait_for_custody=lost,
            revert_install=revert,
        )
        monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
        return calls

    return build


@pytest.mark.parametrize(
    ("outcome", "phrase"),
    [
        ("removed", "unloaded and its file removed"),
        ("restored", "previous unit was restored"),
        ("unchanged", "left as it was"),
    ],
)
def test_install_names_what_it_reverted_when_custody_is_lost(
    lost_install, outcome: str, phrase: str
) -> None:
    reverted: list[object] = []
    calls = lost_install(lambda state: reverted.append(state) or outcome)

    result = CliRunner().invoke(
        _cli_root_app, ["daemon", "service", "install", "--yes", "--json"]
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "SERVICE_SUPERVISION_LOST"
    assert phrase in payload["error"]
    assert payload["data"]["install"] == {"supervision": "lost", "reverted": outcome}
    assert calls == ["capture", "install", "wait"]
    assert reverted == [("old", True)]


def test_install_says_so_when_the_revert_itself_fails(lost_install) -> None:
    def revert(_state):  # type: ignore[no-untyped-def]
        raise RuntimeError("bootout refused")

    lost_install(revert)

    result = CliRunner().invoke(
        _cli_root_app, ["daemon", "service", "install", "--yes", "--json"]
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "SERVICE_SUPERVISION_LOST"
    assert "could not be reverted" in payload["error"]
    assert "service uninstall" in payload["error"]
    assert payload["data"]["install"]["reverted"] == "failed"


def _doctor(monkeypatch: pytest.MonkeyPatch, **overrides: object):  # type: ignore[no-untyped-def]
    values = {
        "platform": "launchd",
        "unit": "com.hyprial.hyprial.daemon",
        "unit_path": Path("/tmp/x.plist"),
        "installed": True,
        "loaded": False,
        "running": False,
        "pid": None,
        "boot_persistent": None,
        "changed": None,
        "attributed": True,
        "template_matches": True,
        "serving_pid": None,
        "supervision": "lost",
        "relaunch_count": None,
        "last_exit_code": None,
        "ownership_busy": False,
        "ownership_reason": None,
        "backup_path": None,
    }
    values.update(overrides)
    manager = SimpleNamespace(inspect=lambda serving_pid=None: SimpleNamespace(**values))
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    return service_module.daemon_service_doctor_check(serving_pid=None, fix=False)


def test_doctor_does_not_show_a_deliberate_stop_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = _doctor(monkeypatch)

    assert check["status"] == "warn"
    assert check["metrics"]["reason"] == "stopped-on-request"
    assert check["metrics"]["supervision"] == "lost"
    assert check["action"]["command"] == "hyprial daemon start"


@pytest.mark.parametrize(
    "overrides",
    [
        {"loaded": True, "running": True, "pid": 4202, "serving_pid": 9001},
        {"loaded": True, "running": False, "relaunch_count": 7},
        {"serving_pid": 9001},
        {"attributed": False},
    ],
    ids=["custody-mismatch", "crash-looping-job", "detached-daemon-beside", "unattributed"],
)
def test_doctor_still_fails_every_other_lost_shape(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    assert _doctor(monkeypatch, **overrides)["status"] == "fail"
