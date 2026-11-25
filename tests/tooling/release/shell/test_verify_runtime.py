"""shell-6.md §D.4 6c-3: ``verify_runtime.py`` against a fake staged runtime.

The fake ``bin/python3.12`` is a shell script that runs a small Python
program with this test's interpreter. It answers ``version --json`` and
``gui --no-open --json --port 0 --stdin-stop`` the way the real CLI does
(record on line 1, 0600 handoff file, stop on stdin EOF). Its behaviour is
switched by a ``mode`` file next to it, because the verifier gives the
interpreter a scrubbed environment. The isolated roots live under a short
directory in ``/tmp`` (AF_UNIX path limit; pytest's macOS temp is too long).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.tooling.release.shell.support import LOCK, load

VERSION = "0.0.1+gccccccc"

FAKE = r'''
import json, os, sys, time
from pathlib import Path
here = Path(sys.argv[0]).resolve().parent
mode = (here / "mode").read_text().strip() if (here / "mode").exists() else "ok"
args = sys.argv[1:]
assert args[:3] == ["-I", "-m", "hyprial.cli"], args
command = args[3:]
if command == ["version", "--json"]:
    if mode == "hang-version":
        time.sleep(60)
    print(json.dumps({"ok": True, "packageVersion": "VERSION"}))
    raise SystemExit(0)
assert command == ["gui", "--no-open", "--json", "--port", "0", "--stdin-stop"], command
assert os.environ["HYPRIAL_NETWORK_ISOLATED"] == "1"
assert os.environ["HYPRIAL_HOME"].startswith(os.environ["VERIFY_ROOT_HINT"])
if mode == "stray-version2":
    import subprocess
    stray = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    (Path(os.environ["VERIFY_ROOT_HINT"]) / "stray.pid").write_text(str(stray.pid))
runtime = Path(os.environ["HARNESS_STATE_DIR"]) / "gui" / "daemon"
runtime.mkdir(parents=True)
handoff = runtime / "launch.url"
fd = os.open(handoff, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, b"http://127.0.0.1:41234/#grant=abc\n")
os.close(fd)
record = {"ok": True, "record_version": 2 if mode in ("version2", "stray-version2") else 1, "url": "http://127.0.0.1:41234/",
          "host": "127.0.0.1", "port": 41234, "handoff_path": str(handoff), "mode": "normal",
          "backend_driver": "daemon", "frontend_pid": os.getpid(), "backend_pid": None}
if mode == "wide-handoff":
    os.chmod(handoff, 0o644)
print(json.dumps(record), flush=True)
if mode == "ignore-eof":
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
sys.stdin.read()
handoff.unlink(missing_ok=True)
if mode == "write-into-runtime":
    (here.parent / "lib" / "written.pyc").write_bytes(b"x")
raise SystemExit(0)
'''.replace("VERSION", VERSION)


def _runtime(tmp_path: Path, mode: str = "ok") -> Path:
    stage = load("stage_runtime")
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "lib").mkdir()
    (runtime / "bin" / "fake.py").write_text(FAKE, encoding="utf-8")
    interpreter = runtime / "bin" / "python3.12"
    interpreter.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "$(dirname "$0")/fake.py" "$@"\n', encoding="utf-8")
    interpreter.chmod(0o755)
    (runtime / "bin" / "mode").write_text(mode, encoding="utf-8")
    lock = stage.load_lock(LOCK)
    facts = {"file": f"hyprial-{VERSION}-py3-none-any.whl", "version": VERSION, "sha256": "0" * 64, "sourceCommit": "c" * 40}
    manifest = stage.build_manifest(runtime, lock, "aarch64-apple-darwin", facts)
    (runtime / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return runtime


@pytest.fixture()
def parent() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="vrt-", dir="/tmp")).resolve()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _verify(runtime: Path, parent: Path, monkeypatch: pytest.MonkeyPatch, **options: float) -> tuple[int, str, dict]:
    verify = load("verify_runtime")
    monkeypatch.setenv("VERIFY_ROOT_HINT", str(parent))
    monkeypatch.setattr(verify, "PASSTHROUGH", (*verify.PASSTHROUGH, "VERIFY_ROOT_HINT"))
    return verify.run(runtime, root_parent=parent, startup_timeout=options.get("startup", 30.0), stop_timeout=options.get("stop", 20.0), keep=False, version_timeout=options.get("version", 120.0))


def test_a_conforming_runtime_passes_and_leaves_nothing(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, line, evidence = _verify(_runtime(tmp_path), parent, monkeypatch)
    assert (code, line) == (0, "PASS"), evidence
    names = [step["name"] for step in evidence["steps"]]
    assert names == ["manifest", "isolation", "version", "gui-record", "handoff", "stdin-stop", "daemon", "children", "manifest-after"]
    assert list(parent.iterdir()) == []
    assert "grant=abc" not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("mode", "step"),
    [("version2", "gui-record"), ("wide-handoff", "handoff"), ("write-into-runtime", "manifest-after")],
)
def test_contract_breaks_fail(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch, mode: str, step: str) -> None:
    code, line, evidence = _verify(_runtime(tmp_path, mode), parent, monkeypatch)
    assert code == 1, evidence
    assert line.startswith(f"FAIL {step}"), line


def test_a_child_that_ignores_eof_fails_and_is_killed(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, line, evidence = _verify(_runtime(tmp_path, "ignore-eof"), parent, monkeypatch, stop=2.0)
    assert code == 1 and line.startswith("FAIL stdin-stop"), line
    pid = evidence["record"]["frontend_pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_tampered_or_linked_runtime_fails_before_running(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "bin" / "fake.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    code, line, _ = _verify(runtime, parent, monkeypatch)
    assert code == 1 and line.startswith("FAIL manifest"), line

    runtime = _runtime(tmp_path / "second")
    (runtime / "lib" / "alias").symlink_to("../bin")
    code, line, _ = _verify(runtime, parent, monkeypatch)
    assert code == 1 and line.startswith("FAIL manifest") and "symlink" in line, line


def test_isolated_environment_drops_the_callers_hyprial_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verify = load("verify_runtime")
    monkeypatch.setenv("HYPRIAL_HOME", "/real/home")
    monkeypatch.setenv("HARNESS_STATE_DIR", "/real/state")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    env = verify.isolated_environment(tmp_path)
    assert env["HYPRIAL_HOME"] == str(tmp_path / "h")
    assert env["HARNESS_STATE_DIR"] == str(tmp_path / "s")
    assert env["HOME"] == str(tmp_path / "u")
    for key, value in {
        "HYPRIAL_NETWORK_ISOLATED": "1", "HYPRIAL_USAGE_DISABLE": "1", "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_ZENOH_LISTEN": "", "HYPRIAL_ZENOH_CONNECT": "",
    }.items():
        assert env[key] == value, key
    assert env["HYPRIAL_NODE_ID"].startswith("verify-runtime-")
    assert "PYTHONPATH" not in env
    assert "127.0.0.1" in env["NO_PROXY"]
    assert stat.S_IMODE((tmp_path / "h").stat().st_mode) == 0o700


def test_socket_path_over_the_limit_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verify = load("verify_runtime")
    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    code, line, _ = verify.run(_runtime(tmp_path), root_parent=deep, startup_timeout=5.0, stop_timeout=5.0, keep=False)
    assert code == 2 and line.startswith("BLOCKED isolation"), line


def test_carriers_finds_a_process_on_the_root(tmp_path: Path) -> None:
    verify = load("verify_runtime")
    root = tmp_path / "root"
    (root / "u").mkdir(parents=True)
    child = subprocess.Popen(["/bin/sleep", "30"], cwd=root / "u", env={"PATH": "/usr/bin:/bin", "HYPRIAL_HOME": str(root / "h")})
    try:
        deadline = time.monotonic() + 5
        while child.pid not in verify.carriers(root) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert child.pid in verify.carriers(root)
        assert os.getpid() not in verify.carriers(root)
    finally:
        child.kill()
        child.wait()
    assert child.pid not in verify.carriers(root)


def test_carriers_also_finds_a_process_by_its_program_path(tmp_path: Path) -> None:
    verify = load("verify_runtime")
    root = tmp_path / "root"
    root.mkdir()
    program = tmp_path / "runtime" / "bin" / "runner.sh"
    program.parent.mkdir(parents=True)
    program.write_text("/bin/sleep 30\n", encoding="utf-8")
    child = subprocess.Popen(["/bin/sh", str(program)], cwd="/", env={"PATH": "/usr/bin:/bin"})
    try:
        deadline = time.monotonic() + 5
        while child.pid not in verify.carriers(root, command=str(program)) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert child.pid in verify.carriers(root, command=str(program))
        assert child.pid not in verify.carriers(root)
    finally:
        child.kill()
        child.wait()
    assert child.pid not in verify.carriers(root, command=str(program))


def test_a_version_step_that_hangs_is_a_failure_not_blocked(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, line, evidence = _verify(_runtime(tmp_path, "hang-version"), parent, monkeypatch, version=2.0)
    assert code == 1, evidence
    assert line.startswith("FAIL version") and "did not finish" in line, line
    assert list(parent.iterdir()) == []


def test_a_failed_run_stops_processes_in_other_sessions_before_the_root_goes(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verify = load("verify_runtime")
    code, line, evidence = _verify(_runtime(tmp_path, "stray-version2"), parent, monkeypatch)
    assert code == 1 and line.startswith("FAIL gui-record"), line
    stray = int((parent / "stray.pid").read_text())
    assert not verify._alive(stray)
    assert evidence["leftAtExit"] == []
    assert [path.name for path in parent.iterdir()] == ["stray.pid"]


def test_kill_carriers_ends_a_process_that_left_the_cli_session(tmp_path: Path) -> None:
    verify = load("verify_runtime")
    root = tmp_path / "root"
    (root / "u").mkdir(parents=True)
    child = subprocess.Popen(["/bin/sleep", "60"], cwd=root / "u", env={"PATH": "/usr/bin:/bin"}, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while child.pid not in verify.carriers(root) and time.monotonic() < deadline:
            time.sleep(0.1)
        verify.kill_carriers(root, wait=5.0)
        assert child.wait(timeout=5) != 0
    finally:
        child.kill()
        child.wait()


def test_smoke_bundle_shares_the_cleanup_of_the_verifier() -> None:
    smoke = load("smoke_bundle")
    assert not hasattr(smoke, "_kill_all")
    assert callable(smoke.verify.kill_carriers)
