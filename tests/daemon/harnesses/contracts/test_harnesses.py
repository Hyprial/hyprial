from __future__ import annotations

import pytest

from hyprial.daemon.impl.api import HarnessLauncher as HarnessLauncherProtocol
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses import (
    HarnessLauncher,
    HarnessStartError,
)
from hyprial.daemon.impl.harnesses.protocol.common import summarize_stderr


def managed_spec(
    harness: str,
    *,
    headless: bool = True,
    args: tuple[str, ...] = (),
    cwd: str | None = None,
    session_ref: str | None = None,
) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name=f"{harness}-worker",
        headless=headless,
        args=args,
        cwd=cwd,
        session_ref=session_ref,
    )


def test_launcher_implements_daemon_protocol_without_concrete_dependency() -> None:
    launcher = HarnessLauncher()

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


def test_default_managed_agy_launch_uses_acp_streaming_process() -> None:
    launched = []

    class FakeAcpProcess:
        running = True

        def wait_ready(self, *, timeout: float) -> bool:
            return True

        def stop(self) -> None:
            self.running = False

    process = FakeAcpProcess()
    launcher = HarnessLauncher(
        acp_factory=lambda spec: launched.append(spec) or process
    )
    spec = managed_spec("agy", args=("--model", "gemini-3.8-flash"))

    assert launcher.start(spec) is process
    assert launched == [spec]


@pytest.mark.parametrize("harness", ["claude", "pi", "codex", "agy"])
def test_interactive_specs_are_refused_without_spawning(harness: str) -> None:
    def _must_not_spawn(spec: HarnessLaunchSpec):
        raise AssertionError(f"no process may be spawned for {spec.harness}")

    launcher = HarnessLauncher(
        agent_sdk_factory=_must_not_spawn,
        pi_rpc_factory=_must_not_spawn,
        codex_app_server_factory=_must_not_spawn,
        python_worker_factory=_must_not_spawn,
        acp_factory=_must_not_spawn,
    )

    with pytest.raises(HarnessStartError) as raised:
        launcher.start(managed_spec(harness, headless=False))

    assert "headless" in str(raised.value)
    assert "CLI" in str(raised.value)


def test_unknown_harness_is_refused() -> None:
    launcher = HarnessLauncher()

    with pytest.raises(HarnessStartError):
        launcher.start(managed_spec("smoke-signals"))


def test_start_error_surfaces_bounded_last_eight_stderr_lines() -> None:
    stderr = "\n".join(
        [f"discard-{i}" for i in range(3)]
        + ["x" * 8192 + "fatal-tail"]
        + [f"recent-{i}" for i in range(7)]
    )

    tail = summarize_stderr(stderr.encode("utf-8"))
    error = HarnessStartError("claude", (), "process exited", stderr_tail=tail)

    lines = error.stderr_tail.splitlines()
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
    stderr = ("\n".join(leaked.values()) + "\n").encode("utf-8")

    surfaced = summarize_stderr(stderr)

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
    stderr = ("\n".join(leaked) + "\n").encode("utf-8")

    surfaced = summarize_stderr(stderr)

    assert surfaced.lower().count("authorization: [redacted]") == 3
    for secret in (
        "dXNlcjpwYXNz",
        "digest-response-value",
        "lowercase-payload",
    ):
        assert secret not in surfaced
