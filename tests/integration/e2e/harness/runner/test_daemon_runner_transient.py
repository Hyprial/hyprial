"""E2E daemon-runner hardening: transient IPC tolerance + failure forensics.

Covers the two E2E-002 gate-flake fixes:

- ``IsolatedDaemon.wait()`` tolerates transient daemon-IPC failures
  (DAEMON_DISCONNECTED / DAEMON_UNAVAILABLE / IPC_TIMEOUT) until its
  deadline instead of failing the scenario on one restart-window blip;
- failure forensics: the scenario state root (daemon.jsonl,
  daemon-launch.log, ...) is preserved into the run-scoped artifacts dir
  instead of being deleted by TemporaryDirectory cleanup.

PR #332 F4②: the runner's local TransientDaemonError(AssertionError) was
replaced by the shared registry hierarchy (contracts/ipc_errors.py); the
fixtures below construct registry instances, mirroring what
``transient_cli_error`` really returns (stub shapes follow the seam).

Decision (2026-11-26): ``fcntl`` is imported inside the two tests that take
a real ``flock`` of ``daemon.lock`` themselves, not at module level, so the
file is collectable on Windows.  Those two carry ``posix_only("fcntl")``;
every other test here needs no lock and runs unmarked on every platform.
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel import ipc_errors
from hyprial.kernel.impl.contracts.daemon.ipc_errors import TransientDaemonError

REPO = Path(__file__).resolve().parents[5]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "_daemon_runner", REPO / "contract/e2e/_daemon_runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "contract/e2e"))
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _daemon() -> Any:
    return runner.IsolatedDaemon(Path("/bin/echo"), Path("/tmp"), "test-node")


def test_start_never_installs_a_user_service(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _daemon()
    calls = []
    ready = {"ok": True, "ready": True}
    monkeypatch.setattr(daemon, "command", lambda *args: calls.append(args) or ready)
    monkeypatch.setattr(daemon, "require_no_external_triggers", lambda: None)
    monkeypatch.setattr(daemon, "require_network_isolated", lambda: None)
    monkeypatch.setattr(runner, "launch_result_is_ready", lambda value: value is ready)

    assert daemon.start() is ready
    assert calls == [("init", "--no-service", "--json")]


def _transient_fixture(code: str, message: str) -> TransientDaemonError:
    """A registry instance, as ``transient_cli_error`` really returns one."""

    error = ipc_errors.transient_error_from_code(code, message)
    assert error is not None, f"{code} must be a registered transient code"
    return error


def _cli_failure(code: str) -> str:
    return json.dumps({"ok": False, "code": code, "error": "fixture"})


# ---------------------------------------------------------------------------
# wait() tolerance: one test per transient code (do not merge them).
# ---------------------------------------------------------------------------


def _tolerant_wait_returns_after_transients(code: str) -> None:
    daemon = _daemon()
    attempts = {"n": 0}

    def probe() -> dict[str, bool]:
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise _transient_fixture(code, f"fixture {code}")
        return {"ready": True}

    result = daemon.wait(probe, lambda value: value.get("ready") is True)
    assert result == {"ready": True}
    assert attempts["n"] == 4


def test_wait_tolerates_daemon_disconnected() -> None:
    _tolerant_wait_returns_after_transients("DAEMON_DISCONNECTED")


def test_wait_tolerates_daemon_unavailable() -> None:
    _tolerant_wait_returns_after_transients("DAEMON_UNAVAILABLE")


def test_wait_tolerates_ipc_timeout() -> None:
    _tolerant_wait_returns_after_transients("IPC_TIMEOUT")


def test_wait_tolerates_daemon_restoring() -> None:
    # PR #332 F4①: the restore gate's refusal joined the transient table;
    # a probe that hits it while the daemon generation is still restoring
    # must be waited out like any restart-window blip.
    _tolerant_wait_returns_after_transients("DAEMON_RESTORING")


def test_wait_times_out_with_the_transient_detail() -> None:
    daemon = _daemon()

    def probe() -> None:
        raise ipc_errors.DaemonUnavailableError("fixture down")

    started = time.monotonic()
    with pytest.raises(AssertionError, match="last transient failure"):
        daemon.wait(probe, lambda value: True, timeout=1.0)
    assert time.monotonic() - started >= 1.0


def test_wait_propagates_non_transient_assertions_immediately() -> None:
    daemon = _daemon()

    def probe() -> None:
        raise AssertionError("logic failure, not a blip")

    started = time.monotonic()
    with pytest.raises(AssertionError, match="logic failure"):
        daemon.wait(probe, lambda value: True, timeout=30.0)
    assert time.monotonic() - started < 5.0


@pytest.mark.posix_only("fcntl")
def test_wait_stopped_waits_for_complete_daemon_lock_release(tmp_path: Path) -> None:
    import fcntl
    daemon = runner.IsolatedDaemon(Path("/bin/echo"), tmp_path, "test-node")
    lock_path = tmp_path / "state" / "daemon.lock"
    lock_path.parent.mkdir(parents=True)
    holder = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release() -> None:
        time.sleep(0.2)
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    thread = threading.Thread(target=release)
    thread.start()
    began = time.monotonic()
    daemon.wait_stopped(timeout=2.0)
    elapsed = time.monotonic() - began
    thread.join(timeout=1.0)

    assert elapsed >= 0.15
    assert not thread.is_alive()


@pytest.mark.posix_only("fcntl")
def test_wait_stopped_timeout_emits_bounded_daemon_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fcntl
    daemon = runner.IsolatedDaemon(Path("/bin/echo"), tmp_path, "test-node")
    state = tmp_path / "state"
    lock_path = state / "daemon.lock"
    logs = state / "logs"
    logs.mkdir(parents=True)
    lock_path.touch()
    (state / "daemon.json").write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )
    (logs / "daemon.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-25T00:00:00Z",
                "event": "daemon.stopping",
                "level": "info",
                "message": "free text must not cross",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner-temp"))
    holder = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(AssertionError, match="did not finish teardown"):
            daemon.wait_stopped(timeout=0.0)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    line = capsys.readouterr().out.strip()
    assert line.startswith("DAEMON_TIMEOUT_EVIDENCE=")
    evidence = json.loads(line.split("=", 1)[1])["evidence"]
    assert evidence["kind"] == "child-stop-timeout"
    assert evidence["child"]["pid"] == os.getpid()
    assert evidence["daemonLog"] == [
        {
            "ts": "2026-09-25T00:00:00Z",
            "event": "daemon.stopping",
            "level": "info",
        }
    ]


# ---------------------------------------------------------------------------
# command() classification and bounded transient retry.
# ---------------------------------------------------------------------------


def _fake_run(outputs: list[subprocess.CompletedProcess[str]], calls: list[int]):
    def fake(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return outputs[min(len(calls) - 1, len(outputs) - 1)]

    return fake


def test_command_classifies_transient_cli_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        _fake_run(
            [
                subprocess.CompletedProcess(
                    [], 1, _cli_failure("DAEMON_DISCONNECTED"), ""
                )
            ],
            calls,
        ),
    )
    with pytest.raises(TransientDaemonError) as caught:
        _daemon().command("daemon", "status", "--json")
    assert caught.value.code == "DAEMON_DISCONNECTED"


def test_command_classifies_daemon_restoring_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        _fake_run(
            [subprocess.CompletedProcess([], 1, _cli_failure("DAEMON_RESTORING"), "")],
            calls,
        ),
    )
    with pytest.raises(TransientDaemonError) as caught:
        _daemon().command("message", "send", "--json")
    assert caught.value.code == "DAEMON_RESTORING"


def test_command_retries_daemon_restoring_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #332 F4①: a command fired during the restoring phase retries
    until the gate opens instead of failing the scenario."""
    calls: list[int] = []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        _fake_run(
            [
                subprocess.CompletedProcess(
                    [], 1, _cli_failure("DAEMON_RESTORING"), ""
                ),
                subprocess.CompletedProcess([], 0, '{"ok":true}', ""),
            ],
            calls,
        ),
    )
    result = _daemon().command("message", "send", "--json", transient_timeout=5.0)
    assert result == {"ok": True}
    assert len(calls) == 2


def test_command_keeps_hard_failures_non_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        _fake_run(
            [subprocess.CompletedProcess([], 1, _cli_failure("INVALID_ARGUMENT"), "")],
            calls,
        ),
    )
    with pytest.raises(AssertionError) as caught:
        _daemon().command("daemon", "status", "--json")
    assert not isinstance(caught.value, TransientDaemonError)


def test_command_retries_transients_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        _fake_run(
            [
                subprocess.CompletedProcess([], 1, _cli_failure("IPC_TIMEOUT"), ""),
                subprocess.CompletedProcess([], 0, '{"ok":true}', ""),
            ],
            calls,
        ),
    )
    result = _daemon().command("daemon", "status", "--json", transient_timeout=5.0)
    assert result == {"ok": True}
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Failure forensics.
# ---------------------------------------------------------------------------


_ARTIFACT_CALLER_LAYOUTS = {
    "_group_conversation/phases.py": {("d",)},
    "run-capabilities-dispatch-gate.py": {()},
    "run-cc-tui-worker.py": {("sender",), ("worker",)},
    "run-cli-restart.py": {()},
    "run-daemon-restart-survival.py": {()},
    "run-orgfs-forwarding-redial.py": {("a",), ("b",)},
    "run-peer-restart-relearn.py": {
        (f"case-{case}", node)
        for case in range(1, 5)
        for node in ("a", "b")
    },
    "run-pi-interactive-attach-manual.py": {("sender",), ("worker",)},
    "run-remote-drop-no-rebuild.py": {("a",), ("b",)},
    "run-first-run-no-agent.py": {()},
    "run-tailcat-org-invite-join.py": {("a",), ("b",)},
    "run-tailcat-cycle-upgrade.py": {("a",), ("b",), ("d",)},
    "run-tailcat-membership-hardening.py": {()},
}


def test_artifact_allowlist_covers_every_preservation_caller() -> None:
    """A new preservation caller must declare its daemon-root layout here."""

    callers: set[str] = set()
    e2e_root = REPO / "contract/e2e"
    for path in e2e_root.rglob("*.py"):
        if path.name == "artifacts.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and getattr(node.func, "id", getattr(node.func, "attr", None))
            in {"preserve_failure_state", "scenario_tempdir"}
            for node in ast.walk(tree)
        ):
            callers.add(path.relative_to(e2e_root).as_posix())

    assert callers == set(_ARTIFACT_CALLER_LAYOUTS)
    declared_roots = set().union(*_ARTIFACT_CALLER_LAYOUTS.values())
    configured_roots = runner.preserve_failure_state.__globals__[
        "_DAEMON_ARTIFACT_ROOTS"
    ]
    assert configured_roots == declared_roots


def test_preserve_failure_state_captures_daemon_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("HYPRIAL_E2E_ARTIFACTS", str(artifacts))
    state = tmp_path / "scenario" / "state"
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "daemon.jsonl").write_text('{"event":"daemon.ready"}\n')
    (state / "daemon-launch.log").write_text("launch\n")

    destination = runner.preserve_failure_state(tmp_path / "scenario", "e2e-002")

    assert destination is not None
    assert (destination / "state" / "logs" / "daemon.jsonl").is_file()
    assert (destination / "state" / "daemon-launch.log").is_file()


_FAILURE_ARTIFACT_SENTINEL_FILES = (
    "dedicated-app-secret",
    "a/sidecar-state/address",
    "home/secrets/login.json",
    # A diagnostic-looking suffix inside a credential subtree must not collide.
    "home/secrets/nested/state/logs/daemon.jsonl",
    "home/provider/.credentials.json",
    "home/provider/auth.json",
    "home/provider/model-credential.json",
    "home/provider/access-token.json",
    "home/provider/client-secret.json",
    "home/provider/device-key.json",
    # Both real locations: HARNESS_STATE_DIR and the canonical-home fallback.
    "state/lark-reauth.json",
    "home/state/lark-reauth.json",
    "home/tls/private.key",
    "home/tls/private.pem",
    "home/state/transport/session.json",
    "home/state/tailcat/address",
    "home/state/enrollment.json",
    # An unknown basename must be excluded without credential-name guessing.
    "state/notes.json",
    # A diagnostic suffix below an unregistered/deeper daemon root stays closed.
    "outer/inner/a/state/logs/daemon.jsonl",
)


def _bind_scenario_socket(unsafe_socket: socket.socket, scenario: Path) -> None:
    # macOS limits AF_UNIX names to 104 bytes; pytest's tmp_path is longer.
    with tempfile.TemporaryDirectory(dir=runner.short_temp_root()) as short:
        alias = Path(short) / "r"
        alias.symlink_to(scenario, target_is_directory=True)
        unsafe_socket.bind(str(alias / "runtime.sock"))


def _write_sensitive_failure_fixture(root: Path, sentinel: bytes) -> None:
    for relative in _FAILURE_ARTIFACT_SENTINEL_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sentinel)
    for node in (
        Path(),
        Path("a"),
        Path("case-1/a"),
        Path("case-4/b"),
    ):
        logs = root / node / "state/logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "daemon.jsonl").write_text('{"event":"daemon.ready"}\n')
        (root / node / "state/daemon-launch.log").write_text("launch\n")


@pytest.mark.parametrize("through_failure_context", [False, True])
def test_failure_artifacts_exclude_credentials_and_unsafe_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    through_failure_context: bool,
) -> None:
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("HYPRIAL_E2E_ARTIFACTS", str(artifacts))
    sentinel = b"E2E-CREDENTIAL-SENTINEL-1778"
    outside = tmp_path / "outside-login.json"
    outside.write_bytes(sentinel)

    unsafe_socket = socket.socket(socket.AF_UNIX)
    try:
        if through_failure_context:
            with pytest.raises(AssertionError, match="boom"):
                with runner.scenario_tempdir(
                    "e2e-sensitive", directory=tmp_path
                ) as scenario:
                    _write_sensitive_failure_fixture(scenario, sentinel)
                    (scenario / "linked-login.json").symlink_to(outside)
                    _bind_scenario_socket(unsafe_socket, scenario)
                    raise AssertionError("boom")
            destinations = list(artifacts.glob("e2e-sensitive-*"))
            assert len(destinations) == 1
            destination = destinations[0]
        else:
            scenario = tmp_path / "scenario"
            _write_sensitive_failure_fixture(scenario, sentinel)
            (scenario / "linked-login.json").symlink_to(outside)
            _bind_scenario_socket(unsafe_socket, scenario)
            destination = runner.preserve_failure_state(scenario, "e2e-sensitive")
            assert destination is not None
    finally:
        unsafe_socket.close()

    assert (destination / "state/logs/daemon.jsonl").is_file()
    assert (destination / "state/daemon-launch.log").is_file()
    assert (destination / "a/state/logs/daemon.jsonl").is_file()
    assert (destination / "a/state/daemon-launch.log").is_file()
    assert (destination / "case-1/a/state/logs/daemon.jsonl").is_file()
    assert (destination / "case-1/a/state/daemon-launch.log").is_file()
    assert (destination / "case-4/b/state/logs/daemon.jsonl").is_file()
    assert (destination / "case-4/b/state/daemon-launch.log").is_file()
    assert all(
        sentinel not in path.read_bytes()
        for path in artifacts.rglob("*")
        if path.is_file()
    )
    output = capsys.readouterr().out
    for relative in (
        *_FAILURE_ARTIFACT_SENTINEL_FILES,
        "linked-login.json",
        "runtime.sock",
    ):
        assert f"  - {relative} (" in output
    assert "symlink" in output
    assert "unsafe non-regular path" in output
    assert "not in diagnostic artifact allowlist" in output


def test_scenario_tempdir_never_masks_failure_when_reporting_breaks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_E2E_ARTIFACTS", str(tmp_path / "artifacts"))

    def broken_print(*args: object, **kwargs: object) -> None:
        raise BrokenPipeError("closed artifact report")

    monkeypatch.setattr(builtins, "print", broken_print)
    with pytest.raises(AssertionError, match="original boom"):
        with runner.scenario_tempdir("e2e-broken-report", directory=tmp_path):
            raise AssertionError("original boom")


def test_scenario_tempdir_preserves_only_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_E2E_ARTIFACTS", str(tmp_path / "artifacts"))

    with runner.scenario_tempdir("e2e-ok") as kept:
        (kept / "state").mkdir()
        (kept / "state" / "daemon-launch.log").write_text("launch\n")
    assert not list((tmp_path / "artifacts").glob("*"))

    with pytest.raises(AssertionError, match="boom"):
        with runner.scenario_tempdir("e2e-bad") as kept:
            (kept / "state" / "logs").mkdir(parents=True)
            (kept / "state" / "logs" / "daemon.jsonl").write_text("{}\n")
            raise AssertionError("boom")
    preserved = list((tmp_path / "artifacts").glob("e2e-bad-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "state" / "logs" / "daemon.jsonl").is_file()
