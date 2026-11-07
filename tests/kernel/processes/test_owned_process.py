from __future__ import annotations

import ast
import errno
import os
import signal
import subprocess
import sys

import pytest

import hyprial.kernel.impl.processes.owned_process as owned_process
from hyprial.kernel import OwnedProcessGroup, ProcessLivenessState


_DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin", reason="native libproc birth identity is Darwin-only"
)


def _spawn_sleeping_group() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )


def _reap_group(worker: subprocess.Popen[bytes]) -> None:
    if worker.poll() is None:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    worker.wait(timeout=2.0)


def _require_darwin_process_probe() -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())],
            check=False,
            capture_output=True,
            text=True,
        )
    except PermissionError as error:
        if error.errno == errno.EPERM:
            pytest.skip("sandbox denies the macOS ps birth-identity probe")
        raise


def _ps_birth_identity(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        check=True,
        capture_output=True,
        text=True,
        timeout=1.0,
    )
    return f"ps:{result.stdout.strip()}"


_LOCALE_PROBE = """
import subprocess, sys
from hyprial.kernel.impl.processes import owned_process as o
child = subprocess.Popen(["sleep", "5"])
try:
    native = o._darwin_process_birth_identity(child.pid)
    ps = o._ps_process_birth_identity(child.pid)
    print(repr((native, ps)))
finally:
    child.kill()
    child.wait()
"""


@_DARWIN_ONLY
@pytest.mark.parametrize(
    ("lc_all", "tz"),
    [
        ("C", None),
        ("en_US.UTF-8", None),
        ("zh_CN.UTF-8", None),
        ("fr_FR.UTF-8", None),
        ("ja_JP.UTF-8", None),
        ("C", "Asia/Tokyo"),
    ],
)
def test_darwin_native_marker_equals_ps_under_the_environment_locale(
    lc_all: str, tz: str | None
) -> None:
    """``ps lstart`` is locale- and TZ-dependent; old markers must still match."""

    _require_darwin_process_probe()
    env = {**os.environ, "LC_ALL": lc_all, "PYTHONPATH": os.pathsep.join(sys.path)}
    if tz is not None:
        env["TZ"] = tz
    result = subprocess.run(
        [sys.executable, "-c", _LOCALE_PROBE],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=10.0,
    )
    native, ps = ast.literal_eval(result.stdout.strip())
    assert ps is not None and ps.startswith("ps:")
    assert native == ps


@_DARWIN_ONLY
def test_marker_persisted_by_the_old_ps_path_verifies_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade round trip: an old daemon's ps marker, a new daemon's check."""

    _require_darwin_process_probe()
    worker = _spawn_sleeping_group()
    group = OwnedProcessGroup(label="round-trip")
    try:
        old_marker = owned_process._ps_process_birth_identity(worker.pid)
        assert old_marker is not None
        group._observed = True
        group._pid = worker.pid
        group._identity = old_marker
        spawned: list[object] = []
        monkeypatch.setattr(
            owned_process.subprocess, "run",
            lambda *a, **k: spawned.append(a) or pytest.fail("no ps fork"),
        )
        assert owned_process.process_birth_identity(worker.pid) == old_marker
        assert group.liveness().state is ProcessLivenessState.ALIVE
        assert spawned == []
    finally:
        _reap_group(worker)


@_DARWIN_ONLY
@pytest.mark.parametrize("native", ["works", "raises"])
def test_exited_group_reads_dead_never_unknown(
    monkeypatch: pytest.MonkeyPatch, native: str
) -> None:
    worker = _spawn_sleeping_group()
    group = OwnedProcessGroup(label="exited")
    group._observed = True
    group._pid = worker.pid
    group._identity = "ps:birth-stamp"
    if native == "raises":
        def failing(_pid: int) -> str | None:
            raise OSError("native read failed")

        monkeypatch.setattr(owned_process, "_darwin_process_birth_identity", failing)
    _reap_group(worker)
    assert owned_process._darwin_process_birth_identity is not None
    assert group.liveness().state is ProcessLivenessState.DEAD
    if native == "works":
        assert owned_process.process_birth_identity(worker.pid) is None


@_DARWIN_ONLY
@pytest.mark.parametrize("target", ["self", "child"])
def test_darwin_native_birth_identity_matches_ps_without_forking(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    child = subprocess.Popen(["sleep", "5"]) if target == "child" else None
    pid = os.getpid() if child is None else child.pid
    try:
        expected = _ps_birth_identity(pid)
        subprocess_calls: list[str] = []
        original_run = subprocess.run

        def counted_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            subprocess_calls.append("run")
            return original_run(*args, **kwargs)

        monkeypatch.setattr(owned_process.subprocess, "run", counted_run)

        assert owned_process.process_birth_identity(pid) == expected
        assert subprocess_calls == []
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=2.0)


@_DARWIN_ONLY
def test_darwin_liveness_does_not_fork_for_twenty_live_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = OwnedProcessGroup(label="fork-count")
    worker = _spawn_sleeping_group()
    try:
        group._observed = True
        group._pid = worker.pid
        group._identity = "ps:birth-stamp"
        calls: list[str] = []

        def counted_run(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append("run")
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout="birth-stamp\n"
            )

        def counted_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            calls.append("Popen")
            raise AssertionError("liveness must not spawn a process")

        monkeypatch.setattr(
            owned_process,
            "_darwin_process_birth_identity",
            lambda _pid: "ps:birth-stamp",
        )
        monkeypatch.setattr(owned_process.subprocess, "run", counted_run)
        monkeypatch.setattr(owned_process.subprocess, "Popen", counted_popen)

        for _ in range(20):
            assert group.liveness().state is ProcessLivenessState.ALIVE

        assert calls == []
    finally:
        _reap_group(worker)


@_DARWIN_ONLY
@pytest.mark.parametrize("failure", [None, OSError("libproc denied")])
def test_darwin_birth_identity_falls_back_to_identical_ps_marker(
    monkeypatch: pytest.MonkeyPatch, failure: OSError | None
) -> None:
    pid = os.getpid()
    expected = "ps:Tue Oct  6 18:21:33 2026"
    native_calls: list[int] = []
    ps_calls: list[int] = []

    def failed_native(observed_pid: int) -> str | None:
        native_calls.append(observed_pid)
        if failure is not None:
            raise failure
        return None

    def counted_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        assert isinstance(command, list)
        ps_calls.append(int(command[-1]))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="Tue Oct  6 18:21:33 2026\n",
        )

    monkeypatch.setattr(
        owned_process,
        "_darwin_process_birth_identity",
        failed_native,
        raising=False,
    )
    monkeypatch.setattr(owned_process.subprocess, "run", counted_run)

    assert owned_process.process_birth_identity(pid) == expected
    assert native_calls == [pid]
    assert ps_calls == [pid]


def test_register_identity_failure_reclaims_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = OwnedProcessGroup(label="Codex app-server")
    worker = _spawn_sleeping_group()
    monkeypatch.setattr(owned_process, "process_birth_identity", lambda _pid: None)
    try:
        with pytest.raises(ConnectionError) as raised:
            group.register(worker.pid)

        assert str(raised.value) == "cannot establish Codex app-server process identity"
        worker.wait(timeout=2.0)
        group.release_if_gone(worker.pid)
        assert group.stopped()
        with pytest.raises(ProcessLookupError):
            os.killpg(worker.pid, 0)
    finally:
        _reap_group(worker)


def test_register_after_stop_uses_label_and_reclaims_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = OwnedProcessGroup(label="Codex app-server")
    group.force_close()
    worker = _spawn_sleeping_group()
    monkeypatch.setattr(
        owned_process,
        "process_birth_identity",
        lambda _pid: "birth-original",
    )
    try:
        with pytest.raises(ConnectionError) as raised:
            group.register(worker.pid)

        assert str(raised.value) == "Codex app-server process is stopping"
        worker.wait(timeout=2.0)
        group.release_if_gone(worker.pid)
        assert group.stopped()
        with pytest.raises(ProcessLookupError):
            os.killpg(worker.pid, 0)
    finally:
        _reap_group(worker)


def test_signal_refuses_reused_process_group_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = OwnedProcessGroup(label="test worker")
    identities = iter(("birth-original", "birth-reused"))
    monkeypatch.setattr(
        owned_process,
        "process_birth_identity",
        lambda _pid: next(identities),
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        owned_process.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )
    group.register(4242)

    group.signal(4242, signal.SIGTERM)

    assert signals == [(4242, 0)]


def test_spawn_register_signal_and_release_process_group() -> None:
    _require_darwin_process_probe()
    group = OwnedProcessGroup(label="test worker")
    worker = _spawn_sleeping_group()
    try:
        group.register(worker.pid)
        assert group.exists(worker.pid)

        group.signal(worker.pid, signal.SIGTERM)
        assert worker.wait(timeout=2.0) == -signal.SIGTERM
        group.release_if_gone(worker.pid)

        assert group.stopped()
        with pytest.raises(ProcessLookupError):
            os.killpg(worker.pid, 0)
    finally:
        _reap_group(worker)
