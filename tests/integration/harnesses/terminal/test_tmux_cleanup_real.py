"""Real-tmux proof for detached launch resource ownership."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable

import pytest

from hyprial.daemon.impl.harnesses.runtime import tmux as tmux_mod
from hyprial.daemon.impl.harnesses.runtime._launch_cleanup import skill_plugin_path


REAL_TMUX = shutil.which("tmux")
_SESSION_SURVIVAL_OBSERVATION_SECONDS = 1.5
_SLOW_CLEAN_EXIT_SECONDS = 3.0
_SLOW_CLEAN_EXIT_OBSERVATION_SECONDS = 5.0


def _run(
    wrapper: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(wrapper), *args], text=True, capture_output=True, check=check
    )


def _waiting_pane(wrapper: Path, session: str, gate: Path) -> str:
    child = (
        "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
        "\nwhile not p.exists(): time.sleep(0.02)"
    )
    _run(
        wrapper,
        "new-session",
        "-d",
        "-s",
        session,
        "--",
        sys.executable,
        "-c",
        child,
        str(gate),
    )
    return _run(wrapper, "display-message", "-p", "-t", session, "#{pane_id}").stdout.strip()


def _wait_until(predicate: Callable[[], bool], timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("real tmux condition did not become true before timeout")


def _isolated_tmux(tmp_path: Path, suffix: str) -> Path:
    assert REAL_TMUX is not None
    wrapper = tmp_path / f"tmux-{suffix}"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(REAL_TMUX)} "
        f"-L {shlex.quote(f'hyprial-mcprev4-{suffix}-{os.getpid()}')} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper


def _launch_paths(tmp_path: Path, label: str) -> tuple[Path, Path, Path]:
    config = tmp_path / f"{label}.json"
    recovery = config.with_suffix(".recover")
    skills = skill_plugin_path(config)
    config.write_text("{}", encoding="utf-8")
    recovery.write_text("recovery", encoding="utf-8")
    skills.mkdir()
    (skills / "payload").write_text("skill", encoding="utf-8")
    return config, recovery, skills


def _cooked_sigint_child() -> str:
    return (
        "import os,pathlib,signal,sys,termios,time; "
        "started=pathlib.Path(sys.argv[1]); "
        "observed=pathlib.Path(sys.argv[2]); "
        "stop=pathlib.Path(sys.argv[3]); "
        "topology=pathlib.Path(sys.argv[4]); "
        "attrs=termios.tcgetattr(sys.stdin.fileno()); "
        "attrs[3] |= termios.ICANON | termios.ISIG; "
        "termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs); "
        "signal.signal(signal.SIGINT, lambda *_: "
        "observed.write_text('TUI-survived-sigint', encoding='utf-8')); "
        "topology.write_text(f'{os.getpid()} {os.getppid()} {os.getpgrp()} "
        "{os.tcgetpgrp(0)}', encoding='utf-8'); "
        "started.touch(); "
        "\nwhile not stop.exists(): time.sleep(0.02)"
    )


def _default_signal_python(tmp_path: Path, real_python: str) -> Path:
    """Exec Python after undoing a launcher's inherited SIGINT ignore."""

    launcher = tmp_path / "python-default-signals"
    launcher.write_text(
        f"#!{real_python}\n"
        "import os, signal, sys\n"
        "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
        f"os.execv({real_python!r}, [{real_python!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    return launcher


def _wait_for_paths_to_disappear(paths: tuple[Path, ...]) -> None:
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and any(path.exists() for path in paths):
        time.sleep(0.05)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(REAL_TMUX is None, reason="real tmux binary is unavailable")
def test_real_tmux_ctrl_c_reaches_child_without_destroying_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A/B the production wrapper against execvpe in one cooked-mode tty."""

    wrapper = _isolated_tmux(tmp_path, "ctrl-c")
    real_python = sys.executable
    # Detached tmux can inherit SIGINT ignored on some hosts. Resetting it in
    # the exec launcher makes the reachable default-disposition case explicit
    # instead of letting environment inheritance turn this regression green.
    monkeypatch.setattr(
        tmux_mod.sys,
        "executable",
        str(_default_signal_python(tmp_path, real_python)),
    )
    observations: dict[str, tuple[bool, str]] = {}
    try:
        for label, owns_cleanup in (("withwrap", True), ("execvpe", False)):
            started = tmp_path / f"{label}-started"
            observed = tmp_path / f"{label}-sigint"
            stop = tmp_path / f"{label}-stop"
            topology = tmp_path / f"{label}-topology"
            cleanup = None
            if owns_cleanup:
                config, recovery, _skills = _launch_paths(tmp_path, label)
                cleanup = tmux_mod.LaunchCleanupResources(config, recovery)
            tmux_mod.new_detached_session(
                str(wrapper),
                label,
                [
                    real_python,
                    "-c",
                    _cooked_sigint_child(),
                    str(started),
                    str(observed),
                    str(stop),
                    str(topology),
                ],
                cwd=tmp_path,
                env={},
                cleanup=cleanup,
            )
            _wait_until(started.exists)
            _wait_until(topology.exists)
            child_pid, parent_pid, child_pgid, foreground_pgid = (
                int(value) for value in topology.read_text(encoding="utf-8").split()
            )
            pane_pid = int(
                _run(
                    wrapper, "display-message", "-p", "-t", label, "#{pane_pid}"
                ).stdout.strip()
            )
            if owns_cleanup:
                assert child_pid != pane_pid
                assert parent_pid == pane_pid
                assert child_pgid == foreground_pgid == pane_pid
            else:
                assert child_pid == pane_pid
                assert child_pgid == foreground_pgid == pane_pid
            _run(wrapper, "send-keys", "-t", label, "C-c")
            _wait_until(observed.exists)
            # R2 died 0.5-0.9s after the child marker. Observing at 1.5s stays
            # outside that measured boundary; the whole-file R2 rollback must
            # fail this assertion three runs out of three.
            time.sleep(_SESSION_SURVIVAL_OBSERVATION_SECONDS)
            observations[label] = (
                _run(wrapper, "has-session", "-t", label, check=False).returncode
                == 0,
                observed.read_text(encoding="utf-8"),
            )
            stop.touch()
            if observations[label][0]:
                _wait_until(
                    lambda label=label: _run(
                        wrapper, "has-session", "-t", label, check=False
                    ).returncode
                    != 0
                )
            _wait_until(lambda child_pid=child_pid: not _process_exists(child_pid))

        assert observations == {
            "withwrap": (True, "TUI-survived-sigint"),
            "execvpe": (True, "TUI-survived-sigint"),
        }
    finally:
        _run(wrapper, "kill-server", check=False)


@pytest.mark.skipif(REAL_TMUX is None, reason="real tmux binary is unavailable")
@pytest.mark.parametrize("termination", ("kill-session", "kill-pane"))
def test_real_tmux_forced_pane_teardown_releases_all_launch_resources(
    tmp_path: Path, termination: str
) -> None:
    wrapper = _isolated_tmux(tmp_path, termination)
    session = f"product-{termination}"
    config, recovery, skills = _launch_paths(tmp_path, termination)
    started = tmp_path / f"{termination}-started"
    child_pid_path = tmp_path / f"{termination}-child-pid"
    child_signal_path = tmp_path / f"{termination}-child-signal"
    child = (
        "import os,pathlib,signal,sys,time; pathlib.Path(sys.argv[1]).touch(); "
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()), encoding='utf-8'); "
        "observed=pathlib.Path(sys.argv[3]); "
        "handler=lambda signum,*_: observed.write_text(str(signum), encoding='utf-8'); "
        "signal.signal(signal.SIGHUP, handler); signal.signal(signal.SIGTERM, handler); "
        "\nwhile True: time.sleep(1)"
    )
    child_pid: int | None = None
    try:
        tmux_mod.new_detached_session(
            str(wrapper),
            session,
            [
                sys.executable,
                "-c",
                child,
                str(started),
                str(child_pid_path),
                str(child_signal_path),
            ],
            cwd=tmp_path,
            env={"HYPRIAL_TMUX_CHILD_SIGNAL_GRACE_SECONDS": "0.25"},
            cleanup=tmux_mod.LaunchCleanupResources(config, recovery),
        )
        _wait_until(started.exists)
        _wait_until(child_pid_path.exists)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        if termination == "kill-session":
            _run(wrapper, "kill-session", "-t", session)
        else:
            pane = _run(
                wrapper, "display-message", "-p", "-t", session, "#{pane_id}"
            ).stdout.strip()
            _run(wrapper, "kill-pane", "-t", pane)
        _wait_for_paths_to_disappear((config, recovery, skills))
        assert not config.exists()
        assert not recovery.exists()
        assert not skills.exists()
        _wait_until(lambda: not _process_exists(child_pid))
        assert child_signal_path.exists()
    finally:
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
        _run(wrapper, "kill-server", check=False)


@pytest.mark.skipif(REAL_TMUX is None, reason="real tmux binary is unavailable")
def test_real_tmux_default_grace_preserves_slow_clean_exit(tmp_path: Path) -> None:
    """The default must outlive a 3s clean exit; zero grace makes this red."""

    wrapper = _isolated_tmux(tmp_path, "slow-clean-exit")
    observations: dict[str, bool] = {}
    child_pids: list[int] = []
    try:
        for label, owns_cleanup in (("withwrap", True), ("execvpe", False)):
            started = tmp_path / f"{label}-slow-started"
            clean_exit = tmp_path / f"{label}-clean-exit"
            child_pid_path = tmp_path / f"{label}-slow-pid"
            cleanup = None
            owned_paths: tuple[Path, ...] = ()
            if owns_cleanup:
                config, recovery, skills = _launch_paths(tmp_path, f"{label}-slow")
                cleanup = tmux_mod.LaunchCleanupResources(config, recovery)
                owned_paths = (config, recovery, skills)
            child = (
                "import os,pathlib,signal,sys,time; "
                "started=pathlib.Path(sys.argv[1]); "
                "clean=pathlib.Path(sys.argv[2]); "
                "pathlib.Path(sys.argv[3]).write_text(str(os.getpid()), encoding='utf-8'); "
                f"delay={_SLOW_CLEAN_EXIT_SECONDS!r}; "
                "handler=lambda *_: (time.sleep(delay), clean.touch(), sys.exit(0)); "
                "signal.signal(signal.SIGHUP, handler); signal.signal(signal.SIGTERM, handler); "
                "started.touch(); "
                "\nwhile True: time.sleep(1)"
            )
            tmux_mod.new_detached_session(
                str(wrapper),
                label,
                [
                    sys.executable,
                    "-c",
                    child,
                    str(started),
                    str(clean_exit),
                    str(child_pid_path),
                ],
                cwd=tmp_path,
                env={},
                cleanup=cleanup,
            )
            _wait_until(started.exists)
            _wait_until(child_pid_path.exists)
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            child_pids.append(child_pid)
            _run(wrapper, "kill-session", "-t", label)
            deadline = time.monotonic() + _SLOW_CLEAN_EXIT_OBSERVATION_SECONDS
            while time.monotonic() < deadline and not clean_exit.exists():
                time.sleep(0.05)
            observations[label] = clean_exit.exists()
            if owned_paths:
                _wait_for_paths_to_disappear(owned_paths)
                assert not any(path.exists() for path in owned_paths)
            _wait_until(lambda child_pid=child_pid: not _process_exists(child_pid))

        assert observations == {"withwrap": True, "execvpe": True}
    finally:
        for child_pid in child_pids:
            if _process_exists(child_pid):
                os.kill(child_pid, signal.SIGKILL)
        _run(wrapper, "kill-server", check=False)


@pytest.mark.skipif(REAL_TMUX is None, reason="real tmux binary is unavailable")
def test_real_tmux_pane_exit_releases_all_launch_resources(tmp_path: Path) -> None:
    """Positive control first, then the production single-pane lifecycle."""

    assert REAL_TMUX is not None
    socket_name = f"hyprial-mcprev2-{os.getpid()}"
    wrapper = tmp_path / "tmux-isolated"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(REAL_TMUX)} -L {shlex.quote(socket_name)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    positive = "positive-control"
    product = "product-single-pane"
    keeper = "server-keeper"
    positive_gate = tmp_path / "positive-exit"
    product_gate = tmp_path / "product-exit"
    marker = tmp_path / "positive-hook-pane"

    config = tmp_path / "claude-channel-live.json"
    recovery = config.with_suffix(".recover")
    skills = skill_plugin_path(config)

    try:
        _run(
            wrapper,
            "new-session",
            "-d",
            "-s",
            keeper,
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
        )
        # Positive control: the global session hook must observe the exact
        # production-shaped session close while the keeper holds the server.
        marker_command = (
            f"printf '%s' '#{{hook_session_name}}' > {shlex.quote(str(marker))}"
        )
        _run(
            wrapper,
            "set-hook",
            "-g",
            "session-closed[42]",
            f"run-shell {shlex.quote(marker_command)}",
        )
        _waiting_pane(wrapper, positive, positive_gate)
        positive_gate.touch()
        _wait_until(
            lambda: marker.exists()
            and marker.read_text(encoding="utf-8") == positive
        )
        assert _run(wrapper, "has-session", "-t", positive, check=False).returncode != 0
        _run(wrapper, "set-hook", "-gu", "session-closed[42]", check=False)

        # Product shape: one window, one pane, production launcher, real exit.
        config.write_text("{}", encoding="utf-8")
        recovery.write_text("recovery", encoding="utf-8")
        skills.mkdir()
        (skills / "payload").write_text("skill", encoding="utf-8")
        started = tmp_path / "product-started"
        child = (
            "import pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).touch(); "
            "gate=pathlib.Path(sys.argv[2]); "
            "\nwhile not gate.exists(): time.sleep(0.02)"
        )
        tmux_mod.new_detached_session(
            str(wrapper),
            product,
            [sys.executable, "-c", child, str(started), str(product_gate)],
            cwd=tmp_path,
            env={"HYPRIAL_REAL_TMUX_PROBE": "1"},
            cleanup=tmux_mod.LaunchCleanupResources(config, recovery),
        )
        _wait_until(started.exists)
        assert _run(wrapper, "has-session", "-t", product).returncode == 0
        assert config.exists() and recovery.exists() and skills.exists()

        # Leave the product pane as the server's last session, matching the
        # production single-pane detached shape that exposed the old leak.
        _run(wrapper, "kill-session", "-t", keeper)
        product_gate.touch()
        _wait_until(
            lambda: _run(
                wrapper, "has-session", "-t", product, check=False
            ).returncode
            != 0
        )
        assert not config.exists()
        assert not recovery.exists()
        assert not skills.exists()
    finally:
        _run(wrapper, "kill-server", check=False)
