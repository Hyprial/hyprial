"""Tests for `hyprial init` startup-budget behavior.

Since the readiness split, init waits only for the phase-① serving boundary
(daemon.json for our pid, then a ping answer). Named phase progress refreshes
the idle window until the hard cap; restore runs on the daemon's own thread
and is reported through ping's ``phase``/``restorePending`` fields. A timeout
is still a real startup failure with launch-log diagnostics, not a "still
starting" warning.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import os
import random
import re
from datetime import UTC, datetime
from pathlib import Path
import threading
import time
from typing import Any

import pytest

from typer.testing import CliRunner

from hyprial.kernel import DAEMON_LAUNCH_ID_ENV, ipc_errors
# Import the startup-failure dependency before replacing subprocess.Popen.
# The MCP SDK evaluates Popen[bytes] annotations during its first import;
# collecting unrelated MCP tests must not be required for this module to run.
from hyprial.daemon.impl.mcp import channel as mcp_channel
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.daemon_start as _cli_owner_2
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture(autouse=True)
def _stub_supervisor(monkeypatch: Any) -> None:
    """Keep the supervisor out of every init test's call sequence.

    ``init`` now installs the per-user service by default, which would add a
    real ``systemctl``/``launchctl`` probe to each test's observed calls.  Tests
    that care about the supervisor override this fixture explicitly.
    """

    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    monkeypatch.setattr(
        service_module,
        "_run_service",
        lambda operation: {"ok": True, "supervision": "supervised"},
    )


class _IdleRuntime:
    """Just enough runtime for the serve loop's assert; timers never fire
    before the test's stop lands, so ``on_timer`` is never called."""

    def on_timer(self, _observed_at_ms: int | None = None) -> Any:
        raise AssertionError("the test must finish before reconciliation")

    def stop(self) -> None:
        pass


class _LiveProcess:
    """A spawned daemon that is still starting: alive, never ready."""

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _unavailable(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 15.0,
    restore_wait: float | None = None,
) -> Any:
    # PR #332 F4②: the transport seam raises the REGISTERED class for
    # DAEMON_UNAVAILABLE (was a CliError); the stub mirrors the seam.
    del restore_wait
    raise ipc_errors.DaemonUnavailableError("no daemon at the socket")


def _isolate(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HYPRIAL_INIT_READY_TIMEOUT", raising=False)
    # _LiveProcess owns a synthetic pid, not an OS process to inspect.
    monkeypatch.setattr(mcp_channel, "_read_process_identity", lambda _pid: None)
    monkeypatch.setattr(_cli_get_services(), "process_cpu_seconds", lambda _pid: None)


def test_onboarding_dispatch_does_not_initialize(monkeypatch: Any) -> None:
    from hyprial.shell.impl.cli.commands.init import init as init_module

    def unexpected_init() -> None:
        raise AssertionError("onboarding must not run the parent initialization")

    monkeypatch.setattr(init_module, "get_services", unexpected_init)
    for argv, expected_exit in [
        (["init", "onboarding"], 2),
        (["init", "onboarding", "--help"], 0),
        (["init", "onboarding", "plan", "--help"], 0),
    ]:
        result = CliRunner().invoke(_cli_root_app, argv)
        assert result.exit_code == expected_exit, result.exception
        assert "Usage:" in result.output


def test_init_installs_the_supervisor_service_by_default(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # A daemon installed without a supervisor survives until the next reboot and
    # then stays down until a human starts it.  Autostart is therefore the
    # default; ``--no-service`` is the exception, for hosts where a supervisor
    # is someone else's job (containers, CI).
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, *args, **kwargs: (
            {"receivedCount": 0, "candidates": []}
            if method == "org.fetch"
            else {"daemon": {"running": True, "pid": 4242}}
        ),
    )
    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr(
        service_module,
        "_run_service",
        lambda operation: calls.append("install")
        or {"ok": True, "supervision": "supervised"},
    )

    default = CliRunner().invoke(_cli_root_app, ["init", "--json"])
    opted_out = CliRunner().invoke(_cli_root_app, ["init", "--no-service", "--json"])

    assert default.exit_code == 0, default.output
    assert json.loads(default.stdout)["service"]["supervision"] == "supervised"
    assert calls == ["install"]
    assert opted_out.exit_code == 0, opted_out.output
    assert "service" not in json.loads(opted_out.stdout)
    assert calls == ["install"]


def test_init_survives_a_supervisor_it_cannot_install(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # The daemon is already serving by the time init reaches the supervisor, so
    # an uninstallable service is a reported condition, not an init failure.
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, *args, **kwargs: (
            {"receivedCount": 0, "candidates": []}
            if method == "org.fetch"
            else {"daemon": {"running": True, "pid": 4242}}
        ),
    )
    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: False)

    def refuse(operation):  # type: ignore[no-untyped-def]
        raise _cli_support.CliError(
            "SERVICE_MANAGER_FAILED",
            "the per-user systemd bus is not reachable from this session",
        )

    monkeypatch.setattr(service_module, "_run_service", refuse)

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["service"]["ok"] is False
    assert payload["service"]["reason"] == "service-install-failed"


def test_init_creates_org_slot_and_warns_when_context_is_absent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    calls: list[str] = []

    def request(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        calls.append(method)
        if method == "org.fetch":
            return {"receivedCount": 0, "candidates": []}
        return {"daemon": {"running": True}}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    warnings = payload["warnings"]
    assert {item["code"] for item in warnings} == {"ORG_CONTEXT_ABSENT"}
    assert warnings[0]["message"] == "org-context absent"
    assert (tmp_path / "home" / "org" / "pending").is_dir()
    assert calls == ["ping", "org.fetch"]


def test_init_reports_neighbor_candidates_without_adopting(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)

    def request(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if method == "org.fetch":
            return {"receivedCount": 2, "candidates": [{}, {}]}
        return {"daemon": {"running": True}}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["warnings"] == [
        {
            "code": "ORG_CONTEXT_CANDIDATES_RECEIVED",
            "message": "收到 2 个候选,hyprial org context status 查看",
            "candidateCount": 2,
        }
    ]
    assert not (tmp_path / "home" / "org-context.md").exists()


def test_init_ready_timeout_is_configurable_via_flag_and_a_timeout_fails(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The flag still wires through as the idle budget and timeout is failure.

    Exhausting the no-progress window means the process never bound and
    answered -- reported as DAEMON_START_TIMEOUT with the launch summary, not
    as a "still starting" success. The live process is still never killed.
    """

    _isolate(monkeypatch, tmp_path)
    observed: dict[str, Any] = {}

    def spawn(argv: list[str], **kwargs: Any) -> _LiveProcess:
        # The launch summary rides a timeout report too: whatever the daemon
        # mirror-wrote to stderr before going deaf is the evidence.
        kwargs["stdout"].write(b'{"event":"daemon.start.failed"}\n')
        kwargs["stdout"].flush()
        observed["process"] = _LiveProcess(argv, **kwargs)
        return observed["process"]

    def time_out(
        *,
        timeout: float,
        process: Any = None,
        startup_progress: Any = None,
        launch_id: str | None = None,
    ) -> dict[str, Any]:
        observed["timeout"] = timeout
        observed["wait_process"] = process
        observed["startup_progress"] = startup_progress
        log_path = tmp_path / "state" / "logs" / "daemon.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "event": "daemon.start.begin",
                    "phase": "actor-runtime-transport",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        observed["phase_before_timeout"] = startup_progress()
        raise _cli_get_services().CliError(
            "DAEMON_START_TIMEOUT",
            f"daemon did not become ready within {timeout:g}s",
        )

    monkeypatch.setattr(_cli_support, "_daemon_request", _unavailable)
    monkeypatch.setattr(_cli_subprocess, "Popen", spawn)
    monkeypatch.setattr(_cli_get_services(), "_wait_for_daemon", time_out)

    result = CliRunner().invoke(_cli_root_app, ["init", "--ready-timeout", "5", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "DAEMON_START_TIMEOUT"
    assert "stillStarting" not in payload
    data = payload["data"]
    assert data["phase"] == "startup"
    assert data["errorType"] == "servingTimeout"
    assert data["pid"] == 4242
    assert data["logAvailable"] is True
    assert data["daemonEvents"] == ["daemon.start.failed"]
    assert data["lastStartupPhase"] == "actor-runtime-transport"
    assert data["phasesSeen"] == 1
    assert isinstance(data["elapsedSeconds"], float)
    assert data["elapsedSeconds"] >= 0
    assert data["child"] == {
        "alive": True,
        "exitCode": None,
        "cpuSeconds": None,
    }
    process = observed["process"]
    assert observed["timeout"] == 5.0
    assert observed["wait_process"] is process
    assert callable(observed["startup_progress"])
    assert observed["phase_before_timeout"] == {
        "lastStartupPhase": "actor-runtime-transport",
        "phasesSeen": 1,
    }
    assert process.terminated is False
    assert process.killed is False


def test_init_ready_timeout_is_configurable_via_environment(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The env override stays: edge environments tune the ① budget up rather
    than the default absorbing every slow machine."""

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("HYPRIAL_INIT_READY_TIMEOUT", "0.2")
    monkeypatch.setattr(_cli_support, "_daemon_request", _unavailable)
    monkeypatch.setattr(_cli_subprocess, "Popen", _LiveProcess)

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "DAEMON_START_TIMEOUT"


def test_init_ready_timeout_is_the_registered_idle_budget(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The default covers silence during startup, not restore.

    Named phase progress refreshes this default, while the separate hard cap
    bounds total time. Restore still runs on the daemon's own thread and is not
    part of either startup budget.
    """

    _isolate(monkeypatch, tmp_path)
    observed: dict[str, Any] = {}

    def wait_for_daemon(
        *,
        timeout: float,
        process: Any = None,
        startup_progress: Any = None,
        launch_id: str | None = None,
    ) -> dict[str, Any]:
        observed["timeout"] = timeout
        observed["startup_progress"] = startup_progress
        return {"running": True, "pid": 4242}

    monkeypatch.setattr(_cli_support, "_daemon_request", _unavailable)
    monkeypatch.setattr(_cli_subprocess, "Popen", _LiveProcess)
    monkeypatch.setattr(_cli_get_services(), "_wait_for_daemon", wait_for_daemon)

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["started"] is True
    assert payload["running"] is True
    assert observed["timeout"] == _cli_owner_1.DAEMON_START_IDLE_BUDGET_SECONDS
    assert callable(observed["startup_progress"])

    help_result = CliRunner().invoke(_cli_root_app, ["init", "--help"])
    assert help_result.exit_code == 0, help_result.output
    help_text = " ".join(
        re.sub(r"\x1b\[[0-9;]*m", "", help_result.output)
        .replace("│", " ")
        .split()
    )
    assert "30-60s" not in help_text
    assert "restore routinely" not in help_text
    assert "without startup phase progress" in help_text
    assert "two-minute hard cap" in help_text


def test_init_survives_a_capture_alias_the_daemon_keeps_open(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """An OS that cannot delete an open file must not fail a healthy launch.

    The daemon child keeps the per-launch capture open as its stdout; Windows
    then refuses the alias unlink (WinError 32) after the daemon already
    answered readiness (#1902 / #2035).  Removing it is cleanup, not part of
    the launch contract: init succeeds, the compatibility path stays readable,
    and the leftover alias is swept by the next locked launch.
    """

    _isolate(monkeypatch, tmp_path)
    original_unlink = Path.unlink

    def refusing_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name.startswith(".daemon-launch.") and self.suffix == ".log":
            raise PermissionError(32, "The process cannot access the file", str(self))
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    monkeypatch.setattr(_cli_support, "_daemon_request", _unavailable)
    monkeypatch.setattr(_cli_subprocess, "Popen", _LiveProcess)
    monkeypatch.setattr(
        _cli_get_services(),
        "_wait_for_daemon",
        lambda **_kwargs: {"running": True, "pid": 4242},
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["started"] is True
    state = tmp_path / "state"
    assert len(tuple(state.glob(".daemon-launch.*.log"))) == 1
    assert (state / "daemon-launch.log").read_bytes().startswith(
        b"hyprial-launch-boundary:"
    )


def test_locked_launch_sweeps_aliases_left_by_earlier_launches(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Only the serialized launch transaction reclaims an earlier alias."""

    _isolate(monkeypatch, tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    stale = state / ".daemon-launch.earlier.log"
    stale.write_bytes(b"earlier launch\n")
    monkeypatch.setattr(_cli_support, "_daemon_request", _unavailable)
    monkeypatch.setattr(_cli_subprocess, "Popen", _LiveProcess)
    monkeypatch.setattr(
        _cli_get_services(),
        "_wait_for_daemon",
        lambda **_kwargs: {"running": True, "pid": 4242},
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["started"] is True
    assert not stale.exists()
    assert (state / "daemon-launch.log").read_bytes().startswith(
        b"hyprial-launch-boundary:"
    )


def test_capture_helper_never_sweeps_a_concurrent_alias(tmp_path: Path) -> None:
    """The unlocked capture helper leaves other launches' aliases alone."""

    other = tmp_path / ".daemon-launch.other.log"
    other.write_bytes(b"another launcher's live alias\n")
    stream, capture, _marker, _offset = _cli_owner_2._daemon_launch_capture(tmp_path)
    try:
        assert other.exists() and capture.exists()
    finally:
        stream.close()
        capture.unlink(missing_ok=True)


def test_init_still_fails_when_the_daemon_process_dies_during_startup(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)

    class DeadProcess(_LiveProcess):
        returncode = 3

        def poll(self) -> int | None:
            return 3

    monkeypatch.setattr(_cli_support, "_daemon_request", _unavailable)
    monkeypatch.setattr(_cli_subprocess, "Popen", DeadProcess)

    result = CliRunner().invoke(_cli_root_app, ["init", "--ready-timeout", "5", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "DAEMON_START_FAILED"


def test_init_failure_returns_only_finite_structured_diagnostics(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "daemon-launch.log").write_text(
        "stale failure must not be repeated\n", encoding="utf-8"
    )

    class DeadProcess(_LiveProcess):
        returncode = 7

        def poll(self) -> int | None:
            return 7

    def spawn(argv: list[str], **kwargs: Any) -> DeadProcess:
        log_stream = kwargs["stdout"]
        log_stream.write(
            b"startup failed: token secret-value\n"
            b'{"event":"daemon.ready","token":"event-secret"}\n'
            b'{"event":"attacker.secret-value","detail":"other-secret"}\n'
        )
        log_stream.flush()
        return DeadProcess(argv, **kwargs)

    monkeypatch.setattr(_cli_support, "_daemon_request", _unavailable)
    monkeypatch.setattr(_cli_subprocess, "Popen", spawn)

    result = CliRunner().invoke(_cli_root_app, ["init", "--ready-timeout", "5", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "DAEMON_START_FAILED"
    assert payload["error"] == "daemon exited during startup with status 7"
    assert "stale failure" not in payload["error"]
    data = payload["data"]
    assert data.pop("lastStartupPhase") is None
    assert data.pop("phasesSeen") == 0
    assert isinstance(data.pop("elapsedSeconds"), float)
    assert data.pop("child") == {
        "alive": False,
        "exitCode": 7,
        "cpuSeconds": None,
    }
    assert data == {
        "phase": "startup",
        "errorType": "processExit",
        "exitCode": 7,
        "logPath": str(state / "daemon-launch.log"),
        "logAvailable": True,
        "daemonEvents": ["daemon.ready"],
        "recentLog": None,
    }
    serialized = result.stdout
    for secret in ("secret-value", "event-secret", "other-secret"):
        assert secret not in serialized
    assert (state / "daemon-launch.log").stat().st_mode & 0o777 == 0o600
    raw_log = (state / "daemon-launch.log").read_bytes()
    assert b"secret-value" in raw_log
    assert b"event-secret" in raw_log
    assert not tuple(state.glob(".daemon-launch.*.log"))


def _capture(tmp_path: Path) -> tuple[Any, Path, bytes, int]:
    tmp_path.mkdir(exist_ok=True)
    return _cli_owner_2._daemon_launch_capture(tmp_path)


def test_launch_summary_keeps_its_inode_when_compatibility_path_rotates(
    tmp_path: Path,
) -> None:
    stream, capture, marker, offset = _capture(tmp_path)
    try:
        stream.write(b"owned by this launch\n")
        replacement = tmp_path / "replacement"
        replacement.write_text("other launch\n", encoding="utf-8")
        os.replace(replacement, tmp_path / "daemon-launch.log")

        assert _cli_owner_2._daemon_launch_log_summary(stream, marker=marker, offset=offset) == {
            "logAvailable": True,
            "daemonEvents": [],
            "recentLog": None,
        }
    finally:
        stream.close()
        capture.unlink(missing_ok=True)


def test_launch_summary_fails_closed_after_truncate(tmp_path: Path) -> None:
    stream, capture, marker, offset = _capture(tmp_path)
    try:
        stream.write(b"real failure\n")
        stream.truncate(0)
        stream.seek(0)
        stream.write(b"forged replacement\n")

        assert _cli_owner_2._daemon_launch_log_summary(stream, marker=marker, offset=offset) == {
            "logAvailable": False,
            "daemonEvents": [],
            "recentLog": None,
        }
    finally:
        stream.close()
        capture.unlink(missing_ok=True)


def test_launch_captures_do_not_mix_concurrent_inodes(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)
    captures: dict[str, tuple[Any, Path, bytes, int]] = {}

    def create(label: str) -> None:
        barrier.wait()
        captures[label] = _capture(tmp_path)

    threads = [threading.Thread(target=create, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    first = captures["a"]
    second = captures["b"]
    try:
        first[0].write(b"first-only\n")
        second[0].write(b"second-only\n")

        assert (
            _cli_owner_2._daemon_launch_log_summary(first[0], marker=first[2], offset=first[3])[
                "logAvailable"
            ]
            is True
        )
        assert (
            _cli_owner_2._daemon_launch_log_summary(
                second[0], marker=second[2], offset=second[3]
            )["logAvailable"]
            is True
        )
    finally:
        for stream, capture, _marker, _offset in (first, second):
            stream.close()
            capture.unlink(missing_ok=True)


def test_launch_summary_never_echoes_arbitrary_stderr(
    tmp_path: Path,
) -> None:
    stream, capture, marker, offset = _capture(tmp_path)
    try:
        generator = random.Random(20260813)
        arbitrary = bytes(generator.randrange(256) for _ in range(64 * 1024))
        stream.write(arbitrary)
        stream.write(b'\n{"event":"daemon.ready","payload":"must-not-echo"}\n')
        stream.write(b'{"event":"daemon.stopping","nested":{"secret":"leak"}}\n')
        stream.write(b'{"event":"unknown","value":"unknown-leak"}\n')

        summary = _cli_owner_2._daemon_launch_log_summary(stream, marker=marker, offset=offset)
        assert summary == {
            "logAvailable": True,
            "daemonEvents": ["daemon.ready", "daemon.stopping"],
            "recentLog": None,
        }
        serialized = json.dumps(summary)
        assert "must-not-echo" not in serialized
        assert "unknown-leak" not in serialized
        assert "leak" not in serialized
    finally:
        stream.close()
        capture.unlink(missing_ok=True)


def test_init_returns_before_restore_completes(
    monkeypatch: Any,
) -> None:
    """Init waits for the serving boundary, not for restore.

    The daemon child is an in-process thread (only ``Popen`` is doubled, with
    this process's pid so the daemon.json fence passes), its adapter restore
    is blocked, and ``hyprial init`` must still return ready -- ready means
    "socket bound, accept running, ping answers", and restore progress is
    reported as ``phase``/``restorePending`` rather than waited on.  Before
    the readiness split this wait outlived every restore by construction.
    """

    import shutil
    import tempfile

    from hyprial.daemon.impl.application import DaemonApplication

    # AF_UNIX paths are length-limited and pytest's tmp_path exceeds the
    # limit on this host, so the state root lives under /tmp.
    root = Path(tempfile.mkdtemp(prefix="hyprial-init-gate-", dir=hyprial_test_tmp_root()))
    state = root / "state"
    home = root / "home"
    state.mkdir(parents=True)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("HYPRIAL_INIT_READY_TIMEOUT", raising=False)

    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="init-gate-test",
        hyprial_home=home,
    **_deep_module_daemon_dependencies())
    monkeypatch.setattr(
        app, "_start_runtime", lambda: setattr(app, "_runtime", _IdleRuntime())
    )
    restore_started = threading.Event()
    release_restore = threading.Event()
    restore_finished = threading.Event()

    def blocked_restore() -> None:
        restore_started.set()
        while not release_restore.is_set() and not app.stop_event.is_set():
            time.sleep(0.01)
        restore_finished.set()

    monkeypatch.setattr(app, "_restore_adapters", blocked_restore)

    daemon: threading.Thread | None = None

    class ThreadProcess:
        """The ``Popen`` double whose child is the in-process daemon thread."""

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            nonlocal daemon
            # ``daemon run`` hands the launcher's nonce to the runtime.
            launch_id = kwargs["env"][DAEMON_LAUNCH_ID_ENV]
            daemon = threading.Thread(
                target=app.run, kwargs={"launch_id": launch_id}, daemon=True
            )
            daemon.start()

        pid = os.getpid()
        returncode: int | None = None

        def poll(self) -> int | None:
            # A daemon thread that died before answering is a dead child:
            # reporting it lets the wait fail instead of timing out mutely.
            if daemon is not None and not daemon.is_alive():
                return 0
            return None

    monkeypatch.setattr(_cli_subprocess, "Popen", ThreadProcess)
    try:
        result = CliRunner().invoke(_cli_root_app, ["init", "--ready-timeout", "5", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["started"] is True
        assert payload["phase"] == "restoring"
        assert payload["restorePending"] is True
        assert restore_started.is_set()
        assert not restore_finished.is_set(), (
            "init waited for restore; the serving boundary is ping, not "
            "connector recovery"
        )
    finally:
        release_restore.set()
        app.stop_event.set()
        if daemon is not None:
            daemon.join(timeout=10.0)
        shutil.rmtree(root, ignore_errors=True)


def test_init_latency_independent_of_fleet_size(
    monkeypatch: Any,
) -> None:
    """A 32-connector restore in flight does not stretch init's wait.

    The deleted wall clock earned its keep by scaling with the fleet (rounds
    x start timeout), which put fleet size on the startup path.  Here every
    one of 32 starts parks until released, so the restore provably cannot
    complete inside the test: if init's readiness wait regains ANY dependency
    on restore progress, this test hangs red instead of passing slowly.
    """

    import shutil
    import tempfile

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime

    fleet = 32
    root = Path(tempfile.mkdtemp(prefix="hyprial-init-fleet-", dir=hyprial_test_tmp_root()))
    state = root / "state"
    home = root / "home"
    state.mkdir(parents=True)
    (state / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [
                    {
                        "provider": "codex",
                        "name": f"c{index:02d}",
                        "headless": True,
                        "args": [],
                        "ownership": "managed",
                    }
                    for index in range(fleet)
                ],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("HYPRIAL_INIT_READY_TIMEOUT", raising=False)

    release = threading.Event()

    class _BlockingLauncher:
        """Healthy but parked: every start blocks until the test releases it."""

        def __init__(self) -> None:
            self.contacted: list[str] = []
            self._lock = threading.Lock()

        def start(self, spec: Any) -> Any:
            with self._lock:
                self.contacted.append(spec.name)
            while not release.is_set():
                time.sleep(0.01)
            return _ParkedProcess()

    class _ParkedProcess:
        running = True
        pid: int | None = None

        def stop(self) -> None:
            pass

    launcher = _BlockingLauncher()
    managed = ManagedHarnessRuntime(launcher, start_timeout_seconds=30.0)

    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="init-fleet-test",
        hyprial_home=home,
    **_deep_module_daemon_dependencies())
    app._harnesses = managed  # type: ignore[assignment]
    monkeypatch.setattr(
        app, "_start_runtime", lambda: setattr(app, "_runtime", _IdleRuntime())
    )
    monkeypatch.setattr(app, "_restore_adapters", lambda: None)

    daemon: threading.Thread | None = None

    class ThreadProcess:
        """The ``Popen`` double whose child is the in-process daemon thread."""

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            nonlocal daemon
            # ``daemon run`` hands the launcher's nonce to the runtime.
            launch_id = kwargs["env"][DAEMON_LAUNCH_ID_ENV]
            daemon = threading.Thread(
                target=app.run, kwargs={"launch_id": launch_id}, daemon=True
            )
            daemon.start()

        pid = os.getpid()
        returncode: int | None = None

        def poll(self) -> int | None:
            if daemon is not None and not daemon.is_alive():
                return 0
            return None

    monkeypatch.setattr(_cli_subprocess, "Popen", ThreadProcess)
    try:
        started = time.monotonic()
        # No --ready-timeout: the default budget applies, so a restore-sized
        # default (the old 90s) would hang this test out to a failure.
        result = CliRunner().invoke(_cli_root_app, ["init", "--json"])
        elapsed = time.monotonic() - started

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["started"] is True
        assert payload["phase"] == "restoring"
        assert payload["restorePending"] is True
        assert elapsed < 5.0, (
            f"init took {elapsed:.1f}s against a parked {fleet}-connector "
            "restore; the wait is drifting back toward restore"
        )

        # The fleet really is mid-restore behind that answer: starts parked at
        # the admission width, the rest queued, the gate never opened.
        deadline = time.monotonic() + 5.0
        while len(launcher.contacted) < managed._actor.start_admission_width:
            assert time.monotonic() < deadline, "restore never engaged the fleet"
            time.sleep(0.02)
        assert not app._restore_done.is_set()
    finally:
        app.stop_event.set()
        release.set()
        if daemon is not None:
            daemon.join(timeout=10.0)
        else:
            managed.stop(2.0)
        shutil.rmtree(root, ignore_errors=True)
