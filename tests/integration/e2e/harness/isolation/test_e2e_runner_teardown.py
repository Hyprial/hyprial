"""Regression coverage for the E2E runner teardown mask (Issue 1).

The runners wrapped their scenario body in ``try: ... finally: daemon.stop()``.
When the body failed AND the teardown ``daemon.stop()`` also raised (a wedged
daemon whose ``stop`` IPC times out), the teardown exception replaced the
original one, so the runner reported the teardown error and lost the true
cause.  ``teardown_guard`` in the shared runner helper closes that gap.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# The runners live outside the package and rely on their own script directory
# being on sys.path; mirror that so the shared helper imports the same way.
_SCENARIOS = Path(__file__).resolve().parents[5] / 'contract/e2e'
if str(_SCENARIOS) not in sys.path:
    sys.path.insert(0, str(_SCENARIOS))

from _daemon_runner import (  # noqa: E402
    short_temp_root,
    teardown_guard,
    write_project_version,
)


class _Interrupt(BaseException):
    """Stand-in for the RunnerInterrupted some runners raise from signals."""


def test_original_exception_surfaces_when_teardown_also_raises() -> None:
    recorded: list[str] = []

    def stop() -> None:
        raise RuntimeError("daemon stop IPC timed out")

    with pytest.raises(ValueError, match="the real cause") as excinfo:
        with teardown_guard(stop, record=recorded.append):
            raise ValueError("the real cause")

    # The original is what propagates; the teardown error is recorded, not lost.
    assert isinstance(excinfo.value, ValueError)
    assert any("daemon stop IPC timed out" in message for message in recorded)


def test_all_steps_run_after_body_failure_and_original_reraised() -> None:
    calls: list[str] = []
    recorded: list[str] = []

    def step_a() -> None:
        calls.append("a")
        raise RuntimeError("a failed")

    def step_b() -> None:
        calls.append("b")

    with pytest.raises(KeyError, match="original"):
        with teardown_guard(step_a, step_b, record=recorded.append):
            raise KeyError("original")

    # A failing first step must not skip the second, and its error is recorded.
    assert calls == ["a", "b"]
    assert any("a failed" in message for message in recorded)


def test_clean_body_still_propagates_a_teardown_failure() -> None:
    def stop() -> None:
        raise RuntimeError("teardown boom")

    with pytest.raises(RuntimeError, match="teardown boom"):
        with teardown_guard(stop):
            pass  # body succeeds; a broken teardown must still fail the run


def test_clean_body_attempts_every_step_before_raising() -> None:
    calls: list[str] = []

    def step_a() -> None:
        calls.append("a")
        raise RuntimeError("a failed")

    def step_b() -> None:
        calls.append("b")

    with pytest.raises(RuntimeError, match="a failed"):
        with teardown_guard(step_a, step_b):
            pass

    assert calls == ["a", "b"]


def test_teardown_runs_for_baseexception_body() -> None:
    ran: list[bool] = []

    with pytest.raises(_Interrupt):
        with teardown_guard(lambda: ran.append(True)):
            raise _Interrupt()

    assert ran == [True]


def test_original_survives_even_when_recording_itself_raises() -> None:
    # `record` is typically report.add -> print(...), which raises BrokenPipeError
    # if the consumer went away. A failing recorder must not become the reported
    # error -- that would recreate the very mask this guard exists to prevent.
    def failing_record(_message: str) -> None:
        raise BrokenPipeError("consumer closed the pipe")

    def stop() -> None:
        raise RuntimeError("daemon stop IPC timed out")

    with pytest.raises(ValueError, match="the real cause"):
        with teardown_guard(stop, record=failing_record):
            raise ValueError("the real cause")


def test_clean_body_with_healthy_teardown_is_a_no_op() -> None:
    ran: list[bool] = []
    with teardown_guard(lambda: ran.append(True)):
        pass
    assert ran == [True]


def test_short_temp_root_exists_and_is_writable() -> None:
    root = short_temp_root()
    assert root.is_dir()
    with tempfile.TemporaryDirectory(prefix="hyprial-short-root-test-", dir=root) as raw:
        assert Path(raw).parent == root


def test_disposable_project_version_rewrite_accepts_stable_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "hyprial"\nversion = "0.4.0"\n'
        '[dependency]\nversion = "9.9.9"\n',
        encoding="utf-8",
    )

    old = write_project_version(project, "0.4.0.dev0")

    assert old == "0.4.0"
    assert project.read_text(encoding="utf-8").count('version = "0.4.0.dev0"') == 1
    assert 'version = "9.9.9"' in project.read_text(encoding="utf-8")


def test_disposable_project_version_rewrite_fails_on_wrong_generation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nversion = "0.4.0.dev0"\n', encoding="utf-8")

    with pytest.raises(AssertionError, match="expected disposable package version"):
        write_project_version(project, "0.4.0.dev2", expected="0.4.0.dev1")

    assert 'version = "0.4.0.dev0"' in project.read_text(encoding="utf-8")


def _dynamic_project(tmp_path: Path, version: str) -> Path:
    # The layout since d9382ea6: pyproject declares the version dynamic and
    # hatchling reads the first line of ./VERSION.
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "hyprial"\ndynamic = ["version"]\n'
        '[tool.hatch.version]\npath = "VERSION"\n'
        'pattern = "^(?P<version>[^\\\\s]+)"\n',
        encoding="utf-8",
    )
    (tmp_path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return project


def test_disposable_project_version_rewrite_follows_the_version_file(
    tmp_path: Path,
) -> None:
    project = _dynamic_project(tmp_path, "0.4.6")
    pyproject_before = project.read_text(encoding="utf-8")

    old = write_project_version(project, "0.4.0.dev0")

    assert old == "0.4.6"
    assert (tmp_path / "VERSION").read_text(encoding="utf-8") == "0.4.0.dev0\n"
    assert project.read_text(encoding="utf-8") == pyproject_before


def test_disposable_project_version_file_checks_the_generation(
    tmp_path: Path,
) -> None:
    project = _dynamic_project(tmp_path, "0.4.0.dev0")

    with pytest.raises(AssertionError, match="expected disposable package version"):
        write_project_version(project, "0.4.0.dev2", expected="0.4.0.dev1")

    assert (tmp_path / "VERSION").read_text(encoding="utf-8") == "0.4.0.dev0\n"


def test_disposable_project_version_names_a_missing_source(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname = "hyprial"\n', encoding="utf-8")

    with pytest.raises(AssertionError, match="project version is missing"):
        write_project_version(project, "0.4.0.dev0")
