"""Unit seams for E2E-012's Codex thread lifecycle assertion."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "contract" / "e2e" / "run-codex-worker.py"
SCENARIOS = RUNNER.parent


def _runner():  # type: ignore[no-untyped-def]
    sys.path.insert(0, str(SCENARIOS))
    try:
        spec = importlib.util.spec_from_file_location("codex_worker_runner", RUNNER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        return module
    finally:
        sys.path.remove(str(SCENARIOS))


def test_e2e012_reads_codex_resume_from_the_capability_table() -> None:
    runner = _runner()

    assert runner.codex_session_resume_supported() is True


def test_e2e012_resume_branch_requires_the_same_persisted_thread() -> None:
    runner = _runner()
    rollout = Path("rollout-x-thread-b.jsonl")

    detail = runner.evaluate_thread_lifecycle(
        resume_supported=True,
        persisted_ref="thread-a",
        thread_before="thread-a",
        thread_after="thread-a",
        rollout=rollout,
    )
    assert "resumed persisted app-server thread thread-a" in detail

    with pytest.raises(AssertionError, match="does not match"):
        runner.evaluate_thread_lifecycle(
            resume_supported=True,
            persisted_ref="someone-else",
            thread_before="thread-a",
            thread_after="someone-else",
            rollout=rollout,
        )
    with pytest.raises(AssertionError, match="did not resume"):
        runner.evaluate_thread_lifecycle(
            resume_supported=True,
            persisted_ref="thread-a",
            thread_before="thread-a",
            thread_after="thread-b",
            rollout=rollout,
        )


def test_e2e012_fresh_branch_survives_when_resume_is_not_supported() -> None:
    runner = _runner()
    rollout = Path("rollout-x-thread-b.jsonl")

    detail = runner.evaluate_thread_lifecycle(
        resume_supported=False,
        persisted_ref=None,
        thread_before="thread-a",
        thread_after="thread-b",
        rollout=rollout,
    )
    assert "fresh app-server thread thread-a->thread-b" in detail

    with pytest.raises(AssertionError, match="reused app-server thread"):
        runner.evaluate_thread_lifecycle(
            resume_supported=False,
            persisted_ref=None,
            thread_before="thread-a",
            thread_after="thread-a",
            rollout=rollout,
        )
    with pytest.raises(AssertionError, match="carries sessionRef"):
        runner.evaluate_thread_lifecycle(
            resume_supported=False,
            persisted_ref="thread-a",
            thread_before="thread-a",
            thread_after="thread-b",
            rollout=rollout,
        )


def test_e2e012_desired_session_ref_parser(tmp_path: Path) -> None:
    runner = _runner()
    state = tmp_path / "state"
    state.mkdir()
    from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
    from hyprial.kernel import HarnessLaunchSpec

    # The runner reads the desired-state AUTHORITY through its store (#386:
    # JSON on dev, SQLite on intg), so the fixture is written through the same
    # store -- a hand-written minimal file is not a document the store
    # accepts ("schema v1 requires providers and interactiveSessions arrays")
    # and the runner would report "fresh expected" for the wrong reason.
    DesiredStateStore(state / "desired-state.json").save(
        DesiredState(
            harnesses=(
                HarnessLaunchSpec(
                    harness="codex",
                    name=runner.WORKER_NAME,
                    headless=True,
                    args=(),
                    ownership="managed",
                    session_ref="thread-a",
                ),
            )
        )
    )

    assert runner.desired_codex_session_ref(tmp_path) == "thread-a"


# The daemon's attempt-failure notice, verbatim from a local E2E-006 codex
# run on dev 6255d635: intent=reply, same conversation, no marker.
FAILURE_NOTICE = (
    "⚠️ 你等待的请求没有拿到回复:provider 侧的这次尝试失败了。\n"
    "- worker: agent:e2e:resident-probe-node:resident-codex-worker\n"
    "- harness: codex\n- 失败原因: HARNESS_TRANSIENT_FAILURE\n"
    "- 尝试: 第 1/3 次(将重试)\n这不是成功回复;该请求本身仍未完成。"
)


def _pending(message: str, *, message_id: str = "reply-1") -> dict[str, str]:
    # Hard cut (send links): pending rows carry the summary, never the body.
    return {"messageId": message_id, "conversationId": "e2e012-N1", "summary": message}


def test_e2e012_accepts_the_correlated_reply_carrying_the_marker() -> None:
    runner = _runner()
    items = [_pending("sent", message_id="request"), _pending("E2E012_CODEX_OK")]

    reply = runner.marker_reply(
        items, conversation="e2e012-N1", request_id="request", marker=runner.MARKER
    )
    assert reply is not None and reply["messageId"] == "reply-1"


def test_e2e012_never_accepts_a_failure_notice_as_the_reply() -> None:
    """Negative control: the notice alone is what nightly E2E-012 accepted
    for seven nights while codex could not reach its model."""

    runner = _runner()
    items = [_pending("sent", message_id="request"), _pending(FAILURE_NOTICE)]

    assert runner.marker_reply(
        items, conversation="e2e012-N1", request_id="request", marker=runner.MARKER
    ) is None


def test_e2e012_marker_in_another_conversation_or_the_request_does_not_count() -> None:
    runner = _runner()
    items = [
        {"messageId": "request", "conversationId": "e2e012-N1", "message": "E2E012_CODEX_OK"},
        {"messageId": "other", "conversationId": "e2e012-N2", "message": "E2E012_CODEX_OK"},
    ]

    assert runner.marker_reply(
        items, conversation="e2e012-N1", request_id="request", marker=runner.MARKER
    ) is None
