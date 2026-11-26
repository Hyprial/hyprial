"""The retired-GUI fixture check decides from the tree alone.

``scripts/ci/check-retired-fixture.sh`` reports whether a retired packaged-GUI
root (``app``, ``src/gui``) is present so a caller can skip the work that
needs it.  These cases run the script against synthetic trees.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[4]


def _fixture_check(tmp_path, *roots):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/ci/check-retired-fixture.sh"), *roots],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_retired_fixture_absent_roots_skip(tmp_path) -> None:
    result = _fixture_check(tmp_path, "app", "src/gui")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "present=false"
    assert "absent" in result.stderr


def test_retired_fixture_present_root_without_lock_is_red(tmp_path) -> None:
    (tmp_path / "src/gui").mkdir(parents=True)
    result = _fixture_check(tmp_path, "app", "src/gui")
    assert result.returncode == 1
    assert "src/gui/package-lock.json" in result.stderr


def test_retired_fixture_complete_roots_are_present(tmp_path) -> None:
    for root in ("app", "src/gui"):
        (tmp_path / root).mkdir(parents=True)
        (tmp_path / root / "package-lock.json").write_text("{}")
    result = _fixture_check(tmp_path, "app", "src/gui")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "present=true"


def test_retired_fixture_partial_deletion_is_red(tmp_path) -> None:
    (tmp_path / "src/gui").mkdir(parents=True)
    (tmp_path / "src/gui/package-lock.json").write_text("{}")
    result = _fixture_check(tmp_path, "app", "src/gui")
    assert result.returncode == 1
    assert "app" in result.stderr
