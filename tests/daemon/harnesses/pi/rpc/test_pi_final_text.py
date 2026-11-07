"""A current empty final must not turn older session text into a reply."""
from __future__ import annotations

import pytest

from hyprial.daemon.impl.api import HarnessResultStatus
from hyprial.daemon.impl.harnesses.pi.messages import _final_assistant_text

from .process.support import delivery, fake_process, wait_for_results


@pytest.mark.parametrize("last", [
    {"role": "assistant", "content": []},
    {"role": "assistant", "content": [{"type": "thinking", "thinking": "work"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "  \n\t"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "partial error"}],
     "stopReason": "error"},
    {"role": "assistant", "content": [{"type": "text", "text": "partial abort"}],
     "stopReason": "aborted"},
])
def test_only_the_actual_last_assistant_can_supply_reply_text(last) -> None:
    assert _final_assistant_text([
        {"role": "assistant", "content": [{"type": "text", "text": "old"}]},
        last,
    ]) is None


def test_nonempty_last_text_is_preserved_verbatim() -> None:
    assert _final_assistant_text([
        {"role": "assistant", "content": [{"type": "text", "text": "old"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "  final\n"}]},
        {"role": "toolResult", "content": [{"type": "text", "text": "ignored"}]},
    ]) == "  final\n"


@pytest.mark.parametrize("mode", ["empty-last", "whitespace-last", "no-text-whitespace"])
def test_empty_current_final_never_recovers_stale_session_text(mode: str) -> None:
    # The fake's session-text fallback deliberately returns a nonempty old value.
    process = fake_process(mode)
    try:
        assert process.enqueue(delivery("delivery-empty-last", "current prompt"))
        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.FAILED
        assert "no final assistant text" in (result.error or "")
    finally:
        process.stop()
