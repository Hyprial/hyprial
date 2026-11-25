"""Completed effects remain recoverable when actor mailbox admission fails."""

import pytest
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from hyprial.daemon.impl.adapters.lark.runtime.commands import _IoResult
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.kernel import AdmissionResult, ChannelConfiguration
from tests.daemon.adapters.lark.lifecycle.test_lark_lifecycle_guards import _Launcher, _gateway


class _RejectCompletion:
    def __init__(self, cell):
        self.cell = cell
        self.reject = True
        self.rejected = threading.Event()

    def tell(self, command):
        if self.reject and isinstance(command, _IoResult):
            self.rejected.set()
            return AdmissionResult.OVERLOADED
        return self.cell.tell(command)

    def __getattr__(self, name):
        return getattr(self.cell, name)


@pytest.mark.parametrize("operation", ["ordinary", "reserved", "failed"])
def test_completed_stop_recovers_without_replaying_the_effect(operation):
    launcher = _Launcher()
    runtime = AdapterRuntime(launcher, ChannelConfiguration(gateways=(_gateway("app"),)))
    blocked = None
    original_stop = None
    try:
        runtime.start("app")
        process = launcher.processes[0]
        original_stop = process.stop
        stops = []

        def stop(timeout=5):
            stops.append(timeout)
            if operation == "failed":
                raise RuntimeError("synthetic stop failed")
            original_stop(timeout)

        process.stop = stop
        blocked = _RejectCompletion(runtime._cell)
        runtime._cell = blocked
        with pytest.raises(RuntimeError, match="completion custody"):
            if operation == "ordinary":
                runtime.remove("app")
            else:
                runtime.begin_removal("app", "proof")
        blocked.reject = False
        assert len(stops) == 1
        if operation == "failed":
            with pytest.raises(RuntimeError, match="not stopped"):
                runtime.finish_removal("app", "proof", committed=True)
            runtime.finish_removal("app", "proof", committed=False)
            assert runtime.gateway_names == ("app",)
            assert process.running
        else:
            if operation == "ordinary":
                runtime.begin_removal("app", "proof")
            runtime.finish_removal("app", "proof", committed=True)
            assert runtime.gateway_names == ()
        assert len(stops) == 1
        assert not runtime._authority.lifecycle_effects
    finally:
        if blocked is not None:
            blocked.reject = False
        if original_stop is not None:
            launcher.processes[0].stop = original_stop
        runtime.stop()


def test_late_undelivered_stop_recovers_after_actor_generation_restart():
    launcher = _Launcher()
    runtime = AdapterRuntime(launcher, ChannelConfiguration(gateways=(_gateway("app"),)))
    blocked = None
    try:
        runtime.start("app")
        blocked = _RejectCompletion(runtime._cell)
        runtime._cell = blocked
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(runtime.begin_removal, "app", "proof")
            assert blocked.rejected.wait(3)
            assert blocked.cell.tell(object()) is AdmissionResult.ACCEPTED
            with pytest.raises(RuntimeError, match="generation restarted"):
                pending.result(timeout=3)
        deadline = time.monotonic() + 7
        while not runtime._authority.retained_completions():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        blocked.reject = False
        runtime.finish_removal("app", "proof", committed=True)
        assert runtime.generation > 1
        assert runtime.gateway_names == ()
        assert launcher.calls == 1
        assert launcher.processes[0].stopped
        assert not runtime._authority.lifecycle_effects
        assert not runtime._authority.retained_completions()
    finally:
        if blocked is not None:
            blocked.reject = False
        runtime.stop()
