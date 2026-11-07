import threading
import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.processes.process_owner import own_process


def test_failed_native_stop_can_be_retried_but_detach_never_signals():
    class Process:
        pid = None
        running = True
        stops = 0

        def stop(self):
            self.stops += 1
            if self.stops == 1:
                raise OSError("transient stop failure")
            self.running = False

    raw = Process()
    owner = own_process(raw)
    with pytest.raises(OSError, match="transient stop failure"):
        owner.stop()
    owner.stop()
    assert raw.stops == 2
    second_raw = Process()
    second = own_process(second_raw)
    assert second.detach()
    assert second_raw.stops == 0
    with pytest.raises(RuntimeError):
        second.stop()


def test_observe_after_stop_uses_its_receipt_but_detach_still_rejects():
    class Process:
        pid = None
        running = True
        session_ref = "last-ref"

        def stop(self):
            self.running = False

    owner = own_process(Process())
    owner.stop()
    facts = owner.observe_facts()
    assert facts.running is False
    assert facts.session_ref == "last-ref"
    assert owner.detach()
    with pytest.raises(RuntimeError, match="closed"):
        owner.observe_facts()
    detached = own_process(Process())
    assert detached.detach()
    with pytest.raises(RuntimeError, match="closed"):
        detached.observe_facts()


def test_observe_does_not_hide_failed_stop():
    class Process:
        pid = None
        running = True

        def stop(self):
            raise OSError("stop failed")

    owner = own_process(Process())
    try:
        with pytest.raises(OSError, match="stop failed"):
            owner.stop()
        with pytest.raises(OSError, match="stop failed"):
            owner.observe_facts()
    finally:
        assert owner.detach()


def test_observe_does_not_replace_unrelated_runtime_failure_with_stop_facts(monkeypatch):
    class Process:
        pid = None
        running = True

        def stop(self):
            self.running = False

    owner = own_process(Process())
    owner.stop()

    def fail(_command):
        raise RuntimeError("unrelated runtime failure")

    monkeypatch.setattr(owner, "_submit", fail)
    with pytest.raises(RuntimeError, match="unrelated runtime failure"):
        owner.observe_facts()


def test_slow_driver_getter_never_blocks_owner_fact_reads():
    entered, release = threading.Event(), threading.Event()

    class Process:
        pid = None
        block = False
        alive = True

        @property
        def running(self):
            if self.block:
                entered.set()
                assert release.wait(3)
            return self.alive

        def stop(self):
            self.alive = False

    raw = Process()
    owner = own_process(raw)
    try:
        raw.block = True
        owner._poll()
        assert entered.wait(1)
        assert owner.running is True
        assert owner.pid is None
    finally:
        raw.block = False
        release.set()
        owner.stop()
    assert owner.running is False


def test_process_effects_have_one_owner_and_streaming_protocol_is_preserved():
    threads = []

    class Process:
        pid = None
        running = True

        def enqueue(self, delivery):
            threads.append(threading.get_ident())
            return True

        def drain_results(self):
            threads.append(threading.get_ident())
            return ()

        def interrupt(self, delivery_id, *, timeout=1):
            return True

        def stop(self):
            self.running = False

    owner = own_process(Process())
    try:
        assert owner.enqueue(None)
        assert owner.drain_results() == ()
        assert len(set(threads)) == 1
        assert threads[0] != threading.get_ident()
    finally:
        owner.stop()


@pytest.mark.parametrize("operation", ["enqueue", "drain_results", "drain_progress"])
def test_resource_transfer_waits_for_late_native_completion(operation):
    from hyprial.daemon.impl.processes.process_owner import StreamingProcessOwner

    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    returned, errors = [], []

    class Process:
        pid = None
        running = True
        calls = 0

        def transfer(self, *args):
            self.calls += 1
            entered.set()
            assert release.wait(3)
            return True if operation == "enqueue" else ("retained-result",)

        enqueue = drain_results = drain_progress = transfer

        def interrupt(self, *args, **kwargs):
            return True

        def stop(self):
            self.running = False

    native = Process()
    owner = StreamingProcessOwner(native, timeout=0.01)

    def invoke():
        try:
            method = getattr(owner, operation)
            returned.append(method(None) if operation == "enqueue" else method())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    caller = threading.Thread(target=invoke)
    caller.start()
    try:
        assert entered.wait(1)
        assert not done.wait(0.05)  # past the ordinary local call deadline
        assert not owner.detach(0.01)
        release.set()
        assert done.wait(1)
        assert errors == []
        assert returned == [True if operation == "enqueue" else ("retained-result",)]
        assert native.calls == 1
    finally:
        release.set()
        caller.join(2)
        assert owner.detach(2)


def test_post_transfer_probe_failure_cannot_discard_result_or_acceptance():
    from hyprial.kernel import ProcessLiveness, ProcessLivenessState
    from hyprial.daemon.impl.processes.process_owner import StreamingProcessOwner

    class Process:
        pid = None
        running = True
        probe_fails = False

        def liveness(self):
            if self.probe_fails:
                raise OSError("synthetic probe failure")
            return ProcessLiveness(ProcessLivenessState.UNKNOWN, observed=False)

        def enqueue(self, delivery):
            self.probe_fails = True
            return True

        def drain_results(self):
            return ("native-result",)

        def interrupt(self, *args, **kwargs):
            return True

        def stop(self):
            self.probe_fails = False
            self.running = False

    owner = StreamingProcessOwner(Process())
    try:
        assert owner.enqueue(None)
        assert owner.drain_results() == ("native-result",)
        assert owner.facts().liveness.state is ProcessLivenessState.UNKNOWN
        assert not owner.facts().liveness.observed
        assert owner.facts().liveness.detail == "process facts probe failed: OSError"
    finally:
        owner.stop()


def test_never_begun_transfer_is_explicitly_rejected_after_owner_restart(monkeypatch):
    import time

    from hyprial.kernel.impl.actor_runtime import ActorState
    from hyprial.daemon.impl.processes.process_owner import (
        EnqueueTurn,
        ProcessOperation,
        StreamingProcessOwner,
    )

    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    errors = []

    class Process:
        pid = None
        running = True
        enqueue_calls = 0

        def enqueue(self, _delivery):
            self.enqueue_calls += 1
            return True

        def drain_results(self):
            return ()

        def interrupt(self, *args, **kwargs):
            return True

        def stop(self):
            self.running = False

    class CrashableOwner(StreamingProcessOwner):
        def _receive(self, event):  # type: ignore[no-untyped-def]
            if event == "crash-before-next-envelope":
                entered.set()
                assert release.wait(2)
                raise RuntimeError("synthetic actor crash")
            return super()._receive(event)

    native = Process()
    owner = CrashableOwner(native, capacity=1)
    assert owner._runtime.tell(
        owner._handle, "crash-before-next-envelope"
    ) is AdmissionResult.ACCEPTED
    assert entered.wait(1)

    admitted = threading.Event()
    tell = owner._runtime.tell

    def observe_admission(handle, command):
        result = tell(handle, command)
        if (
            isinstance(command, ProcessOperation)
            and isinstance(command.command, EnqueueTurn)
            and result is AdmissionResult.ACCEPTED
        ):
            admitted.set()
        return result

    monkeypatch.setattr(owner._runtime, "tell", observe_admission)

    def invoke() -> None:
        try:
            owner.enqueue(None)
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    caller = threading.Thread(target=invoke)
    caller.start()
    try:
        # Thread.start does not prove the envelope belongs to the old mailbox.
        assert admitted.wait(1), "enqueue was not admitted before the crash"
        release.set()
        assert done.wait(1)
        assert len(errors) == 1
        assert "ACTOR_RESTARTED_UNDELIVERED" in str(errors[0])
        assert native.enqueue_calls == 0
        # Opposite order: a fresh envelope admitted after restart must execute.
        deadline = time.monotonic() + 2
        while True:
            snapshot = owner._runtime.snapshot(owner._handle)
            if snapshot.generation > 1 and snapshot.state is ActorState.RUNNING:
                break
            assert time.monotonic() < deadline, snapshot
            time.sleep(0.005)
        assert owner.enqueue(None) is True
        assert native.enqueue_calls == 1
    finally:
        release.set()
        caller.join(2)
        assert owner.detach(2)
