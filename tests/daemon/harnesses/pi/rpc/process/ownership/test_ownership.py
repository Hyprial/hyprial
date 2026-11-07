from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.api import HarnessResultStatus
from hyprial.daemon.impl.harnesses.pi import rpc_client as pi_rpc
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
from hyprial.daemon.impl.harnesses.streaming import StreamingTurnProcess
from hyprial.daemon.impl.harnesses.streaming import process as streaming_process
from hyprial.daemon.impl.harnesses.streaming.protocol import _OBSERVER_STOP

from ..support import (
    FAKE_PI,
    SlowQueryPiRpcClient,
    _owned_pipe_fds,
    _prompt,
    _reaped_process_with,
    delivery,
    fake_process,
    spec,
    wait_for_results,
)


class _HeldEnterClient:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    async def __aenter__(self) -> "_HeldEnterClient":
        self._entered.set()
        assert await asyncio.to_thread(self._release.wait, 5.0)
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):  # type: ignore[no-untyped-def]
        if False:
            yield None

    async def interrupt(self) -> None:
        return None


def _failed_stop_process(
    thread_name: str,
) -> tuple[StreamingTurnProcess, threading.Event, threading.Event]:
    entered = threading.Event()
    release = threading.Event()
    process = StreamingTurnProcess(
        harness="pi",
        label="failed-stop pi RPC",
        client_factory=lambda: _HeldEnterClient(entered, release),
        thread_name=thread_name,
        stop_timeout_seconds=0.01,
        force_stop=lambda: None,
        force_stopped=lambda: False,
        force_stop_join_seconds=0.01,
        on_turn_failure=lambda _failure: None,
    )
    assert entered.wait(timeout=2.0)
    return process, entered, release


def _cleanup_failed_stop(process: StreamingTurnProcess, release: threading.Event) -> None:
    release.set()
    process._thread.join(timeout=2.0)
    observer = process._observer_thread
    if observer is not None and observer.is_alive():
        with process._observer_admission_lock:
            process._observer_stop_requested.set()
            assert process._observer_queue is not None
            try:
                process._observer_queue.put_nowait(_OBSERVER_STOP)
            except queue.Full:
                pass
        observer.join(timeout=2.0)
    process._turn_runtime.drain(2.0)


def _owned_auxiliary_threads(
    process: StreamingTurnProcess, before: set[threading.Thread]
) -> tuple[threading.Thread, ...]:
    observer = process._observer_thread
    relay = process._turn_runtime._relay._thread
    runtime_actors = tuple(
        thread
        for thread in threading.enumerate()
        if thread not in before and thread.name.startswith("_RuntimeActor-")
    )
    assert observer is not None and observer not in before
    assert relay not in before
    assert runtime_actors
    return (observer, relay, *runtime_actors)


def test_failed_native_stop_still_reclaims_every_auxiliary_thread() -> None:
    before = set(threading.enumerate())
    process, _entered, release = _failed_stop_process("failed-stop-owned-threads")
    owned = _owned_auxiliary_threads(process, before)
    try:
        with pytest.raises(RuntimeError, match="did not stop after forced cleanup"):
            process.stop()

        assert all(not thread.is_alive() for thread in owned)
    finally:
        _cleanup_failed_stop(process, release)


def test_idle_failure_observer_blocks_until_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingQueue(queue.Queue):
        def __init__(self, maxsize: int = 0) -> None:
            super().__init__(maxsize)
            self.get_calls = 0

        def get(self, *args: object, **kwargs: object) -> object:
            self.get_calls += 1
            return super().get(*args, **kwargs)

    monkeypatch.setattr(streaming_process.queue, "Queue", CountingQueue)
    client_release = threading.Event()
    client_release.set()
    process = StreamingTurnProcess(
        harness="pi",
        label="idle-observer pi RPC",
        client_factory=lambda: _HeldEnterClient(threading.Event(), client_release),
        thread_name="idle-observer",
        on_turn_failure=lambda _failure: None,
    )
    observer_queue = process._observer_queue
    assert isinstance(observer_queue, CountingQueue)
    try:
        time.sleep(0.25)
        assert observer_queue.get_calls <= 2
    finally:
        # This process never enters its client, so its ordinary close command
        # wakes the pump and the observer sentinel wakes the auxiliary thread.
        process.stop()
    assert process._observer_thread is not None
    assert not process._observer_thread.is_alive()


def test_twenty_failed_stop_cycles_keep_thread_growth_to_one_generation() -> None:
    generations: list[tuple[threading.Thread, ...]] = []
    processes: list[tuple[StreamingTurnProcess, threading.Event]] = []
    try:
        for _index in range(20):
            before = set(threading.enumerate())
            process, _entered, release = _failed_stop_process(
                "same-harness-failed-stop"
            )
            processes.append((process, release))
            generations.append(_owned_auxiliary_threads(process, before))
            with pytest.raises(
                RuntimeError, match="did not stop after forced cleanup"
            ):
                process.stop()
            release.set()
            process._thread.join(timeout=2.0)

        leaked = [
            thread
            for generation in generations
            for thread in generation
            if thread.is_alive()
        ]
        assert len(leaked) <= len(generations[-1])
    finally:
        for process, release in processes:
            _cleanup_failed_stop(process, release)


def test_length_stop_without_text_is_context_exhausted(tmp_path: Path) -> None:
    command_log = tmp_path / "commands.jsonl"
    process = fake_process("context-exhausted-no-compact", command_log=command_log)
    try:
        assert process.enqueue(delivery("delivery-context", "large turn"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.FAILED
        assert result.failure_code == "CONTEXT_EXHAUSTED"
        assert result.error is not None
        assert "stopReason=length" in result.error
        assert "usage(input=812" in result.error
        assert "output=0" in result.error
        assert "cacheRead=997828" in result.error
        commands = [
            json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()
        ]
        assert [item["type"] for item in commands].count("compact") == 1
        assert [item["type"] for item in commands].count("prompt") == 1
    finally:
        process.stop()


def test_an_ordinary_length_stop_with_text_completes_without_compaction(
    tmp_path: Path,
) -> None:
    # The reverse guard: stopReason=length alone is not exhaustion.  A
    # truncated answer that carries text is a reply, not a failure.
    command_log = tmp_path / "commands.jsonl"
    process = fake_process("length-with-text", command_log=command_log)
    try:
        assert process.enqueue(delivery("delivery-length", "long answer"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED, result.error
        assert result.output == _prompt("long answer partial")
        commands = [
            json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()
        ]
        assert [item["type"] for item in commands].count("compact") == 0
        assert [item["type"] for item in commands].count("prompt") == 1
    finally:
        process.stop()


def test_blocking_extension_ui_dialog_is_cancelled_not_deadlocked() -> None:
    process = fake_process("ui-dialog")
    try:
        assert process.enqueue(delivery("delivery-1", "with dialog"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == "dialog cancelled=true"
    finally:
        process.stop()


def test_stop_during_query_latches_abort_until_prompt_is_accepted(
    tmp_path: Path,
) -> None:
    """#84 D1: stop must not let pi see abort before prompt."""

    command_log = tmp_path / "commands.jsonl"
    launch = spec(session_ref="stable-session")
    clients: list[SlowQueryPiRpcClient] = []

    def factory() -> SlowQueryPiRpcClient:
        client = SlowQueryPiRpcClient(
            launch,
            "stable-session",
            command=(sys.executable, str(FAKE_PI)),
            env={"FAKE_PI_MODE": "abort", "FAKE_PI_LOG": str(command_log)},
            startup_timeout_seconds=10.0,
        )
        clients.append(client)
        return client

    process = PiRpcProcess(launch, client_factory=factory, reconnect_delay_seconds=0.01)
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-stop-race", "stop me"))
        (client,) = clients
        assert client.query_entered.wait(timeout=5.0)

        process.stop()

        assert not process._thread.is_alive()
        [result] = process.drain_results()
        assert result.delivery_id == "delivery-stop-race"
        assert result.status is HarnessResultStatus.INTERRUPTED
        commands = [
            json.loads(line)
            for line in command_log.read_text(encoding="utf-8").splitlines()
        ]
        kinds = [item["type"] for item in commands]
        prompt_index = kinds.index("prompt")
        assert "abort" not in kinds[:prompt_index]
        assert kinds.index("abort", prompt_index) > prompt_index
    finally:
        if process._thread.is_alive():
            pid = process.pid
            if pid is not None:
                os.kill(pid, 9)
            process._thread.join(timeout=2.0)


def test_stop_without_force_hook_raises_instead_of_silently_leaving_thread() -> None:
    """#84 D2: a bounded stop that failed must not report success."""

    entered = threading.Event()
    release = threading.Event()

    class BlockedClient:
        async def __aenter__(self) -> "BlockedClient":
            entered.set()
            assert await asyncio.to_thread(release.wait, 2.0)
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self):  # type: ignore[no-untyped-def]
            if False:
                yield None

        async def interrupt(self) -> None:
            return None

    process = StreamingTurnProcess(
        harness="pi",
        label="test pi RPC",
        client_factory=BlockedClient,
        thread_name="test-pi-stop-without-force",
        stop_timeout_seconds=0.01,
    )
    try:
        assert entered.wait(timeout=2.0)
        with pytest.raises(RuntimeError, match="no force-stop hook"):
            process.stop()
        assert process._thread.is_alive()
    finally:
        release.set()
        process._thread.join(timeout=2.0)


def test_pi_stop_force_terminates_an_unresponsive_child() -> None:
    """#84 D2: Pi follows the Codex force-stop contract after bounded wait."""

    process = fake_process("hang")
    child_pid: int | None = None
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-hang", "never finish"))
        deadline = time.monotonic() + 5.0
        while process._active_delivery_id is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child_pid = process.pid
        assert child_pid is not None

        process.stop()

        assert not process._thread.is_alive()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process._thread.is_alive():
            pid = child_pid if child_pid is not None else process.pid
            if pid is not None:
                os.kill(pid, 9)
            process._thread.join(timeout=2.0)


def test_stop_fences_spawned_not_ready_client_and_closes_exact_three_pipes() -> None:
    """The full-suite race: spawn completed, ready probe did not, stop now."""

    clients: list[PiRpcClient] = []
    created = threading.Event()
    launch = spec(session_ref="stable-session")

    def factory() -> PiRpcClient:
        client = PiRpcClient(
            launch,
            "stable-session",
            command=(sys.executable, str(FAKE_PI)),
            env={"FAKE_PI_MODE": "startup-hang"},
            startup_timeout_seconds=30.0,
        )
        clients.append(client)
        created.set()
        return client

    process = PiRpcProcess(launch, client_factory=factory)
    assert created.wait(timeout=2.0)
    assert clients[0]._spawned.wait(timeout=2.0)
    assert process.dispatch_ready is False
    pipe_fds = _owned_pipe_fds(clients[0])

    process.stop()
    process.stop()

    assert not process._thread.is_alive()
    assert all(thread.name != "hyprial-pi-rpc-pi-worker" for thread in threading.enumerate())
    for fd in pipe_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_ready_probe_failure_closes_process_transport_before_enter_raises() -> None:
    """A failed __aenter__ has no async-with __aexit__; cleanup is internal."""

    client = PiRpcClient(
        spec(),
        "dead-session",
        command=(sys.executable, str(FAKE_PI)),
        env={
            "FAKE_PI_MODE": "reject-session",
            "FAKE_PI_DEAD_ID": "dead-session",
        },
        startup_timeout_seconds=2.0,
    )

    async def scenario() -> None:
        with pytest.raises(ConnectionError, match="exited unexpectedly"):
            await client.__aenter__()

    asyncio.run(scenario())
    assert client._process is None
    assert client._process_protocol is None
    assert client._stderr_task is None
    assert client._exit_task is None


def test_immediate_stop_full_sequence_releases_every_pump_thread_and_pipe() -> None:
    """Repeat the original worker-channel construction/stop sequence exactly."""

    launch = spec(session_ref="stable-session")
    for index in range(12):
        clients: list[PiRpcClient] = []
        created = threading.Event()

        def factory() -> PiRpcClient:
            client = PiRpcClient(
                launch,
                "stable-session",
                command=(sys.executable, str(FAKE_PI)),
                env={"FAKE_PI_MODE": "settled"},
                startup_timeout_seconds=2.0,
            )
            clients.append(client)
            created.set()
            return client

        process = PiRpcProcess(
            launch,
            client_factory=factory,
        )
        assert created.wait(timeout=2.0), f"iteration {index} did not construct"
        assert clients[0]._spawned.wait(timeout=2.0), f"iteration {index} did not spawn"
        process.stop()
        assert not process._thread.is_alive()


def test_close_reaches_force_escalation_when_stdin_never_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck pipe must degrade to a dirty close, not hang the caller.

    ``stdin.wait_closed()`` runs BEFORE the terminate/kill escalation, so an
    unbounded await there means force-close is never reached at all: the
    reviewer's probe recorded CLOSE_TIMED_OUT with TERMINATE_REACHED and
    KILL_REACHED both false.  Every step in the close path is now bounded
    separately, so a step that never completes still hands control to the one
    after it.
    """

    monkeypatch.setattr(pi_rpc, "_CLOSE_STEP_SECONDS", 0.05)
    reached: list[str] = []

    class NeverClosingStdin:
        def close(self) -> None:
            reached.append("stdin_close")

        async def wait_closed(self) -> None:
            await asyncio.Event().wait()  # the child never drains stdin

    class EmptyStream:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _n: int = -1) -> bytes:
            return b""

    class StuckProcess:
        returncode = None
        stdin = NeverClosingStdin()
        stdout = EmptyStream()
        stderr = EmptyStream()

        async def wait(self) -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def terminate(self) -> None:
            reached.append("terminate")

        def kill(self) -> None:
            reached.append("kill")

    client = PiRpcClient(
        spec(), "stuck-session", command=(sys.executable, str(FAKE_PI))
    )
    client._process = StuckProcess()  # type: ignore[assignment]
    client._process_protocol = None
    client._stderr_task = None
    client._exit_task = None

    async def scenario() -> None:
        # The assertion is that this returns at all; without the bound it
        # never does.
        await asyncio.wait_for(client._close(), timeout=5.0)

    asyncio.run(scenario())

    assert reached == ["stdin_close", "terminate", "kill"]
    # This child never reaps: returncode stays None through kill, so custody
    # must survive the close. The assertion here used to be
    # ``client._process is None``, which pinned the defect instead of the
    # contract -- clearing the handle let force_stopped() report a clean stop
    # off ``_process is None`` while the child was still alive holding its
    # pipes, telling the caller the worker had stopped when it had not.
    assert client._process is not None
    assert client.force_stopped() is False
    assert client.running is True


def test_close_barrier_timeout_is_actually_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An observation point that never fires is not an observation point.

    The bounded close path degrades to a dirty close; that degradation is only
    useful if it is visible, so assert the event really reaches the logger
    rather than trusting that the branch exists.
    """

    monkeypatch.setattr(pi_rpc, "_CLOSE_STEP_SECONDS", 0.05)
    logged: list[tuple[str, dict[str, object]]] = []

    class CapturingLogger:
        def log(self, _level: str, event: str, **fields: object) -> None:
            logged.append((event, fields))

    class NeverClosingStdin:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            await asyncio.Event().wait()

    class EmptyStream:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _n: int = -1) -> bytes:
            return b""

    class StuckProcess:
        returncode = None
        stdin = NeverClosingStdin()
        stdout = EmptyStream()
        stderr = EmptyStream()

        async def wait(self) -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    client = PiRpcClient(
        spec(), "logged-session", command=(sys.executable, str(FAKE_PI))
    )
    client._logger = CapturingLogger()  # type: ignore[assignment]
    client._process = StuckProcess()  # type: ignore[assignment]
    client._process_protocol = None
    client._stderr_task = None
    client._exit_task = None

    asyncio.run(asyncio.wait_for(client._close(), timeout=5.0))

    timeouts = [
        fields.get("step")
        for event, fields in logged
        if event == "worker.close_barrier_timeout"
    ]
    assert "stdin_wait_closed" in timeouts, logged
    assert "wait_after_kill" in timeouts, logged


def test_close_total_budget_fits_inside_the_caller_stop_window() -> None:
    """Per-step ceilings are not a total ceiling.

    The close path has six to eight bounded awaits. At 1s each, step limits
    alone permit ~8s, while StreamingTurnProcess.stop() joins this thread
    with stop_timeout_seconds (2.0s by default). A stuck close then surfaces
    to the operator as "did not stop within 2.0s and has no force-stop hook"
    reported by the caller as a stop failure rather than as a slow close.
    """

    import inspect

    from hyprial.daemon.impl.harnesses.pi import rpc_client as pi_rpc
    from hyprial.daemon.impl.harnesses.streaming import StreamingTurnProcess

    caller_window = inspect.signature(
        StreamingTurnProcess.__init__
    ).parameters["stop_timeout_seconds"].default

    # Checked as arithmetic, not as "is it conservative enough": raising
    # either constant without re-checking the other fails here.
    assert pi_rpc._CLOSE_TOTAL_SECONDS < caller_window
    assert (
        caller_window - pi_rpc._CLOSE_TOTAL_SECONDS
        >= pi_rpc._CLOSE_CALLER_MARGIN_SECONDS
    ), "not enough slack left for the caller's join and scheduler jitter"
    assert pi_rpc._CLOSE_STEP_SECONDS <= pi_rpc._CLOSE_TOTAL_SECONDS


def test_a_stuck_close_stays_within_the_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every step stuck must still return inside the total budget."""

    from hyprial.daemon.impl.harnesses.pi import rpc_client as pi_rpc

    reached: list[str] = []

    class NeverClosingStdin:
        def close(self) -> None:
            reached.append("stdin_close")

        async def wait_closed(self) -> None:
            await asyncio.Event().wait()

    class EmptyStream:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _n: int = -1) -> bytes:
            return b""

    class StuckProcess:
        returncode = None
        stdin = NeverClosingStdin()
        stdout = EmptyStream()
        stderr = EmptyStream()

        async def wait(self) -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def terminate(self) -> None:
            reached.append("terminate")

        def kill(self) -> None:
            reached.append("kill")

    monkeypatch.setattr(pi_rpc, "_CLOSE_STEP_SECONDS", 0.05)
    monkeypatch.setattr(pi_rpc, "_CLOSE_TOTAL_SECONDS", 0.12)

    client = PiRpcClient(
        spec(), "budget-session", command=(sys.executable, str(FAKE_PI))
    )
    client._process = StuckProcess()  # type: ignore[assignment]
    client._process_protocol = None
    client._stderr_task = None
    client._exit_task = None

    started = time.monotonic()
    asyncio.run(asyncio.wait_for(client._close(), timeout=5.0))
    elapsed = time.monotonic() - started

    # Generous slack for scheduling; the point is that it is bounded by the
    # total budget rather than by step_ceiling * step_count (~0.3s here).
    assert elapsed < 0.30, f"close overran its total budget: {elapsed:.3f}s"
    assert client.force_stopped() is False


def test_unsettled_custody_reaches_the_public_force_stopped_check() -> None:
    """The layer the product actually consults must see the unsettled close.

    Proving custody survives on ``client._close()`` alone would repeat the
    mistake TURNWAIT exposed: a test driving an entry point the product does
    not use. StreamingTurnProcess.stop() never touches _process; it calls the
    force_stopped hook, which for pi is PiRpcProcess._force_stopped_client().
    That chain is what must report "not stopped" while a child is alive.
    """

    reached: list[str] = []

    class NeverClosingStdin:
        def close(self) -> None:
            reached.append("stdin_close")

        async def wait_closed(self) -> None:
            await asyncio.Event().wait()

    class EmptyStream:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _n: int = -1) -> bytes:
            return b""

    class StuckProcess:
        returncode = None
        stdin = NeverClosingStdin()
        stdout = EmptyStream()
        stderr = EmptyStream()

        async def wait(self) -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def terminate(self) -> None:
            reached.append("terminate")

        def kill(self) -> None:
            reached.append("kill")

    client = PiRpcClient(
        spec(), "public-check", command=(sys.executable, str(FAKE_PI))
    )
    client._process = StuckProcess()  # type: ignore[assignment]
    client._process_protocol = None
    client._stderr_task = None
    client._exit_task = None

    asyncio.run(asyncio.wait_for(client._close(), timeout=5.0))

    # Build the process shell without starting its pump thread: the method
    # under test only needs the lock and the client slots, and starting a real
    # pump would try to launch pi.
    process = PiRpcProcess.__new__(PiRpcProcess)
    process._lock = threading.Lock()
    process._client = client
    process._connecting_client = None

    # This is the exact predicate StreamingTurnProcess.stop() evaluates before
    # deciding the worker is gone.
    assert process._force_stopped_client() is False

    # And the inverse: a settled client reports stopped through the same path.
    client._process = None
    assert process._force_stopped_client() is True


def test_a_reaped_child_with_unsettled_pipes_is_not_reported_stopped() -> None:
    """Reaping is not settlement, and a second close must be able to finish.

    Both halves were wrong before. force_stopped() answered off
    ``returncode is not None``, so once the child reaped the public predicate
    said "stopped" while stdout/stderr/protocol still held the pipe FDs. And
    _await_barrier cancelled what it timed out on, so the handles kept for a
    later close were cancelled handles -- the retry raised CancelledError
    instead of converging, which made the retention pointless.
    """

    from hyprial.daemon.impl.harnesses.pi import rpc_client as pi_rpc

    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    release = asyncio.Event

    class EmptyStream:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _n: int = -1) -> bytes:
            return b""

    class ReapedProcess:
        """Child is gone; its pipes are not."""

        returncode = 0
        stdin = None
        stdout = EmptyStream()
        stderr = EmptyStream()

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:  # pragma: no cover - already reaped
            raise AssertionError("already reaped")

        def kill(self) -> None:  # pragma: no cover - already reaped
            raise AssertionError("already reaped")

    async def scenario() -> None:
        loop_holder["loop"] = asyncio.get_running_loop()
        gate = release()

        client = PiRpcClient(
            spec(), "reaped-session", command=(sys.executable, str(FAKE_PI))
        )
        client._process = ReapedProcess()  # type: ignore[assignment]
        client._process_protocol = None
        # A drain that has not finished: this is the outstanding custody.
        client._stderr_task = asyncio.create_task(gate.wait())
        client._exit_task = None

        monkey_step = 0.05
        pi_rpc._CLOSE_STEP_SECONDS, saved_step = monkey_step, pi_rpc._CLOSE_STEP_SECONDS
        pi_rpc._CLOSE_TOTAL_SECONDS, saved_total = 0.12, pi_rpc._CLOSE_TOTAL_SECONDS
        try:
            await client._close()

            # Child reaped, pipes outstanding -> not stopped.
            assert client.force_stopped() is False
            process = PiRpcProcess.__new__(PiRpcProcess)
            process._lock = threading.Lock()
            process._client = client
            process._connecting_client = None
            assert process._force_stopped_client() is False

            # The retained task must still be awaitable, not cancelled.
            assert client._stderr_task is not None
            assert not client._stderr_task.cancelled()

            # Release the barrier; the second close converges.
            gate.set()
            await client._close()
            assert client.force_stopped() is True
            assert process._force_stopped_client() is True
        finally:
            pi_rpc._CLOSE_STEP_SECONDS = saved_step
            pi_rpc._CLOSE_TOTAL_SECONDS = saved_total

    asyncio.run(scenario())


def test_public_stop_converges_after_a_late_barrier_without_reentering_close() -> None:
    """Convergence must be reachable from the entry points the product uses.

    The outer stop only calls force_stop()/force_stopped(); it never calls
    client._close() again. A test that converged by invoking the inner close
    directly proved something the product cannot do -- the previous version
    did exactly that, so custody could in fact never be released once a close
    timed out.
    """

    class EmptyStream:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _n: int = -1) -> bytes:
            return b""

    async def scenario() -> None:
        gate = asyncio.Event()
        client = PiRpcClient(
            spec(), "late-barrier", command=(sys.executable, str(FAKE_PI))
        )
        client._process = _reaped_process_with(EmptyStream(), EmptyStream())  # type: ignore[assignment]
        client._process_protocol = None
        client._stderr_task = asyncio.create_task(gate.wait())
        client._exit_task = None

        saved = pi_rpc._CLOSE_STEP_SECONDS, pi_rpc._CLOSE_TOTAL_SECONDS
        pi_rpc._CLOSE_STEP_SECONDS, pi_rpc._CLOSE_TOTAL_SECONDS = 0.05, 0.12
        try:
            await client._close()

            process = PiRpcProcess.__new__(PiRpcProcess)
            process._lock = threading.Lock()
            process._client = client
            process._connecting_client = None

            assert process._force_stopped_client() is False

            # The barrier finishes on its own; nothing calls _close() again.
            gate.set()
            await asyncio.sleep(0)

            assert process._force_stopped_client() is True
            assert client._process is None
        finally:
            pi_rpc._CLOSE_STEP_SECONDS, pi_rpc._CLOSE_TOTAL_SECONDS = saved

    asyncio.run(scenario())
