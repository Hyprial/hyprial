from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
import types
from dataclasses import dataclass, field

import pytest


class StageFailure(RuntimeError):
    pass


class CleanupFailure(RuntimeError):
    pass


@dataclass
class _FakeHandle:
    state: _FakeWindows
    label: str
    value: int
    closed: bool = False
    close_calls: int = 0

    def __int__(self) -> int:
        return self.value

    def Close(self) -> None:
        if self.closed:
            raise AssertionError(f"{self.label} was closed twice")
        self.close_calls += 1
        remaining = self.state.close_failures.get(self.label, 0)
        if remaining:
            self.state.close_failures[self.label] = remaining - 1
            raise CleanupFailure(f"close:{self.label}")
        self.closed = True


@dataclass
class _FakeProcess:
    state: _FakeWindows
    pid: int = 4242
    returncode: int | None = None
    kill_calls: int = 0
    wait_calls: int = 0

    def kill(self) -> None:
        self.kill_calls += 1
        if self.state.kill_failures:
            self.state.kill_failures -= 1
            raise CleanupFailure("bootstrap-kill")
        if self.state.bootstrap_kill_clears_job:
            self.state.active_processes = 0
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.state.wait_failures:
            self.state.wait_failures -= 1
            raise CleanupFailure("bootstrap-wait")
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@dataclass
class _FakeWindows:
    fail_stage: str | None = None
    wait_result: int = 0
    active_processes: int = 0
    close_failures: dict[str, int] = field(default_factory=dict)
    handles: list[_FakeHandle] = field(default_factory=list)
    duplicate_calls: int = 0
    terminate_calls: int = 0
    terminate_failures: int = 0
    kill_failures: int = 0
    wait_failures: int = 0
    termination_delay_seconds: float | None = None
    bootstrap_kill_clears_job: bool = True
    process: _FakeProcess | None = None
    prepared: threading.Event = field(default_factory=threading.Event)
    release_prepared: threading.Event = field(default_factory=threading.Event)
    terminated: threading.Event = field(default_factory=threading.Event)
    block_duplicate_ready: bool = False

    def handle(self, label: str) -> _FakeHandle:
        item = _FakeHandle(self, label, 100 + len(self.handles))
        self.handles.append(item)
        return item

    def fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise StageFailure(stage)

    async def create_subprocess(self, *_args, **_kwargs) -> _FakeProcess:
        self.fail("spawn")
        self.active_processes = 1
        process = _FakeProcess(self)
        self.process = process
        if self.block_duplicate_ready:
            assert await asyncio.to_thread(self.terminated.wait, 2.0)
        return process


@pytest.fixture
def fake_windows(monkeypatch: pytest.MonkeyPatch):
    state = _FakeWindows()

    win32api = types.ModuleType("win32api")
    win32api.GetCurrentProcess = lambda: object()  # type: ignore[attr-defined]

    def duplicate_handle(_source, handle, _target, *_args):
        state.duplicate_calls += 1
        stage = "duplicate_job" if state.duplicate_calls == 1 else "duplicate_ready"
        if stage == "duplicate_ready" and state.block_duplicate_ready:
            state.prepared.set()
            assert state.release_prepared.wait(2.0)
        state.fail(stage)
        return state.handle(f"inherited_{handle.label}")

    win32api.DuplicateHandle = duplicate_handle  # type: ignore[attr-defined]

    win32con = types.ModuleType("win32con")
    win32con.DUPLICATE_SAME_ACCESS = 2  # type: ignore[attr-defined]

    win32event = types.ModuleType("win32event")
    win32event.WAIT_OBJECT_0 = 0  # type: ignore[attr-defined]
    win32event.WAIT_TIMEOUT = 258  # type: ignore[attr-defined]

    def create_event(*_args):
        state.fail("create_ready")
        return state.handle("ready")

    win32event.CreateEvent = create_event  # type: ignore[attr-defined]
    win32event.WaitForSingleObject = (  # type: ignore[attr-defined]
        lambda _handle, _timeout: state.wait_result
    )

    win32job = types.ModuleType("win32job")
    win32job.JobObjectExtendedLimitInformation = 1  # type: ignore[attr-defined]
    win32job.JobObjectBasicAccountingInformation = 2  # type: ignore[attr-defined]
    win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 4  # type: ignore[attr-defined]

    def create_job(*_args):
        state.fail("create_job")
        return state.handle("job")

    def query_job(_job, information):
        if information == win32job.JobObjectExtendedLimitInformation:
            state.fail("query")
            return {"BasicLimitInformation": {"LimitFlags": 0}}
        return {"ActiveProcesses": state.active_processes}

    def set_job(*_args):
        state.fail("set")

    def terminate_job(*_args):
        state.terminate_calls += 1
        if state.terminate_failures:
            state.terminate_failures -= 1
            raise CleanupFailure("terminate:job")
        if state.termination_delay_seconds is None:
            state.active_processes = 0
        else:
            asyncio.get_running_loop().call_later(
                state.termination_delay_seconds,
                setattr,
                state,
                "active_processes",
                0,
            )
        state.terminated.set()

    win32job.CreateJobObject = create_job  # type: ignore[attr-defined]
    win32job.QueryInformationJobObject = query_job  # type: ignore[attr-defined]
    win32job.SetInformationJobObject = set_job  # type: ignore[attr-defined]
    win32job.TerminateJobObject = terminate_job  # type: ignore[attr-defined]

    for name, module in {
        "win32api": win32api,
        "win32con": win32con,
        "win32event": win32event,
        "win32job": win32job,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    target = "hyprial.kernel.impl.platform.windows_owned_process"
    sys.modules.pop(target, None)
    module = importlib.import_module(target)
    monkeypatch.setattr(module, "process_identity", lambda pid: f"fake:{pid}")
    monkeypatch.setattr(
        module.subprocess, "STARTUPINFO", type("STARTUPINFO", (), {}), raising=False
    )
    monkeypatch.setattr(
        module.asyncio, "create_subprocess_exec", state.create_subprocess
    )
    try:
        yield module, state
    finally:
        sys.modules.pop(target, None)


@pytest.mark.parametrize(
    "stage,acquired",
    [
        ("query", ("job",)),
        ("set", ("job",)),
        ("duplicate_job", ("job",)),
        ("create_ready", ("job", "inherited_job")),
        ("duplicate_ready", ("job", "inherited_job", "ready")),
    ],
)
def test_each_partial_acquisition_failure_closes_every_acquired_handle(
    fake_windows, stage: str, acquired: tuple[str, ...]
) -> None:
    module, state = fake_windows
    state.fail_stage = stage
    group = module.WindowsOwnedProcessGroup(label="partial")

    with pytest.raises(StageFailure, match=stage):
        asyncio.run(group.spawn(("worker",)))

    by_label = {handle.label: handle for handle in state.handles}
    assert set(by_label) == set(acquired)
    assert all(by_label[label].closed for label in acquired)
    assert all(by_label[label].close_calls == 1 for label in acquired)
    assert group._job is None


def test_late_bootstrap_failure_preserves_error_and_retries_failed_cleanup(
    fake_windows,
) -> None:
    module, state = fake_windows
    state.wait_result = state_module = sys.modules["win32event"].WAIT_TIMEOUT
    assert state_module == 258
    state.close_failures["job"] = 1
    group = module.WindowsOwnedProcessGroup(label="late")

    with pytest.raises(ConnectionError, match="did not confirm Job ownership"):
        asyncio.run(group.spawn(("worker",)))

    first_job = next(handle for handle in state.handles if handle.label == "job")
    assert first_job.close_calls == 1
    assert not first_job.closed
    assert state.process is not None
    assert state.process.kill_calls == 1
    assert state.process.wait_calls == 1
    assert group._job is first_job

    state.wait_result = sys.modules["win32event"].WAIT_OBJECT_0
    process = asyncio.run(group.spawn(("worker",)))
    assert process.pid == 4242
    assert first_job.close_calls == 2
    assert first_job.closed

    group.force_close()
    assert group.stopped()
    assert group._job is None


def test_failed_prepared_handle_close_stays_visible_until_next_spawn_cleanup(
    fake_windows,
) -> None:
    module, state = fake_windows
    state.fail_stage = "spawn"
    state.close_failures["inherited_ready"] = 1
    group = module.WindowsOwnedProcessGroup(label="prepared-close")

    with pytest.raises(StageFailure, match="spawn"):
        asyncio.run(group.spawn(("worker",)))

    inherited_ready = next(
        handle for handle in state.handles if handle.label == "inherited_ready"
    )
    assert inherited_ready.close_calls == 1
    assert not inherited_ready.closed
    liveness = group.liveness()
    assert liveness.state is module.ProcessLivenessState.UNKNOWN
    assert liveness.detail == "Windows Job cleanup is pending"

    state.fail_stage = None
    process = asyncio.run(group.spawn(("worker",)))
    assert process.pid == 4242
    assert inherited_ready.close_calls == 2
    assert inherited_ready.closed

    group.force_close()
    assert group.stopped()


def test_unsettled_termination_and_bootstrap_cleanup_hold_ownership_until_retry(
    fake_windows,
) -> None:
    module, state = fake_windows
    state.wait_result = sys.modules["win32event"].WAIT_TIMEOUT
    state.terminate_failures = 1
    state.kill_failures = 1
    group = module.WindowsOwnedProcessGroup(label="held")

    with pytest.raises(ConnectionError, match="did not confirm Job ownership"):
        asyncio.run(group.spawn(("worker",)))

    held_job = next(handle for handle in state.handles if handle.label == "job")
    assert group._job is held_job
    assert not held_job.closed
    assert state.process is not None
    assert state.process.wait_calls == 0
    assert not group.stopped()
    liveness = group.liveness()
    assert liveness.state is module.ProcessLivenessState.ALIVE
    assert liveness.detail == "Windows Job cleanup is pending"

    state.wait_result = sys.modules["win32event"].WAIT_OBJECT_0
    process = asyncio.run(group.spawn(("worker",)))
    assert process.pid == 4242
    assert held_job.closed

    group.force_close()
    assert group.stopped()
    assert group._job is None


def test_delayed_job_termination_inside_existing_budget_allows_next_spawn(
    fake_windows, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, state = fake_windows
    monkeypatch.setattr(module, "PROCESS_FORCE_TERM_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(module, "PROCESS_FORCE_KILL_SECONDS", 0.02, raising=False)
    state.wait_result = sys.modules["win32event"].WAIT_TIMEOUT
    state.termination_delay_seconds = 0.01
    state.bootstrap_kill_clears_job = False
    group = module.WindowsOwnedProcessGroup(label="delayed-inside")

    with pytest.raises(ConnectionError, match="did not confirm Job ownership"):
        asyncio.run(group.spawn(("worker",)))

    first_job = group._job
    assert first_job is not None
    state.wait_result = sys.modules["win32event"].WAIT_OBJECT_0
    process = asyncio.run(group.spawn(("worker",)))

    assert process.pid == 4242
    assert first_job.closed
    state.termination_delay_seconds = None
    group.force_close()
    assert group.stopped()


def test_delayed_job_termination_beyond_existing_budget_retains_ownership(
    fake_windows, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, state = fake_windows
    monkeypatch.setattr(module, "PROCESS_FORCE_TERM_SECONDS", 0.005, raising=False)
    monkeypatch.setattr(module, "PROCESS_FORCE_KILL_SECONDS", 0.005, raising=False)
    state.wait_result = sys.modules["win32event"].WAIT_TIMEOUT
    state.termination_delay_seconds = 0.1
    state.bootstrap_kill_clears_job = False
    group = module.WindowsOwnedProcessGroup(label="delayed-beyond")

    with pytest.raises(ConnectionError, match="did not confirm Job ownership"):
        asyncio.run(group.spawn(("worker",)))

    held_job = group._job
    assert held_job is not None
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cleanup is unresolved"):
        asyncio.run(group.spawn(("worker",)))
    assert time.monotonic() - started < 0.08
    assert group._job is held_job
    assert not held_job.closed

    state.termination_delay_seconds = None
    group.force_close()
    assert group.stopped()


def test_concurrent_stop_during_prepared_resources_settles_exact_generation(
    fake_windows,
) -> None:
    module, state = fake_windows
    state.block_duplicate_ready = True
    group = module.WindowsOwnedProcessGroup(label="concurrent")
    failures: list[BaseException] = []

    def run_spawn() -> None:
        try:
            asyncio.run(group.spawn(("worker",)))
        except BaseException as error:  # noqa: BLE001 - cross-thread assertion
            failures.append(error)

    spawn_thread = threading.Thread(target=run_spawn)
    spawn_thread.start()
    assert state.prepared.wait(1.0)
    stop_thread = threading.Thread(target=group.force_close)
    stop_thread.start()
    state.release_prepared.set()
    spawn_thread.join(3.0)
    stop_thread.join(3.0)

    assert not spawn_thread.is_alive()
    assert not stop_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ConnectionError)
    assert "stopping" in str(failures[0])
    assert state.process is not None
    assert state.process.kill_calls == 1
    assert all(handle.closed for handle in state.handles)
    assert all(handle.close_calls == 1 for handle in state.handles)
    assert group.stopped()
    assert group._job is None


def test_identity_failure_cleans_only_the_exact_bootstrap_process(
    fake_windows, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, state = fake_windows
    group = module.WindowsOwnedProcessGroup(label="identity")

    def fail_identity(_pid: int) -> str:
        raise OSError("identity unavailable")

    monkeypatch.setattr(module, "process_identity", fail_identity)
    with pytest.raises(OSError, match="identity unavailable"):
        asyncio.run(group.spawn(("worker",)))

    assert state.process is not None
    assert state.process.kill_calls == 1
    assert state.process.wait_calls == 1
    assert group._job is None


def test_signal_never_terminates_an_unverified_pid(fake_windows) -> None:
    module, state = fake_windows
    group = module.WindowsOwnedProcessGroup(label="pid-fence")
    process = asyncio.run(group.spawn(("worker",)))

    group.signal(process.pid + 1, 9)
    assert state.terminate_calls == 0
    assert state.active_processes == 1

    group.force_close()
    assert state.terminate_calls >= 1
    assert group.stopped()
