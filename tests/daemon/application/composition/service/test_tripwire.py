"""Exercise the session tripwire against fake homes, never real service files."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.runtime.environment import isolated_environment, service_unit_snapshot


@pytest.mark.parametrize("change", ["new", "mtime", "removed", "unchanged"])
def test_tripwire_controls_pytest_exit_status(tmp_path, change):
    env = isolated_environment(tmp_path)
    home = Path(env["HOME"])
    unit = home / "Library/LaunchAgents/com.hyprial.test.plist"
    unit.parent.mkdir(parents=True)
    if change != "new":
        unit.write_text("contents must not be read")
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\n"
        "from tests.support.runtime.environment import ServiceUnitTripwire\n"
        "def pytest_configure(config):\n"
        f"    config.pluginmanager.register(ServiceUnitTripwire(Path({str(home)!r})))\n"
    )
    action = {
        "new": "unit.touch()",
        "mtime": "os.utime(unit, ns=(1, 1))",
        "removed": "unit.unlink()",
        "unchanged": "pass",
    }[change]
    (tmp_path / "test_change.py").write_text(
        "from pathlib import Path\nimport os\n"
        f"def test_change():\n    unit = Path({str(unit)!r})\n    {action}\n"
    )
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[5])
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--confcutdir", str(tmp_path), str(tmp_path)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == (0 if change == "unchanged" else 1), result.stdout + result.stderr
    assert ("REAL USER SERVICE UNIT CHANGED" in result.stdout) == (change != "unchanged")


def test_snapshot_covers_both_managers_without_reading_contents(tmp_path, monkeypatch):
    units = [tmp_path / suffix for suffix in (
        "Library/LaunchAgents/com.hyprial.test.plist",
        ".config/systemd/user/hyprial.service",
    )]
    for unit in units:
        unit.parent.mkdir(parents=True)
        unit.write_text("not read")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("read contents"))
    before = service_unit_snapshot(tmp_path)
    os.utime(units[1], ns=(1, 1))
    after = service_unit_snapshot(tmp_path)
    assert len(before) == 2
    assert before != after


def test_cleanup_removes_only_exact_isolated_units(tmp_path):
    from tests.support.runtime.environment import remove_isolated_service_files

    env = isolated_environment(tmp_path)
    home = Path(env["HOME"])
    for suffix in ("Library/LaunchAgents/com.hyprial.hyprial.daemon.plist", ".config/systemd/user/hyprial.service"):
        unit = home / suffix
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.touch()
    unrelated = home / "Library/LaunchAgents/unrelated.plist"
    unrelated.touch()
    remove_isolated_service_files(tmp_path, env)
    assert service_unit_snapshot(home) == {}
    assert unrelated.exists()


def test_cleanup_rejects_a_symlink_outside_the_isolated_home(tmp_path):
    from tests.support.runtime.environment import remove_isolated_service_files

    env = isolated_environment(tmp_path / "test")
    home = Path(env["HOME"])
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "com.hyprial.hyprial.daemon.plist"
    sentinel.touch()
    (home / "Library").mkdir()
    (home / "Library/LaunchAgents").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AssertionError):
        remove_isolated_service_files(tmp_path / "test", env)
    assert sentinel.exists()
