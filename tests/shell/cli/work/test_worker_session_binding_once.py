"""One command operation resolves one worker session binding snapshot."""

from __future__ import annotations

import pytest

from hyprial.identity import WorkerSessionBinding
from hyprial.shell.impl.cli.commands.common.support import _message_claim_params
import hyprial.shell.impl.cli.runtime.entry as entry


def test_resolved_binding_is_reused_without_a_second_read(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = WorkerSessionBinding("agent:o:n:worker", "session-1")
    monkeypatch.setattr(
        entry,
        "worker_session_binding",
        lambda: (_ for _ in ()).throw(AssertionError("must not re-read a supplied binding")),
    )

    params = _message_claim_params("agent:o:n:worker", binding=binding)

    assert params["sessionRef"] == "session-1"


def test_lazy_callers_still_resolve_once(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = WorkerSessionBinding("agent:o:n:worker", "session-1")
    calls: list[int] = []
    monkeypatch.setattr(
        entry,
        "worker_session_binding",
        lambda: calls.append(1) or binding,
    )

    params = _message_claim_params("agent:o:n:worker")

    assert params["sessionRef"] == "session-1"
    assert calls == [1]
