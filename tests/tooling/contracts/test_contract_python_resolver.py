from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import textwrap
import venv
from collections.abc import Callable

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def clear_ambient_contract_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every resolver case own the selections it is asserting."""

    for name in ("HYPRIAL_BIN", "HYPRIAL_PYTHON", "VIRTUAL_ENV"):
        monkeypatch.delenv(name, raising=False)


def _test_path(*directories: Path) -> str:
    """Put test-owned executables first without assuming a host PATH layout."""

    return os.pathsep.join([*(str(directory) for directory in directories), os.defpath])


@pytest.fixture(scope="module")
def dependency_free_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a controlled Python >=3.12 without project dependencies."""

    environment = tmp_path_factory.mktemp("dependency-free-python") / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    executable = environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    probe = subprocess.run(
        [str(executable), "-I", "-c", "import lark_oapi"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert probe.returncode != 0, (
        "controlled interpreter unexpectedly has project dependencies"
    )
    return executable


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _sandbox(tmp_path: Path, *, standalone: bool = False) -> Path:
    root = tmp_path / "checkout with spaces"
    (root / "contract" / "cli-blackbox").mkdir(parents=True)
    shutil.copy2(ROOT / "contract" / "run-all.sh", root / "contract" / "run-all.sh")
    shutil.copy2(
        ROOT / "contract" / "cli-blackbox" / "run.sh",
        root / "contract" / "cli-blackbox" / "run.sh",
    )
    resolver = (
        ROOT / "contract" / "python-resolver.sh"
        if not standalone
        else ROOT / "contract" / "python-resolver.sh.missing"
    )
    if resolver.exists():
        shutil.copy2(resolver, root / "contract" / "python-resolver.sh")
    for relative in (
       "cli-blackbox/run.py",
        "wire/golden/validate.py",
        "e2e/validate.py",
        "daemon-lifecycle/run.py",
        "mcp-tools/run.py",
        "workers/python-worker/validate.py",
        "workers/user-proxy/validate.py",
        "http-request/validate.py",
    ):
        _write_executable(
            root / "contract" / relative,
            "import os, pathlib, sys\n"
            "pathlib.Path(os.environ['HYPRIAL_TEST_CHILD_LOG']).open('a').write("
            "f'{pathlib.Path(__file__).as_posix()}|{sys.executable}\\n')\n",
        )
    return root


def _fake_hyprial(path: Path, marker: str) -> Path:
    _write_executable(
        path,
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            from pathlib import Path
            import sys

            Path(os.environ["HYPRIAL_TEST_CLI_LOG"]).open("a").write({marker!r} + "\\n")
            command = sys.argv[1]
            args = sys.argv[2:]
            home = os.environ.get("HYPRIAL_HOME")
            if command != "init" and home is not None and not Path(home).is_dir():
                print(json.dumps({{
                    "ok": False, "code": "HYPRIAL_HOME_NOT_INITIALIZED",
                    "error": "HYPRIAL home does not exist or is not a directory: "
                    + home + " (source: HYPRIAL_HOME environment variable)",
                }}, separators=(",", ":")))
                raise SystemExit(1)
            if command == "agent" and args[:1] == ["start"] and "--help" in args:
                print("Usage: hyprial agent start [OPTIONS] HARNESS [--effort LEVEL] [-- ARGS]")
                raise SystemExit(0)
            if command == "agent" and args[:1] == ["start"] and "--effort" in args:
                print(json.dumps({{"ok": False, "code": "INVALID_ARGUMENT",
                                  "error": "invalid --effort for harness"}},
                                 separators=(",", ":")))
                raise SystemExit(1)
            if command == "help":
                value = {{"ok": True, "usage": " " .join("hyprial " + name for name in (
                    "version", "doctor", "log", "message"
                ))}}
            elif command == "version":
                value = {{"ok": True, "packageVersion": "0.4.1",
                          "localVersion": "0.4.1", "registry": "test"}}
            elif command == "ps":
                value = {{"ok": True, "daemon": {{"running": False}}, "connectors": []}}
            elif command == "daemon" and args[:1] == ["status"]:
                value = {{"ok": True, "daemon": {{"running": False}}}}
            elif command == "log" and "--since" in args:
                value = {{"ok": False, "error": "--since must not be later than --until"}}
            elif command == "log" and "--component" in args:
                value = {{"ok": True, "skippedLines": 1,
                          "entries": [{{"event": "worker.exited"}}]}}
            elif command == "log":
                value = {{"ok": True, "skippedLines": 1,
                          "entries": [{{"event": "send.received"}}]}}
            elif command == "message" and args[:1] == ["send"] and "--summary" not in args:
                value = {{"ok": False, "code": "SUMMARY_REQUIRED",
                          "error": "--summary is required (at most 512 UTF-8 bytes)"}}
            else:
                value = {{"ok": False, "code": "DAEMON_UNAVAILABLE",
                          "error": "cannot connect to the Harness daemon socket /s/daemon.sock: [Errno 2]"}}
            print(json.dumps(value, separators=(",", ":")))
            raise SystemExit(1 if command in {{"message"}} or
                             (command == "log" and "--since" in args) else 0)
            """
        ),
    )
    return path


def _cli_sandbox(tmp_path: Path) -> Path:
    root = _sandbox(tmp_path)
    shutil.copy2(ROOT / "contract" / "cli-blackbox" / "run.py", root / "contract" / "cli-blackbox" / "run.py")
    _python_wrapper(root / ".venv" / "bin" / "python", "repo-venv")
    return root


def _python_wrapper(path: Path, marker: str) -> Path:
    _write_executable(
        path,
        textwrap.dedent(
            f"""\
            #!/bin/sh
            printf '%s\\n' {marker!r} >> "$HYPRIAL_TEST_RESOLVER_LOG"
            exec {str(Path(sys.executable))!r} "$@"
            """
        ),
    )
    return path


def _non_python_executable(path: Path, output: str = "") -> Path:
    _write_executable(
        path,
        "#!/bin/sh\n" + (f"printf '%s\\n' {output!r}\n" if output else "exit 0\n"),
    )
    return path


def _run(root: Path, tmp_path: Path, **env_changes: str) -> subprocess.CompletedProcess[str]:
    child_log = tmp_path / "children.log"
    resolver_log = tmp_path / "resolver.log"
    env = {
        **os.environ,
        "PATH": _test_path(),
        "HYPRIAL_TEST_CHILD_LOG": str(child_log),
        "HYPRIAL_TEST_RESOLVER_LOG": str(resolver_log),
        "HYPRIAL_TEST_CLI_LOG": str(tmp_path / "cli.log"),
        **env_changes,
    }
    env.pop("VIRTUAL_ENV", None)
    if "VIRTUAL_ENV" in env_changes:
        env["VIRTUAL_ENV"] = env_changes["VIRTUAL_ENV"]
    return subprocess.run(
        [str(root / "contract" / "run-all.sh"), "--isolated-only"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_bare_run_all_prefers_repo_cli_over_path_with_restricted_environment(
    tmp_path: Path,
) -> None:
    root = _cli_sandbox(tmp_path)
    _fake_hyprial(root / ".venv" / "bin" / "hyprial", "repo-cli")
    path_bin = tmp_path / "path bin"
    _fake_hyprial(path_bin / "hyprial", "path-cli")

    completed = _run(root, tmp_path, PATH=_test_path(path_bin))

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert set((tmp_path / "cli.log").read_text().splitlines()) == {"repo-cli"}


def test_explicit_cli_priority_is_flag_then_env_then_repo_then_path(tmp_path: Path) -> None:
    root = _cli_sandbox(tmp_path)
    repo = _fake_hyprial(root / ".venv" / "bin" / "hyprial", "repo-cli")
    explicit = _fake_hyprial(tmp_path / "explicit cli", "flag-cli")
    env_cli = _fake_hyprial(tmp_path / "environment cli", "env-cli")
    path_bin = tmp_path / "path bin"
    _fake_hyprial(path_bin / "hyprial", "path-cli")

    cases = (
        (["--hyprial", str(explicit)], {"HYPRIAL_BIN": str(env_cli)}, "flag-cli"),
        ([], {"HYPRIAL_BIN": str(env_cli)}, "env-cli"),
        ([], {}, "repo-cli"),
    )
    for index, (args, extra_env, expected) in enumerate(cases):
        log = tmp_path / f"cli-{index}.log"
        env = {
            **os.environ,
            "PATH": _test_path(path_bin),
            "HYPRIAL_PYTHON": str(root / ".venv" / "bin" / "python"),
            "HYPRIAL_TEST_CHILD_LOG": str(tmp_path / f"children-{index}.log"),
            "HYPRIAL_TEST_RESOLVER_LOG": str(tmp_path / f"resolver-{index}.log"),
            "HYPRIAL_TEST_CLI_LOG": str(log),
            **extra_env,
        }
        completed = subprocess.run(
            [str(root / "contract" / "run-all.sh"), "--isolated-only", *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert set(log.read_text().splitlines()) == {expected}

    repo.unlink()
    path_log = tmp_path / "cli-path.log"
    completed = subprocess.run(
        [str(root / "contract" / "run-all.sh"), "--isolated-only"],
        cwd=root,
        env={
            **os.environ,
            "PATH": _test_path(path_bin),
            "HYPRIAL_PYTHON": str(root / ".venv" / "bin" / "python"),
            "HYPRIAL_TEST_CHILD_LOG": str(tmp_path / "children-path.log"),
            "HYPRIAL_TEST_RESOLVER_LOG": str(tmp_path / "resolver-path.log"),
            "HYPRIAL_TEST_CLI_LOG": str(path_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert set(path_log.read_text().splitlines()) == {"path-cli"}


@pytest.mark.parametrize(
    ("source", "kind", "expected"),
    (
        ("flag", "empty", "is empty"),
        ("flag", "missing", "does not exist"),
        ("flag", "non-executable", "is not executable"),
        ("environment", "empty", "is empty"),
        ("environment", "missing", "does not exist"),
        ("environment", "non-executable", "is not executable"),
        ("repository", "missing", "does not exist"),
        ("repository", "non-executable", "is not executable"),
    ),
)
def test_invalid_cli_selection_fails_closed_with_source_and_path(
    tmp_path: Path, source: str, kind: str, expected: str
) -> None:
    root = _cli_sandbox(tmp_path)
    repo_cli = root / ".venv" / "bin" / "hyprial"
    path_bin = tmp_path / "path bin"
    _fake_hyprial(path_bin / "hyprial", "path-cli")
    selected = ""
    if kind == "missing":
        selected = str(tmp_path / "missing cli")
    elif kind == "non-executable":
        selected_path = tmp_path / "non executable cli"
        selected_path.write_text("not executable", encoding="utf-8")
        selected = str(selected_path)

    args: list[str] = []
    extra_env: dict[str, str] = {}
    expected_source = {"flag": "--hyprial", "environment": "HYPRIAL_BIN"}.get(source)
    if source == "flag":
        args = ["--hyprial", selected]
    elif source == "environment":
        extra_env["HYPRIAL_BIN"] = selected
    else:
        repo_cli.parent.mkdir(parents=True, exist_ok=True)
        if kind == "missing":
            repo_cli.symlink_to(selected)
        else:
            repo_cli.write_text("not executable", encoding="utf-8")
        expected_source = "repository .venv/bin/hyprial"

    completed = subprocess.run(
        [str(root / "contract" / "run-all.sh"), "--isolated-only", *args],
        cwd=root,
        env={
            **os.environ,
            "PATH": _test_path(path_bin),
            "HYPRIAL_PYTHON": str(root / ".venv" / "bin" / "python"),
            "HYPRIAL_TEST_CHILD_LOG": str(tmp_path / "children-invalid.log"),
            "HYPRIAL_TEST_RESOLVER_LOG": str(tmp_path / "resolver-invalid.log"),
            "HYPRIAL_TEST_CLI_LOG": str(tmp_path / "cli-invalid.log"),
            **extra_env,
        },
        text=True,
        capture_output=True,
        check=False,
    )

    output = completed.stderr + completed.stdout
    assert completed.returncode != 0
    assert expected_source in output
    assert expected in output
    if selected and source != "repository":
        assert selected in output
    assert not (tmp_path / "cli-invalid.log").exists()


@pytest.mark.parametrize("source", ("flag", "environment", "repository"))
@pytest.mark.parametrize("kind", ("broken", "directory"))
def test_cli_rejects_broken_links_and_directories_without_fallback(
    tmp_path: Path, source: str, kind: str
) -> None:
    root = _cli_sandbox(tmp_path)
    repo_cli = root / ".venv" / "bin" / "hyprial"
    repo_cli.unlink(missing_ok=True)
    selected = repo_cli if source == "repository" else tmp_path / f"{source} cli"
    selected.parent.mkdir(parents=True, exist_ok=True)
    if kind == "broken":
        selected.symlink_to(tmp_path / "missing cli target")
    else:
        selected.mkdir()
    path_bin = tmp_path / "path bin"
    _fake_hyprial(path_bin / "hyprial", "path-cli")
    args = ["--hyprial", str(selected)] if source == "flag" else []
    extra_env = {"HYPRIAL_BIN": str(selected)} if source == "environment" else {}

    completed = subprocess.run(
        [str(root / "contract" / "run-all.sh"), "--isolated-only", *args],
        cwd=root,
        env={
            **os.environ,
            "PATH": _test_path(path_bin),
            "HYPRIAL_PYTHON": str(root / ".venv" / "bin" / "python"),
            "HYPRIAL_TEST_CHILD_LOG": str(tmp_path / "children-cli-kind.log"),
            "HYPRIAL_TEST_RESOLVER_LOG": str(tmp_path / "resolver-cli-kind.log"),
            "HYPRIAL_TEST_CLI_LOG": str(tmp_path / "cli-kind.log"),
            **extra_env,
        },
        text=True,
        capture_output=True,
        check=False,
    )

    output = completed.stderr + completed.stdout
    assert completed.returncode != 0
    assert str(selected) in output
    assert "broken symlink" in output if kind == "broken" else "not a file" in output
    assert not (tmp_path / "cli-kind.log").exists()


@pytest.mark.parametrize("source", ("flag", "environment"))
def test_cli_selection_is_never_shell_evaluated(tmp_path: Path, source: str) -> None:
    root = _cli_sandbox(tmp_path)
    (root / ".venv" / "bin" / "hyprial").unlink(missing_ok=True)
    injected = tmp_path / "cli-injected"
    value = f"$(touch {injected})"
    args = ["--hyprial", value] if source == "flag" else []
    extra_env = {"HYPRIAL_BIN": value} if source == "environment" else {}

    completed = subprocess.run(
        [str(root / "contract" / "run-all.sh"), "--isolated-only", *args],
        cwd=root,
        env={
            **os.environ,
            "PATH": _test_path(),
            "HYPRIAL_PYTHON": str(root / ".venv" / "bin" / "python"),
            "HYPRIAL_TEST_CHILD_LOG": str(tmp_path / "children-cli-injection.log"),
            "HYPRIAL_TEST_RESOLVER_LOG": str(tmp_path / "resolver-cli-injection.log"),
            "HYPRIAL_TEST_CLI_LOG": str(tmp_path / "cli-injection.log"),
            **extra_env,
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert value in completed.stdout + completed.stderr
    assert not injected.exists()


def test_standalone_cli_blackbox_uses_path_cli_not_repo_default(tmp_path: Path) -> None:
    root = _cli_sandbox(tmp_path)
    _fake_hyprial(root / ".venv" / "bin" / "hyprial", "repo-cli")
    path_bin = tmp_path / "path bin"
    _fake_hyprial(path_bin / "hyprial", "standalone-ts-cli")
    cli_log = tmp_path / "standalone-cli.log"

    completed = subprocess.run(
        [str(root / "contract" / "cli-blackbox" / "run.sh"), "--isolated-only"],
        cwd=root,
        env={
            **os.environ,
            "PATH": _test_path(path_bin),
            "HYPRIAL_PYTHON": str(root / ".venv" / "bin" / "python"),
            "HYPRIAL_TEST_CHILD_LOG": str(tmp_path / "standalone-children.log"),
            "HYPRIAL_TEST_RESOLVER_LOG": str(tmp_path / "standalone-resolver.log"),
            "HYPRIAL_TEST_CLI_LOG": str(cli_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert set(cli_log.read_text().splitlines()) == {"standalone-ts-cli"}


def test_bare_run_all_prefers_repo_venv_even_when_path_has_only_system_python(
    tmp_path: Path,
) -> None:
    root = _sandbox(tmp_path)
    _python_wrapper(root / ".venv" / "bin" / "python", "repo-venv")

    completed = _run(root, tmp_path)

    assert completed.returncode == 0, completed.stderr
    # Resolution, child validation, dependency probe, and eight contract runners.
    assert (tmp_path / "resolver.log").read_text().splitlines() == ["repo-venv"] * 11
    assert len((tmp_path / "children.log").read_text().splitlines()) == 8


def test_explicit_hyprial_python_wins_over_repo_and_virtualenv(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    explicit = _python_wrapper(tmp_path / "explicit python", "explicit")
    _python_wrapper(root / ".venv" / "bin" / "python", "repo-venv")
    virtual = _python_wrapper(tmp_path / "active venv" / "bin" / "python", "active")

    completed = _run(
        root,
        tmp_path,
        HYPRIAL_PYTHON=str(explicit),
        VIRTUAL_ENV=str(virtual.parents[1]),
    )

    assert completed.returncode == 0, completed.stderr
    assert set((tmp_path / "resolver.log").read_text().splitlines()) == {"explicit"}


def test_active_virtualenv_precedes_path_when_repo_venv_is_absent(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    virtual = _python_wrapper(tmp_path / "active venv" / "bin" / "python", "active")

    completed = _run(root, tmp_path, VIRTUAL_ENV=str(virtual.parents[1]))

    assert completed.returncode == 0, completed.stderr
    assert set((tmp_path / "resolver.log").read_text().splitlines()) == {"active"}


@pytest.mark.parametrize("kind", ("missing", "broken", "directory", "non-executable"))
def test_asserted_nonempty_virtualenv_is_authoritative_and_invalid_fails_closed(
    tmp_path: Path, kind: str
) -> None:
    root = _sandbox(tmp_path)
    virtual_root = tmp_path / "asserted venv"
    candidate = virtual_root / "bin" / "python"
    candidate.parent.mkdir(parents=True)
    if kind == "broken":
        candidate.symlink_to(tmp_path / "missing target")
    elif kind == "directory":
        candidate.mkdir()
    elif kind == "non-executable":
        candidate.write_text("not executable", encoding="utf-8")
    path_bin = tmp_path / "fallback bin"
    _python_wrapper(path_bin / "python3", "path-python3")

    completed = _run(
        root,
        tmp_path,
        VIRTUAL_ENV=str(virtual_root),
        PATH=_test_path(path_bin),
    )

    assert completed.returncode != 0
    assert "VIRTUAL_ENV/bin/python" in completed.stderr
    assert str(candidate) in completed.stderr
    assert not (tmp_path / "resolver.log").exists()
    assert not (tmp_path / "children.log").exists()


def test_empty_virtualenv_is_not_asserted_and_allows_path_fallback(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    path_bin = tmp_path / "fallback bin"
    _python_wrapper(path_bin / "python3", "path-python3")

    completed = _run(root, tmp_path, VIRTUAL_ENV="", PATH=_test_path(path_bin))

    assert completed.returncode == 0, completed.stderr
    assert set((tmp_path / "resolver.log").read_text().splitlines()) == {"path-python3"}


def test_no_venv_falls_back_to_python3_from_path(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    path_bin = tmp_path / "fallback bin"
    _python_wrapper(path_bin / "python3", "path-python3")

    completed = _run(root, tmp_path, PATH=_test_path(path_bin))

    assert completed.returncode == 0, completed.stderr
    assert set((tmp_path / "resolver.log").read_text().splitlines()) == {"path-python3"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "HYPRIAL_PYTHON is empty"),
        ("missing/python", "HYPRIAL_PYTHON does not exist"),
        ("not-executable", "HYPRIAL_PYTHON is not executable"),
    ],
)
def test_invalid_explicit_python_fails_closed(
    tmp_path: Path, value: str, expected: str
) -> None:
    root = _sandbox(tmp_path)
    selected = value
    if value == "not-executable":
        candidate = tmp_path / value
        candidate.write_text("not executable", encoding="utf-8")
        selected = str(candidate)
    elif value:
        selected = str(tmp_path / value)

    completed = _run(root, tmp_path, HYPRIAL_PYTHON=selected)

    assert completed.returncode != 0
    assert expected in completed.stderr
    assert not (tmp_path / "children.log").exists()


@pytest.mark.parametrize("kind", ("broken", "directory"))
def test_explicit_python_rejects_broken_link_and_directory(
    tmp_path: Path, kind: str
) -> None:
    root = _sandbox(tmp_path)
    candidate = tmp_path / f"explicit {kind}"
    if kind == "broken":
        candidate.symlink_to(tmp_path / "missing target")
    else:
        candidate.mkdir()

    completed = _run(root, tmp_path, HYPRIAL_PYTHON=str(candidate))

    assert completed.returncode != 0
    assert "HYPRIAL_PYTHON" in completed.stderr
    assert str(candidate) in completed.stderr
    assert "broken symlink" in completed.stderr if kind == "broken" else "not a file" in completed.stderr
    assert not (tmp_path / "children.log").exists()


@pytest.mark.parametrize(
    "output",
    ("", "not-hyprial-python:3:12:3"),
    ids=("no-output", "wrong-sentinel"),
)
def test_non_python_executable_is_rejected_by_exact_identity_probe(
    tmp_path: Path, output: str
) -> None:
    root = _sandbox(tmp_path)
    candidate = _non_python_executable(tmp_path / "not-python", output)

    completed = _run(root, tmp_path, HYPRIAL_PYTHON=str(candidate))

    assert completed.returncode != 0
    assert "did not emit the exact Python identity" in completed.stderr
    assert str(candidate) in completed.stderr
    assert not (tmp_path / "children.log").exists()


def test_python_identity_requires_exact_sentinel(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    impostor = _non_python_executable(
        tmp_path / "python impostor", "not-hyprial-python:3:12:3"
    )

    completed = _run(root, tmp_path, HYPRIAL_PYTHON=str(impostor))

    assert completed.returncode != 0
    assert "did not emit the exact Python identity" in completed.stderr
    assert not (tmp_path / "children.log").exists()


def test_python_override_is_never_shell_evaluated(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    injected = tmp_path / "injected"
    value = f"$(touch {injected})"

    completed = _run(root, tmp_path, HYPRIAL_PYTHON=value)

    assert completed.returncode != 0
    assert value in completed.stderr
    assert not injected.exists()
    assert not (tmp_path / "children.log").exists()


@pytest.mark.parametrize("kind", ("broken", "directory", "non-executable"))
def test_repository_python_presence_is_authoritative_and_invalid_fails_closed(
    tmp_path: Path, kind: str
) -> None:
    root = _sandbox(tmp_path)
    candidate = root / ".venv" / "bin" / "python"
    candidate.parent.mkdir(parents=True)
    if kind == "broken":
        candidate.symlink_to(tmp_path / "missing target")
    elif kind == "directory":
        candidate.mkdir()
    else:
        candidate.write_text("not executable", encoding="utf-8")
    path_bin = tmp_path / "fallback bin"
    _python_wrapper(path_bin / "python3", "path-python3")

    completed = _run(root, tmp_path, PATH=_test_path(path_bin))

    assert completed.returncode != 0
    assert "repository .venv/bin/python" in completed.stderr
    assert str(candidate) in completed.stderr
    assert not (tmp_path / "resolver.log").exists()
    assert not (tmp_path / "children.log").exists()


def test_every_contract_child_uses_exact_selected_python_identity(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    selected = Path(sys.executable)

    completed = _run(root, tmp_path, HYPRIAL_PYTHON=str(selected))

    assert completed.returncode == 0, completed.stderr + completed.stdout
    children = (tmp_path / "children.log").read_text().splitlines()
    assert len(children) == 8
    assert {line.rsplit("|", 1)[1] for line in children} == {str(selected)}


def test_too_old_python_reports_required_and_observed_versions(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    old_python = tmp_path / "old-python"
    _write_executable(
        old_python,
        "#!/bin/sh\n"
        "printf '%s\\n' 'hyprial-contract-python-v1:3:11:9'\n",
    )

    completed = _run(root, tmp_path, HYPRIAL_PYTHON=str(old_python))

    assert completed.returncode != 0
    assert "requires Python >=3.12" in completed.stderr
    assert "Python 3.11.9" in completed.stderr


def test_missing_project_dependencies_fail_with_install_command(
    tmp_path: Path, dependency_free_python: Path
) -> None:
    root = _sandbox(tmp_path)

    completed = _run(root, tmp_path, HYPRIAL_PYTHON=str(dependency_free_python))

    assert completed.returncode != 0
    assert "missing required project dependencies" in completed.stderr
    assert "lark_oapi" in completed.stderr
    assert "uv sync --extra test" in completed.stderr
    # Dependency validation fails before any child can run under a bad identity.
    assert not (tmp_path / "children.log").exists()


def test_cli_blackbox_standalone_does_not_require_project_dependencies(
    tmp_path: Path, dependency_free_python: Path
) -> None:
    root = _sandbox(tmp_path)
    child_log = tmp_path / "children.log"
    completed = subprocess.run(
        [str(root / "contract" / "cli-blackbox" / "run.sh")],
        cwd=root,
        env={
            **os.environ,
            "PATH": _test_path(dependency_free_python.parent),
            "HYPRIAL_PYTHON": str(dependency_free_python),
            "HYPRIAL_TEST_CHILD_LOG": str(child_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert len(child_log.read_text().splitlines()) == 1


def test_cli_blackbox_wrapper_can_run_from_a_standalone_copy(
    tmp_path: Path, dependency_free_python: Path
) -> None:
    root = _sandbox(tmp_path, standalone=True)
    child_log = tmp_path / "children.log"

    completed = subprocess.run(
        [str(root / "contract" / "cli-blackbox" / "run.sh")],
        cwd=root,
        env={
            **os.environ,
            "PATH": _test_path(dependency_free_python.parent),
            "HYPRIAL_TEST_CHILD_LOG": str(child_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert len(child_log.read_text().splitlines()) == 1


def test_standalone_cli_blackbox_rejects_empty_override(tmp_path: Path) -> None:
    root = _sandbox(tmp_path, standalone=True)

    completed = subprocess.run(
        [str(root / "contract" / "cli-blackbox" / "run.sh")],
        cwd=root,
        env={**os.environ, "PATH": _test_path(), "HYPRIAL_PYTHON": ""},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "HYPRIAL_PYTHON is empty" in completed.stderr


@pytest.mark.parametrize(
    "output",
    ("", "not-hyprial-python:3:12:3"),
    ids=("no-output", "wrong-sentinel"),
)
def test_standalone_cli_blackbox_rejects_non_python_executable(
    tmp_path: Path, output: str
) -> None:
    root = _sandbox(tmp_path, standalone=True)
    candidate = _non_python_executable(tmp_path / "standalone-not-python", output)

    completed = subprocess.run(
        [str(root / "contract" / "cli-blackbox" / "run.sh")],
        cwd=root,
        env={**os.environ, "PATH": _test_path(), "HYPRIAL_PYTHON": str(candidate)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "did not emit the exact Python identity" in completed.stderr


@pytest.mark.parametrize(
    ("mutation", "test_name", "target_relative"),
    [
        (
            lambda text: text.replace(
                'elif [ -e "$hyprial_repo_root/.venv/bin/python" ] || [ -L "$hyprial_repo_root/.venv/bin/python" ]; then',
                "elif false; then",
                1,
            ),
            "test_bare_run_all_prefers_repo_venv_even_when_path_has_only_system_python",
            "contract/python-resolver.sh",
        ),
        (
            lambda text: text.replace(
                'if [ "${HYPRIAL_PYTHON+x}" = x ]; then', "if false; then", 1
            ),
            "test_explicit_hyprial_python_wins_over_repo_and_virtualenv",
            "contract/python-resolver.sh",
        ),
        (
            lambda text: text.replace(
                'hyprial_require_project_dependencies "$REPO_ROOT"', ":", 1
            ),
            "test_missing_project_dependencies_fail_with_install_command",
            "contract/run-all.sh",
        ),
        (
            lambda text: text.replace(
                ' || [ -L "$hyprial_repo_root/.venv/bin/python" ]', "", 1
            ),
            "test_repository_python_presence_is_authoritative_and_invalid_fails_closed[broken]",
            "contract/python-resolver.sh",
        ),
        (
            lambda text: text.replace(
                'if [ ! -f "$hyprial_python_candidate" ]; then', "if false; then", 1
            ),
            "test_explicit_python_rejects_broken_link_and_directory[directory]",
            "contract/python-resolver.sh",
        ),
        (
            lambda text: text.replace(
                '[ "$1" != hyprial-contract-python-v1 ]', "false", 1
            ),
            "test_python_identity_requires_exact_sentinel",
            "contract/python-resolver.sh",
        ),
        (
            lambda text: text.replace(
                'elif [ -n "${VIRTUAL_ENV:-}" ]; then', "elif false; then", 1
            ),
            "test_asserted_nonempty_virtualenv_is_authoritative_and_invalid_fails_closed[missing]",
            "contract/python-resolver.sh",
        ),
        (
            lambda text: text.replace(
                'if [ "$hyprial_cli_argument_present" = false ] && [ "${HYPRIAL_BIN+x}" != x ]; then',
                "if false; then",
                1,
            ),
            "test_bare_run_all_prefers_repo_cli_over_path_with_restricted_environment",
            "contract/run-all.sh",
        ),
    ],
    ids=(
        "repo-venv-precedence",
        "explicit-override",
        "dependency-gate",
        "repo-cli-precedence",
        "broken-repo-link-fence",
        "regular-file-fence",
        "exact-python-sentinel",
        "asserted-venv-fence",
    ),
)
def test_key_mutations_are_killed(
    tmp_path: Path,
    mutation: Callable[[str], str],
    test_name: str,
    target_relative: str,
) -> None:
    mutant = tmp_path / "mutant"
    shutil.copytree(ROOT / "contract", mutant / "contract")
    shutil.copytree(ROOT / "tests", mutant / "tests")
    target = mutant / target_relative
    original = target.read_text(encoding="utf-8")
    changed = mutation(original)
    assert changed != original
    target.write_text(changed, encoding="utf-8")

    # The semantic test layout is a filesystem path, not a dotted module name.
    # Locate this exact copied consumer so a pytest collection error cannot
    # masquerade as a killed mutation.
    test_file = mutant / Path(__file__).relative_to(ROOT)
    assert test_file.is_file()

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{test_file}::{test_name}",
        ],
        cwd=mutant,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0, f"mutation survived:\n{completed.stdout}"
    assert "1 failed" in completed.stdout
