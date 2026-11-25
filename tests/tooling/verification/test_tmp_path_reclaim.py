"""Verify protocol-end reclamation of function temporary directories."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def run_nested(tmp_path: Path, source: str, name: str) -> tuple[Path, subprocess.CompletedProcess[str]]:
    test_file = tmp_path / f"{name}.py"
    base = tmp_path / "basetemp"
    test_file.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-q", f"--basetemp={base}", str(test_file)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return base, result


def test_passed_tmp_path_waits_for_module_teardown_and_is_reclaimed(tmp_path):
    source = '''
import sqlite3
import pytest

database = None

@pytest.fixture(scope="module", autouse=True)
def module_guard():
    yield
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute("select value from marker").fetchone() == (7,)

@pytest.fixture
def indirect_fixture(tmp_path):
    return tmp_path

def test_pass(indirect_fixture):
    global database
    database = indirect_fixture / "state.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("create table marker (value integer)")
        connection.execute("insert into marker values (7)")
        connection.commit()
    readonly = indirect_fixture / "readonly"
    readonly.mkdir()
    (readonly / "child").write_text("data")
    (readonly / "child").chmod(0o400)
    readonly.chmod(0o500)
'''
    base, result = run_nested(tmp_path, source, "passed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(base.glob("passed*"))


def test_failed_tmp_path_is_retained(tmp_path):
    source = '''
def test_failure(tmp_path):
    (tmp_path / "failure-marker").write_text("kept")
    assert False
'''
    base, result = run_nested(tmp_path, source, "failed")
    assert result.returncode != 0
    retained = list(base.glob("test_failure*"))
    assert retained and (retained[0] / "failure-marker").is_file()


def test_factory_directories_are_retained_across_cases(tmp_path):
    source = '''
import pytest

@pytest.fixture(scope="session", autouse=True)
def session_directory(tmp_path_factory):
    path = tmp_path_factory.mktemp("session-shared")
    (path / "marker").write_text("kept")
    return path

def test_first(tmp_path, session_directory):
    assert session_directory.joinpath("marker").is_file()

def test_second(tmp_path, session_directory):
    assert session_directory.joinpath("marker").is_file()
'''
    base, result = run_nested(tmp_path, source, "factory")
    assert result.returncode == 0, result.stdout + result.stderr
    retained = list(base.glob("session-shared*"))
    assert retained and (retained[0] / "marker").is_file()
