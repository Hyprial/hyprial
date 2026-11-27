from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.kernel import AdmissionResult, ChannelConfiguration

from ..support import _Launcher


@pytest.mark.parametrize(
    "admissions",
    [
        (AdmissionResult.CLOSED,),
        (AdmissionResult.OVERLOADED, AdmissionResult.CLOSED),
        (AdmissionResult.OVERLOADED, AdmissionResult.ACCEPTED),
    ],
)
def test_shutdown_completion_releases_pending_after_terminal_admission(
    admissions: tuple[AdmissionResult, ...],
) -> None:
    runtime = AdapterRuntime(_Launcher(), ChannelConfiguration(gateways=()))
    closed_cell = runtime._cell
    assert runtime.stop() is True
    entered = threading.Event()
    release = threading.Event()
    received = []
    future: Future[None] = Future()
    correlation = "closed-shutdown-completion"
    runtime._authority.put_pending(correlation, future)

    class CompletionMailbox:
        def tell(self, result: object) -> AdmissionResult:
            received.append(result)
            entered.set()
            assert release.wait(3.0)
            admission = admissions[min(len(received), len(admissions)) - 1]
            if admission is AdmissionResult.CLOSED:
                assert closed_cell.tell(result) is AdmissionResult.CLOSED
            return admission

    runtime._cell = CompletionMailbox()
    try:
        assert runtime._effects.submit(
            correlation_id=correlation,
            generation=runtime.generation,
            version=0,
            name="*",
            operation="shutdown",
            payload=time.monotonic() + 2.0,
        ) is True
        assert entered.wait(1.0)
        assert runtime._effects.pending() == 1
        runtime._cell = None
        release.set()

        assert runtime._effects.close(timeout=2.0) is True
        assert runtime._effects.pending() == 0
        assert len(received) == len(admissions)
        retained = runtime._authority.retained_completions()
        if admissions[-1] is AdmissionResult.CLOSED:
            assert retained == (received[-1],)
            assert retained[0].correlation_id == correlation
            assert retained[0].operation == "shutdown"
            with pytest.raises(RuntimeError, match="could not enter the actor mailbox"):
                future.result(timeout=0)
        else:
            assert retained == ()
            assert not future.done()
    finally:
        release.set()
        runtime._cell = None
        assert runtime._effects.close(timeout=6.0) is True
        runtime._authority.pop_pending(correlation)
