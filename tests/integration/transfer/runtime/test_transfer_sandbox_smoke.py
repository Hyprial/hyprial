"""The mapping-time sandbox smoke (design §4 / card 356 item 0).

The smoke sits between "the payload is mapped on the target" and "the
session is resumed".  These tests pin what it may claim: a codex host whose
own sandbox entry refuses the outside write passes, a codex host whose
sandbox lets the write through refuses the landing, and every harness whose
native sandbox this repo cannot prove is reported ``NOT_RUN`` -- never a
silent pass.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

from hyprial.daemon.impl.transfer.execution.sandbox_smoke import (
    SMOKE_FAIL,
    SMOKE_NOT_RUN,
    SMOKE_PASS,
    SmokeResult,
    run_sandbox_smoke,
    summarise)


class _Runner:
    """One scripted ``subprocess.run`` call."""

    def __init__(
        self,
        returncode: int = 0,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        error: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.argv: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.calls = 0

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls += 1
        self.argv = list(argv)
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def test_harnesses_without_a_native_sandbox_are_reported_not_run() -> None:
    runner = _Runner()
    for harness in ("pi", "claude"):
        result = run_sandbox_smoke(harness, runner=runner)
        assert result.status == SMOKE_NOT_RUN, harness
        assert "no provable native sandbox" in result.detail
    assert runner.calls == 0, "NOT_RUN must not shell out and must not pass"


def test_a_container_landing_is_not_run_and_says_why() -> None:
    result = run_sandbox_smoke("codex", containerized=True, runner=_Runner())
    assert result.status == SMOKE_NOT_RUN
    assert "image" in result.detail


def test_codex_passes_when_its_sandbox_refuses_the_outside_write() -> None:
    runner = _Runner(stdout=b"native-read-only-sentinel: PASS\n")
    result = run_sandbox_smoke("codex", runner=runner)

    assert result.status == SMOKE_PASS
    assert runner.argv[:3] == ["codex", "sandbox", "-c"]
    assert 'sandbox_mode="read-only"' in runner.argv
    assert runner.argv[5] == "/bin/sh", "the sentinel runs under a shell"
    assert runner.argv[-2] == "hyprial-sandbox-smoke"
    assert runner.argv[-1].endswith("sentinel"), runner.argv[-1]
    # Credentials are never needed for this smoke, so they are not handed over.
    env = runner.kwargs["env"]
    assert not any(
        name.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
        for name in env
    )
    assert env["CODEX_HOME"].startswith(env["TMPDIR"].rsplit("/tmp", 1)[0])


def test_a_sandbox_that_lets_the_write_through_refuses_the_landing() -> None:
    result = run_sandbox_smoke("codex", runner=_Runner(returncode=91))

    assert result.status == SMOKE_FAIL
    assert "SUCCEEDED" in result.detail
    assert "not enforcing" in result.detail


def test_any_other_exit_is_a_failure_with_the_harness_output() -> None:
    runner = _Runner(returncode=1, stderr=b"error: sandbox unavailable\n")
    result = run_sandbox_smoke("codex", runner=runner)

    assert result.status == SMOKE_FAIL
    assert "exited 1" in result.detail
    assert "sandbox unavailable" in result.detail


def test_a_missing_binary_is_a_failure_not_a_skip() -> None:
    result = run_sandbox_smoke("codex", runner=_Runner(error=FileNotFoundError("codex")))

    assert result.status == SMOKE_FAIL
    assert "could not run" in result.detail


def test_a_hanging_smoke_is_a_failure_not_a_skip() -> None:
    result = run_sandbox_smoke(
        "codex", runner=_Runner(error=subprocess.TimeoutExpired("codex", 20.0))
    )

    assert result.status == SMOKE_FAIL
    assert "did not finish" in result.detail


def test_the_summary_never_lets_not_run_read_as_a_pass() -> None:
    verdicts = [
        SmokeResult("pi", SMOKE_NOT_RUN, "no entry point"),
        SmokeResult("codex", SMOKE_PASS, "denied"),
    ]
    assert summarise(verdicts) == "pi:NOT_RUN, codex:PASS"


def test_a_real_codex_run_passes_or_is_explicitly_skipped() -> None:
    """The injected-runner tests cannot catch a broken CODEX_HOME.

    A scratch home that is never created makes codex exit before the sandbox
    even starts, so every codex landing would be refused for a reason that has
    nothing to do with the sandbox.  This runs the real binary when the machine
    has one (skip where it does not, e.g. CI without a codex install).
    """

    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("no codex on PATH; the real-binary smoke cannot run here")

    result = run_sandbox_smoke("codex")

    assert result.status == SMOKE_PASS, result.detail
    assert "denied" in result.detail
