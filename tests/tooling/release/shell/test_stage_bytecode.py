"""shell-6.md §D.4 6c-2: the staged bytecode is deterministic and complete.

The tiny fake tarball of ``test_stage_runtime`` cannot expose the one
non-determinism that mattered: ``compileall -j 0`` lets the marshal reference
flags depend on which worker compiled which file. These tests assert the
command line structurally and compile a realistic tree (the standard library
of the running interpreter and the ``hyprial`` package, copied into a temporary directory) twice through
the very function the staging uses.
"""

from __future__ import annotations

import hashlib
import importlib
import shutil
import sysconfig
from pathlib import Path

import pytest

from tests.tooling.release.shell.support import load

SKIPPED_PACKAGES = {"test", "idlelib", "lib2to3", "site-packages", "tkinter", "turtledemo", "ensurepip", "__pycache__", "config-3.12-x86_64-linux-gnu"}


def _stdlib_copy(destination: Path) -> int:
    source = Path(sysconfig.get_path("stdlib"))
    target = destination / "lib" / "python3.12"
    count = 0
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source)
        if SKIPPED_PACKAGES & set(relative.parts) or "site-packages" in relative.parts:
            continue
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target / relative)
        count += 1
    return count


def _add_package(destination: Path, name: str) -> int:
    package = importlib.import_module(name)
    source = Path(package.__file__ or "").parent
    target = destination / "lib" / "python3.12" / "site-packages" / name
    count = 0
    for path in sorted(source.rglob("*.py")):
        (target / path.relative_to(source)).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target / path.relative_to(source))
        count += 1
    return count


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.pyc")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def test_compile_command_is_single_process(tmp_path: Path) -> None:
    stage = load("stage_runtime")
    argv = stage.compile_command(tmp_path)
    assert [argv[index + 1] for index, value in enumerate(argv) if value == "-j"] == ["1"]
    assert "unchecked-hash" in argv
    assert str(tmp_path) in argv


def test_two_compilations_of_a_realistic_tree_are_byte_identical(tmp_path: Path) -> None:
    stage = load("stage_runtime")
    first, second = tmp_path / "a", tmp_path / "b"
    modules = _stdlib_copy(first) + _add_package(first, "hyprial")
    assert _stdlib_copy(second) + _add_package(second, "hyprial") == modules
    assert modules > 1000
    stage.compile_bytecode(first)
    stage.compile_bytecode(second)
    compiled = list(first.rglob("*.pyc"))
    assert len(compiled) == modules
    assert _tree_hash(first) == _tree_hash(second)


def test_a_module_that_does_not_compile_refuses_the_staging(tmp_path: Path) -> None:
    stage = load("stage_runtime")
    library = tmp_path / "lib" / "python3.12"
    library.mkdir(parents=True)
    (library / "good.py").write_text("X = 1\n", encoding="utf-8")
    (library / "legacy.py").write_text("print 'python 2'\n", encoding="utf-8")
    with pytest.raises(stage.StageError, match=r"legacy\.py"):
        stage.compile_bytecode(tmp_path)


def test_a_clean_tree_gets_bytecode_for_every_module(tmp_path: Path) -> None:
    stage = load("stage_runtime")
    library = tmp_path / "lib" / "python3.12"
    library.mkdir(parents=True)
    (library / "good.py").write_text("X = 1\n", encoding="utf-8")
    stage.compile_bytecode(tmp_path)
    assert (library / "__pycache__" / "good.cpython-312.pyc").is_file()
