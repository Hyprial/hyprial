from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessLauncher as HarnessLauncherProtocol
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses import (
    OperationStatus,
    HarnessLauncher,
    HarnessStartError,
    PtyHarnessProcess,
)


def managed_spec(
    harness: str,
    *,
    args: tuple[str, ...] = (),
    cwd: str | None = None,
    session_ref: str | None = None,
) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name=f"{harness}-worker",
        headless=True,
        args=args,
        cwd=cwd,
        session_ref=session_ref,
    )


def wait_for_completion(
    process: PtyHarnessProcess, lookup_ref: str, timeout: float = 2.0
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = process.retrieve(lookup_ref)
        if snapshot.status is not OperationStatus.IN_PROGRESS:
            return snapshot
        time.sleep(0.01)
    pytest.fail(f"harness operation {lookup_ref} did not complete")


@pytest.mark.parametrize("harness", ["claude", "pi", "codex"])
def test_real_pty_cat_supports_lifecycle_inject_and_retrieve(harness: str) -> None:
    process = PtyHarnessProcess.spawn(harness, ("/bin/cat",))
    try:
        assert process.running

        lookup_ref = process.inject("hello from harness")
        snapshot = wait_for_completion(process, lookup_ref)

        assert snapshot.harness == harness
        assert snapshot.lookup_ref == lookup_ref
        assert snapshot.status is OperationStatus.COMPLETED
        assert snapshot.output == "hello from harness\n"
    finally:
        process.stop()

    assert not process.running
    process.stop()  # lifecycle shutdown is idempotent


def test_real_sleep_process_remains_in_progress_until_stopped() -> None:
    process = PtyHarnessProcess.spawn("pi", ("/bin/sleep", "30"))
    try:
        lookup_ref = process.inject("queued input")
        assert process.retrieve(lookup_ref).status is OperationStatus.IN_PROGRESS
    finally:
        process.stop()

    snapshot = process.retrieve(lookup_ref)
    assert snapshot.status is OperationStatus.FAILED
    assert snapshot.error == "harness process stopped before producing a result"


def test_launcher_implements_daemon_protocol_without_concrete_dependency() -> None:
    launcher = HarnessLauncher(
        commands={
            "claude": (sys.executable, "-c", "import time; time.sleep(30)"),
            "pi": (sys.executable, "-c", "import time; time.sleep(30)"),
            "codex": (sys.executable, "-c", "import time; time.sleep(30)"),
        }
    )

    assert isinstance(launcher, HarnessLauncherProtocol)


def test_default_managed_claude_launch_uses_agent_sdk_streaming_process() -> None:
    launched = []

    class FakeSdkProcess:
        running = True

        def wait_ready(self, *, timeout: float) -> bool:
            return True

        def stop(self) -> None:
            self.running = False

    process = FakeSdkProcess()
    launcher = HarnessLauncher(
        agent_sdk_factory=lambda spec: launched.append(spec) or process
    )
    spec = managed_spec("claude", args=("--model", "opus"))

    assert launcher.start(spec) is process
    assert launched == [spec]


def test_claude_retries_use_fresh_sessions_without_losing_model_passthrough() -> None:
    launcher = HarnessLauncher(
        commands={
            "claude": (sys.executable, "-c", "import time; time.sleep(30)"),
        }
    )
    spec = managed_spec(
        "claude",
        args=("--model", "opus", "--session-id", "caller-must-not-win"),
        session_ref="stable-control-plane-ref",
    )

    first = launcher.start(spec)
    second = launcher.start(spec)  # daemon retry uses the same public seam
    try:
        first_session = first.argv[first.argv.index("--session-id") + 1]
        second_session = second.argv[second.argv.index("--session-id") + 1]

        assert first_session != second_session
        assert first_session not in {"caller-must-not-win", spec.session_ref}
        assert second_session not in {"caller-must-not-win", spec.session_ref}
        assert first.argv.count("--session-id") == 1
        assert second.argv.count("--session-id") == 1
        assert first.argv[first.argv.index("--model") + 1] == "opus"
        assert second.argv[second.argv.index("--model") + 1] == "opus"
    finally:
        first.stop()
        second.stop()


@pytest.mark.parametrize("harness", ["claude", "pi", "codex"])
def test_start_error_surfaces_bounded_last_eight_stderr_lines(harness: str) -> None:
    code = (
        "import sys; "
        "[print(f'discard-{i}', file=sys.stderr) for i in range(3)]; "
        "print('x' * 8192 + 'fatal-tail', file=sys.stderr); "
        "[print(f'recent-{i}', file=sys.stderr) for i in range(7)]; "
        "raise SystemExit(7)"
    )
    launcher = HarnessLauncher(commands={harness: (sys.executable, "-c", code)})

    with pytest.raises(HarnessStartError) as raised:
        launcher.start(managed_spec(harness))

    error = raised.value
    lines = error.stderr_tail.splitlines()
    assert error.returncode == 7
    assert len(lines) <= 8
    assert len(error.stderr_tail.encode("utf-8")) <= 4 * 1024
    assert lines[0].startswith("… [truncated] ")
    assert error.stderr_tail.endswith("recent-6")
    assert "discard-0" not in error.stderr_tail
    assert error.stderr_tail in str(error)


def test_start_error_redacts_six_credential_shapes() -> None:
    leaked = {
        "api-key": "api_key=api-value",
        "token": "TOKEN: token-value",
        "secret": "client-secret=secret-value",
        "password": "password: password-value",
        "credential": "credential=credential-value",
        "authorization": "Authorization: Bearer authorization-value",
    }
    code = (
        "import sys; "
        + "; ".join(f"print({line!r}, file=sys.stderr)" for line in leaked.values())
        + "; raise SystemExit(9)"
    )
    launcher = HarnessLauncher(commands={"pi": (sys.executable, "-c", code)})

    with pytest.raises(HarnessStartError) as raised:
        launcher.start(managed_spec("pi"))

    surfaced = raised.value.stderr_tail
    assert surfaced.count("[REDACTED]") == 6
    for secret in (
        "api-value",
        "token-value",
        "secret-value",
        "password-value",
        "credential-value",
        "authorization-value",
    ):
        assert secret not in surfaced


def test_start_error_redacts_authorization_schemes_and_payloads() -> None:
    leaked = (
        "Authorization: Basic dXNlcjpwYXNz",
        "Authorization: Digest digest-response-value",
        "authorization: basic lowercase-payload",
    )
    code = (
        "import sys; "
        + "; ".join(f"print({line!r}, file=sys.stderr)" for line in leaked)
        + "; raise SystemExit(9)"
    )
    launcher = HarnessLauncher(commands={"claude": (sys.executable, "-c", code)})

    with pytest.raises(HarnessStartError) as raised:
        launcher.start(managed_spec("claude"))

    surfaced = raised.value.stderr_tail
    assert surfaced.lower().count("authorization: [redacted]") == 3
    for secret in (
        "dXNlcjpwYXNz",
        "digest-response-value",
        "lowercase-payload",
    ):
        assert secret not in surfaced


def test_invalid_cwd_start_error_preserves_original_os_error(tmp_path: Path) -> None:
    missing = tmp_path / "deleted-worktree"
    launcher = HarnessLauncher(commands={"codex": ("/bin/cat",)})

    with pytest.raises(HarnessStartError) as raised:
        launcher.start(managed_spec("codex", cwd=str(missing)))

    assert "No such file or directory" in str(raised.value)
    assert str(missing) in str(raised.value)
