"""N1 (launchd acceptance, 2026-11-16): one installation's interpreter spellings.

``same_installation_program`` follows symlinks only while they stay inside the
entry's own directory, resolves only the directory, and never raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.configuration.launchd_jobs import same_installation_program


@pytest.fixture
def venv(tmp_path: Path) -> dict[str, Path]:
    base = tmp_path / "base" / "bin" / "python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("#!stub\n")
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(base)
    (bin_dir / "python3").symlink_to("python")
    (bin_dir / "python3.12").symlink_to(bin_dir / "python")
    return {"base": base, "bin": bin_dir, "root": tmp_path}


def test_sibling_symlinks_to_the_interpreter_are_the_same_installation(
    venv: dict[str, Path],
) -> None:
    bin_dir = venv["bin"]

    assert same_installation_program(bin_dir / "python3", bin_dir / "python")
    assert same_installation_program(bin_dir / "python3.12", bin_dir / "python")
    assert same_installation_program(bin_dir / "python3", bin_dir / "python3.12")
    assert same_installation_program(bin_dir / "python", bin_dir / "python")


def test_the_venv_interpreter_is_not_its_resolved_base_interpreter(
    venv: dict[str, Path],
) -> None:
    bin_dir = venv["bin"]

    for spelling in ("python", "python3", "python3.12"):
        assert not same_installation_program(bin_dir / spelling, venv["base"])
        assert not same_installation_program(venv["base"], bin_dir / spelling)


def test_the_same_name_in_another_directory_is_a_different_installation(
    venv: dict[str, Path],
) -> None:
    other = venv["root"] / "other" / "bin"
    other.mkdir(parents=True)
    (other / "python").symlink_to(venv["base"])
    (other / "python3").symlink_to("python")

    assert not same_installation_program(other / "python", venv["bin"] / "python")
    assert not same_installation_program(other / "python3", venv["bin"] / "python3")


def test_a_link_leaving_the_directory_ends_the_chain_at_the_link(
    venv: dict[str, Path],
) -> None:
    bin_dir = venv["bin"]
    (bin_dir / "py-also").symlink_to(venv["base"])

    assert not same_installation_program(bin_dir / "py-also", bin_dir / "python")
    assert not same_installation_program(bin_dir / "py-also", venv["base"])


def test_distinct_regular_files_in_one_directory_are_different(
    venv: dict[str, Path],
) -> None:
    (venv["bin"] / "pip").write_text("#!stub\n")

    assert not same_installation_program(venv["bin"] / "pip", venv["bin"] / "python")


def test_an_aliased_directory_spelling_is_the_same_installation(
    venv: dict[str, Path],
) -> None:
    alias = venv["root"] / "alias"
    alias.symlink_to(venv["root"] / "venv")

    assert same_installation_program(alias / "bin" / "python3", venv["bin"] / "python")
    assert same_installation_program(alias / "bin" / "python", venv["bin"] / "python")


def test_dot_segments_and_relative_links_are_normalised(venv: dict[str, Path]) -> None:
    bin_dir = venv["bin"]
    (bin_dir / "py").symlink_to("./python")

    assert same_installation_program(bin_dir / "py", bin_dir / ".." / "bin" / "python")


def test_missing_paths_and_link_loops_never_raise(
    tmp_path: Path, venv: dict[str, Path]
) -> None:
    loop = venv["bin"] / "loop-a"
    loop.symlink_to("loop-b")
    (venv["bin"] / "loop-b").symlink_to("loop-a")
    missing = tmp_path / "nowhere" / "bin" / "python"

    assert same_installation_program(missing, missing)
    assert not same_installation_program(missing, venv["bin"] / "python")
    assert same_installation_program(loop, loop) in (True, False)
    assert not same_installation_program(loop, venv["bin"] / "python")
    assert not same_installation_program("", venv["bin"] / "python")
