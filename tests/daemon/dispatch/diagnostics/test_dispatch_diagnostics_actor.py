from __future__ import annotations

from hyprial.daemon.impl.dispatch.runtime.diagnostics import DispatchDiagnostics


def test_conversation_opener_and_counters_have_one_owner() -> None:
    owner = DispatchDiagnostics()
    assert owner.open_conversation("thread-a", "send-1")
    assert owner.open_conversation("thread-a", "send-1")
    assert not owner.open_conversation("thread-a", "send-2")
    owner.classify(True)
    owner.classify(False)
    projection = owner.projection()
    assert projection.without_pac == 1
    assert projection.conversation == 1
    assert projection.known_conversations == 1
    assert owner.close(2)
