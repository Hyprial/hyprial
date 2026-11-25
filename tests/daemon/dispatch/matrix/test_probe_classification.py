"""Probe command construction, classification and probe_combinations over the capability store."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.probe import (
    ProbeRun,
    RuntimeProber,
    _subprocess_runner,
    build_probe_command,
    classify_run,
    probe_combinations,
)
from hyprial.identity import RuntimeCapabilityStore

OWNER = "owner-key-1"


def test_build_probe_command_uses_real_invocations() -> None:
    assert build_probe_command("pi", "k3", "kimi-coding") == (
        "pi",
        "-p",
        "--provider",
        "kimi-coding",
        "--model",
        "k3",
        "--thinking",
        "high",
        "hello",
    )
    assert build_probe_command("claude", "sonnet") == (
        "claude",
        "-p",
        "--model",
        "sonnet",
        "hello",
    )
    assert build_probe_command("codex", "gpt-5.3-codex") == (
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--model",
        "gpt-5.3-codex",
        "hello",
    )


def test_build_probe_command_requires_provider_for_pi() -> None:
    with pytest.raises(ValueError, match="provider"):
        build_probe_command("pi", "k3")
    with pytest.raises(ValueError, match="unsupported harness"):
        build_probe_command("emacs", "k3")


def test_classify_run_success() -> None:
    assert classify_run(ProbeRun(0, "Hello!")) == ("available", None, None)


def test_classify_run_usage_limit_without_retry_hint() -> None:
    status, reason, retry_at = classify_run(
        ProbeRun(1, "You're out of extra usage for this period")
    )
    assert (status, reason, retry_at) == ("unavailable", "usage-limit", None)


def test_classify_run_usage_limit_captures_retry_at() -> None:
    output = (
        "ERROR: You've hit your usage limit. try again at 2026-08-07T10:30:00Z."
    )
    status, reason, retry_at = classify_run(ProbeRun(1, output))
    assert status == "unavailable"
    assert reason == "usage-limit"
    assert retry_at == "2026-08-07T10:30:00Z"


def test_classify_run_retry_at_kept_raw_when_unparseable() -> None:
    _, _, retry_at = classify_run(
        ProbeRun(1, "usage limit reached, try again at tomorrow morning")
    )
    assert retry_at == "tomorrow morning"


def test_classify_run_missing_credential() -> None:
    status, reason, _ = classify_run(
        ProbeRun(1, "Error: 401 Unauthorized: invalid api key")
    )
    assert (status, reason) == ("unavailable", "missing-credential")


def test_classify_run_command_not_found_and_timeout() -> None:
    assert classify_run(ProbeRun(None, "command not found: pi"))[:2] == (
        "unavailable",
        "command-not-found",
    )
    assert classify_run(ProbeRun(None, "", timed_out=True))[:2] == (
        "unavailable",
        "probe-timeout",
    )


def test_classify_run_generic_failure() -> None:
    status, reason, _ = classify_run(ProbeRun(2, "traceback: something exploded"))
    assert (status, reason) == ("unavailable", "probe-error")


def test_subprocess_runner_reports_missing_command() -> None:
    run = _subprocess_runner(("hyprial-definitely-not-a-real-binary",), 1.0)
    assert run.exit_code is None
    assert run.timed_out is False
    assert "command not found" in run.output


def test_subprocess_runner_reports_timeout() -> None:
    run = _subprocess_runner(
        (sys.executable, "-c", "import time; time.sleep(30)"), 0.2
    )
    assert run.timed_out is True


def test_prober_records_facts_with_injected_clock() -> None:
    calls: list[tuple[tuple[str, ...], float]] = []

    def runner(command: tuple[str, ...], timeout: float) -> ProbeRun:
        calls.append((command, timeout))
        return ProbeRun(0, "hi")

    prober = RuntimeProber(
        runner=runner,
        timeout=42.0,
        now=lambda: datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    result = prober.probe("pi", "k3", "kimi-coding")

    assert calls == [(build_probe_command("pi", "k3", "kimi-coding"), 42.0)]
    assert result.status == "available"
    assert result.probed_at == "2026-08-06T12:00:00Z"
    assert result.reason is None


def test_prober_records_failure_detail() -> None:
    prober = RuntimeProber(
        runner=lambda command, timeout: ProbeRun(1, "You're out of extra usage"),
    )
    result = prober.probe("claude", "opus")
    assert result.status == "unavailable"
    assert result.reason == "usage-limit"
    assert result.detail == "You're out of extra usage"


def test_probe_combinations_persists_unless_dry_run(tmp_path: Path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime-capabilities.json")
    prober = RuntimeProber(runner=lambda command, timeout: ProbeRun(0, "ok"))

    results, changed, capabilities = probe_combinations(
        store,
        OWNER,
        [("claude", None, "sonnet"), ("pi", "kimi-coding", "k3")],
        prober=prober,
    )
    assert len(results) == 2
    assert changed == ("runtimeCapabilities",) * 2
    assert len(capabilities) == 2

    results, changed, capabilities = probe_combinations(
        store,
        OWNER,
        [("codex", None, "gpt-5.3-codex")],
        prober=prober,
        dry_run=True,
    )
    assert changed == ()
    assert "codex" not in {c.harness for c in capabilities}
    reloaded = RuntimeCapabilityStore(store.path).list(OWNER)
    assert "codex" not in {c.harness for c in reloaded}
    assert len(reloaded) == 2


def test_probe_combinations_needs_no_registered_owner(tmp_path: Path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime-capabilities.json")
    prober = RuntimeProber(runner=lambda command, timeout: ProbeRun(0, "ok"))

    _results, changed, capabilities = probe_combinations(
        store, "never-seen", [("claude", None, "sonnet")], prober=prober
    )

    assert changed == ("runtimeCapabilities",)
    assert [c.model for c in capabilities] == ["sonnet"]
