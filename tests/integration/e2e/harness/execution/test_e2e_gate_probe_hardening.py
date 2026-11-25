"""Mutation tests for the E2E-013 gate-hardening probes (fix/gate-hardening-probes).

Each mutation pins one failure class the hardening was built for:

- worker-cwd pid-reuse: a dead or re-owned pid is a PROBE failure
  (ProbeObservationError -> BLOCKED at the call site), never a product FAIL
  and never a silent pass;
- lark preflight needs_refresh: one side-effect-free read must be attempted
  to trigger the lazy refresh; a permanently invalid token still blocks;
- Lark read-back retry: transient failures are retried with logging and a
  success on a later attempt is green; exhaustion raises ReadbackExhausted
  so the caller can classify observation-layer vs delivery failure.

Retry safety invariant pinned by these tests: the retried operations are
pure reads; the send path (the action under test) is never retried.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
RUN_013 = ROOT / "contract" / "e2e" / "run-all-harness-lifecycle.py"


def load_module(path: Path, name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


@pytest.fixture()
def run013():  # type: ignore[no-untyped-def]
    return load_module(RUN_013, "e2e013_runner_gate_hardening")


# -- ① worker-cwd pid-reuse guard ---------------------------------------------


def test_worker_cwd_dead_pid_is_probe_error_not_fail(run013, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A pid that died before lsof is a probe failure, not a product one."""

    monkeypatch.setattr(run013, "process_command", lambda pid: None)
    with pytest.raises(run013.ProbeObservationError, match="pid 已易主"):
        run013.worker_cwd_checked("claude", 424242)


def test_worker_cwd_reused_pid_is_probe_error_not_fail(run013, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """run 971's mutation: the pid now belongs to the CI runner itself."""

    monkeypatch.setattr(
        run013,
        "process_command",
        lambda pid: "/usr/bin/python3 /Users/ci/.cache/act/hostexecutor/job.py",
    )
    with pytest.raises(run013.ProbeObservationError, match="pid 已易主"):
        run013.worker_cwd_checked("claude", 424242)


def test_worker_cwd_lsof_failure_after_liveness_is_probe_error(run013, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The worker can still exit between the liveness check and lsof."""

    monkeypatch.setattr(
        run013,
        "process_command",
        lambda pid: (
            "claude --output-format stream-json --session-id abc "
            "--input-format stream-json"
        ),
    )

    def no_cwd(pid: int) -> Path:
        raise AssertionError("lsof exposed no cwd")

    monkeypatch.setattr(run013, "process_cwd", no_cwd)
    with pytest.raises(run013.ProbeObservationError, match="pid 已易主"):
        run013.worker_cwd_checked("claude", 424242)


def test_worker_cwd_live_matching_pid_returns_cwd(run013, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        run013,
        "process_command",
        lambda pid: (
            "claude --output-format stream-json --session-id abc "
            "--input-format stream-json"
        ),
    )
    monkeypatch.setattr(run013, "process_cwd", lambda pid: tmp_path)
    assert run013.worker_cwd_checked("claude", 424242) == tmp_path


def test_worker_cwd_name_matchers_track_the_pid_probes(run013) -> None:  # type: ignore[no-untyped-def]
    """The reuse guard must accept exactly what the pid probes accept."""

    for harness in ("claude", "pi", "codex"):
        assert run013.WORKER_CWD_PID_PROBES[harness].__name__.endswith("pids")
    assert run013.WORKER_CWD_NAME_MATCHERS["claude"](
        "claude --output-format stream-json --session-id x "
        "--input-format stream-json"
    )
    assert not run013.WORKER_CWD_NAME_MATCHERS["claude"](
        "claude daemon run --json-path /tmp/daemon.json"
    )
    assert not run013.WORKER_CWD_NAME_MATCHERS["claude"](
        "python -m hyprial.daemon.impl.mcp.entry agent-channel"
    )
    assert run013.WORKER_CWD_NAME_MATCHERS["pi"]("pi")
    assert run013.WORKER_CWD_NAME_MATCHERS["pi"]("pi --mode rpc")
    assert run013.WORKER_CWD_NAME_MATCHERS["codex"]("codex app-server --stdio")
    assert not run013.WORKER_CWD_NAME_MATCHERS["codex"]("vim app-server")
    assert run013.LARK_CHANNEL_ACTOR.startswith("adapter:lark:")


# -- ② lark preflight needs_refresh self-heal ----------------------------------


def _auth_status(token_status: str) -> dict:
    return {
        "verified": True,
        "identities": {
            "user": {"status": "ready", "tokenStatus": token_status}
        },
    }


def test_lark_user_valid_immediately_no_refresh_read(run013, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, ...]] = []

    def fake_lark_cli(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        return _auth_status("valid")

    monkeypatch.setattr(run013, "lark_cli", fake_lark_cli)
    run013.verify_lark_user()
    assert len(calls) == 1  # only auth status; no refresh trigger


def test_lark_user_needs_refresh_triggers_one_read_then_passes(run013, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, ...]] = []
    statuses = iter(["needs_refresh", "valid"])

    def fake_lark_cli(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        if args[:2] == ("auth", "status"):
            return _auth_status(next(statuses))
        return {"ok": True}

    monkeypatch.setattr(run013, "lark_cli", fake_lark_cli)
    run013.verify_lark_user()
    assert [args[:2] for args in calls].count(("auth", "status")) == 2
    assert any("chat-list" in " ".join(args) for args in calls)


def test_lark_user_permanent_needs_refresh_still_blocked(run013, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Mutation: the refresh never heals -> the refusal must survive."""

    def fake_lark_cli(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("auth", "status"):
            return _auth_status("needs_refresh")
        return {"ok": True}

    monkeypatch.setattr(run013, "lark_cli", fake_lark_cli)
    monkeypatch.setattr(
        run013, "_lark_reauth_check_summary", lambda: "reauth-summary-stub"
    )
    with pytest.raises(AssertionError, match="not ready"):
        run013.verify_lark_user()


def test_lark_user_ready_despite_lying_verified_field(run013, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#141: top-level `verified` reflects the default/auto identity, not the
    user identity.  user.status='ready' + tokenStatus='valid' must pass even
    when `verified` is False/absent."""

    def fake_lark_cli(*args: str, **kwargs: object) -> dict:
        return {
            "verified": False,
            "identities": {
                "user": {"status": "ready", "tokenStatus": "valid"}
            },
        }

    monkeypatch.setattr(run013, "lark_cli", fake_lark_cli)
    run013.verify_lark_user()


# -- ③ read-back retry semantics ------------------------------------------------


def test_readback_succeeds_after_two_failures(run013, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(run013.time, "sleep", lambda seconds: None)
    attempts = iter(
        [AssertionError("blip 1"), OSError("blip 2"), {"ok": True, "data": 1}]
    )

    def read() -> dict:
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    assert run013.lark_read_with_retry("probe-read", read) == {"ok": True, "data": 1}
    out = capsys.readouterr().out
    assert out.count("[readback-retry]") == 2  # every failed attempt logged


def test_readback_exhaustion_raises_with_attempt_log(run013, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Mutation: permanent read failure -> ReadbackExhausted, never silent."""

    monkeypatch.setattr(run013.time, "sleep", lambda seconds: None)

    def read() -> dict:
        raise OSError("api down")

    with pytest.raises(run013.ReadbackExhausted, match="all 3 attempts"):
        run013.lark_read_with_retry("probe-read", read)
    out = capsys.readouterr().out
    assert out.count("[readback-retry]") == 3


def test_readback_first_try_success_has_no_retry_log(run013, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(run013.time, "sleep", lambda seconds: None)
    assert run013.lark_read_with_retry("probe-read", lambda: {"ok": True}) == {
        "ok": True
    }
    assert "[readback-retry]" not in capsys.readouterr().out


def test_readback_is_a_probe_observation_error(run013) -> None:  # type: ignore[no-untyped-def]
    """The caller maps this class to BLOCKED; it must be distinguishable."""

    assert issubclass(run013.ReadbackExhausted, run013.ProbeObservationError)


# -- ③ send-receipt classification helpers --------------------------------------


def test_attachment_send_receipt_requires_route_delivery(run013, tmp_path) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "agent-image-nonce.png"
    source.write_bytes(b"png")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        f'{{"resourceDeliveries": [{{"source": "{source}"}}]}}\n', encoding="utf-8"
    )
    assert run013.attachment_send_receipt(transcript, source) is True
    transcript.write_text(f'{{"note": "{source}"}}\n', encoding="utf-8")
    assert run013.attachment_send_receipt(transcript, source) is False
    assert run013.attachment_send_receipt(None, source) is False


def test_reply_send_receipt_requires_tool_and_marker(run013, tmp_path) -> None:  # type: ignore[no-untyped-def]
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"tool": "harness_reply", "text": "E2E013_MARK nonce"}\n', encoding="utf-8"
    )
    assert run013.reply_send_receipt(transcript, "E2E013_MARK") is True
    transcript.write_text('{"text": "E2E013_MARK nonce"}\n', encoding="utf-8")
    assert run013.reply_send_receipt(transcript, "E2E013_MARK") is False
    transcript.write_text('{"tool": "harness_reply"}\n', encoding="utf-8")
    assert run013.reply_send_receipt(transcript, "E2E013_MARK") is False
    assert run013.reply_send_receipt(None, "E2E013_MARK") is False
