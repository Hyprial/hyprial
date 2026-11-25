"""The cgroup oom_kill reader behind CI_WORKER_EXIT never fails the run."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.ci.cgroup_memory import cgroup_root, oom_kill, oom_kill_since


def _events(root: Path, text: str | bytes, name: str = "memory.events") -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        target.write_bytes(text)
    else:
        target.write_text(text)


def test_cgroup_v2_counter_is_read(tmp_path: Path) -> None:
    _events(tmp_path, "low 0\nhigh 0\nmax 4\noom 2\noom_kill 2\noom_group_kill 0\n")
    assert oom_kill(tmp_path) == 2


def test_cgroup_v1_counter_is_read(tmp_path: Path) -> None:
    _events(tmp_path, "oom_kill_disable 0\nunder_oom 0\noom_kill 6\n", "memory/memory.oom_control")
    assert oom_kill(tmp_path) == 6


def test_a_v1_file_without_the_counter_is_unknown(tmp_path: Path) -> None:
    _events(tmp_path, "oom_kill_disable 0\nunder_oom 0\n", "memory/memory.oom_control")
    assert oom_kill(tmp_path) is None


def test_absent_files_are_unknown(tmp_path: Path) -> None:
    assert oom_kill(tmp_path / "nothing-here") is None
    assert oom_kill_since(4, tmp_path / "nothing-here") == (None, None)


@pytest.mark.parametrize(
    "text", ["oom_kill x\n", "oom_kill\n", "oom_kill -1\n", "oom_kill 1 2\n", b"\xff\xfe\x00", ""]
)
def test_malformed_files_are_unknown(tmp_path: Path, text: str | bytes) -> None:
    _events(tmp_path, text)
    assert oom_kill(tmp_path) is None


def test_a_directory_in_place_of_the_file_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "memory.events").mkdir()
    assert oom_kill(tmp_path) is None


def test_growth_is_measured_from_the_start_value(tmp_path: Path) -> None:
    _events(tmp_path, "oom_kill 9\n")
    assert oom_kill_since(7, tmp_path) == (9, 2)
    assert oom_kill_since(None, tmp_path) == (9, None)


def test_the_root_can_be_redirected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CI_CGROUP_ROOT", str(tmp_path))
    assert cgroup_root() == tmp_path
    monkeypatch.delenv("CI_CGROUP_ROOT")
    assert cgroup_root() == Path("/sys/fs/cgroup")
