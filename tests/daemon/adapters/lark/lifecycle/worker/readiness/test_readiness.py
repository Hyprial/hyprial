from __future__ import annotations

import json
import os
import resource
import select
import threading
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.adapters.lark.contracts.errors import AdapterStartError
from hyprial.daemon.impl.adapters.lark.worker.process import ERROR, LarkWorkerProcess, _wait_readable

from ..support import (
    _gateway,
    _launcher,
    _write,
)


def test_wait_readable_works_beyond_fd_setsize() -> None:
    """The readiness channel must not die at fd 1024 (macOS FD_SETSIZE).

    Bare select.select() raises ValueError on any watched fd >= 1024; a
    long-lived daemon accumulates descriptors past that line (observed in
    the field at fd 1148).  This test parks the descriptor space above the
    cap, then proves both halves: bare select() really cannot watch the
    pipe (the test is exercising the >1024 condition, not nothing), and
    _wait_readable answers correctly on the same descriptors.
    """

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if 0 < soft <= 1100:
        pytest.skip("RLIMIT_NOFILE too low to reach FD_SETSIZE")
    held: list[int] = []
    try:
        while True:
            descriptor = os.open(os.devnull, os.O_RDONLY)
            held.append(descriptor)  # the crossing fd must be parked too
            if descriptor >= 1030:
                break
        read_fd, write_fd = os.pipe()
        assert read_fd > 1024, "pipe did not land above FD_SETSIZE"
        try:
            with pytest.raises(ValueError, match="out of range"):
                select.select([read_fd], [], [], 0)

            assert _wait_readable(read_fd, 0.05) is False
            os.write(write_fd, b"x")
            assert _wait_readable(read_fd, 1.0) is True
            assert _wait_readable(read_fd, 0) is False
        finally:
            os.close(read_fd)
            os.close(write_fd)
    finally:
        for descriptor in held:
            os.close(descriptor)


def test_wait_readable_handles_closed_descriptor() -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)  # EOF: the pipe is immediately readable.
    try:
        assert _wait_readable(read_fd, 0.5) is True
    finally:
        os.close(read_fd)


def test_sdk_log_noise_on_stdout_does_not_break_the_ready_signal(
    tmp_path: Path,
) -> None:
    """SDK-style non-JSON log lines on stdout precede the ready signal; the
    adapter must still come online (the signal travels on a dedicated fd)."""

    script = _write(
        tmp_path,
        """
        import json, os, sys, time

        name = sys.argv[1]
        # What the real Lark SDK prints to stdout before the websocket is up.
        print("[Lark] [2026-08-08 19:52:50,488] [INFO] connected to "
              "wss://msg-frontier.feishu.cn/...", flush=True)
        print("[Lark] [2026-08-08 19:52:50,490] [DEBUG] heartbeat scheduled",
              flush=True)
        os.write(int(os.environ["HYPRIAL_LARK_READY_FD"]),
                 json.dumps({"status": "online", "name": name}).encode() + b"\\n")
        while True:
            time.sleep(0.05)
        """,
    )
    process = _launcher(tmp_path, script).start(_gateway())
    try:
        assert process.running
    finally:
        process.stop()
    assert not process.running

    log_path = tmp_path / "state" / "logs" / "lark-gateway.jsonl"
    deadline = time.monotonic() + 1.0
    records: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        records = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        if any(record["event"] == "lark.sdk.output" for record in records):
            break
        time.sleep(0.01)

    events = [record["event"] for record in records]
    # ⛔ No assertion on events[0]: the stdout drain thread starts at
    # runtime.py:144-145, thirteen lines before "adapter.started" is logged at
    # :157, so the pump and the adapter are two independent producers writing
    # the same log.  The code establishes no order between them -- a test that
    # asserted one was decided by arrival order, which means its greens were
    # coin flips too, not evidence that the behaviour was right.
    # The ordering that IS a contract (started -> ready -> stopped) is asserted
    # below by filtering to those three events, which is order-correct whatever
    # the pump interleaves.
    assert [
        event
        for event in events
        if event in {"adapter.started", "adapter.ready", "adapter.stopped"}
    ] == ["adapter.started", "adapter.ready", "adapter.stopped"]
    assert all(record["component"] == "lark-adapter" for record in records)
    assert all(record["name"] == "lark-test" for record in records)
    raw = log_path.read_text(encoding="utf-8")
    assert "msg-frontier.feishu.cn" not in raw
    assert "[REDACTED_URL]" in raw


def test_sdk_log_flood_on_stdout_cannot_block_the_ready_signal(
    tmp_path: Path,
) -> None:
    """Stress probe: the worker floods stdout with ~2k SDK-style log lines
    (~152KB, well beyond the 64KB pipe buffer) before signaling. Without the
    from-spawn stdout drainer the worker would block on a full pipe and the
    start would time out; with it the adapter comes online.

    The guard is the outcome, not a stopwatch: a ~152KB write cannot fit the
    64KB pipe, so if the drainer were gone the child would block in that write
    and ``start()`` would raise before reaching these assertions.

    The flood is sized against the drainer's own cost model, not against
    bravado: the drainer logs every line with one synchronous append
    (~0.15ms idle), so drain time grows linearly with line count.  The
    earlier 40k-line flood measured 6.2s idle but crossed the 30s startup
    budget under CI CPU contention (Forgejo runs 44174 and 44264), and the
    backlog its kill left behind outlived stop()'s 2s drainer join, tripping
    the teardown fd gate with a pipe fd that closed moments later.  2k lines
    keep the same above-pipe-capacity guard with roughly an order of
    magnitude of headroom against the budget on a contended host.  The
    bounded drainer-EOF wait below keeps the same false positive away in
    this test's own teardown: stop() does not guarantee the drainer has
    finished logging the backlog, and the fd gate must see the settled
    state, not a straggler."""

    script = _write(
        tmp_path,
        """
        import json, os, sys, time

        name = sys.argv[1]
        for index in range(2_000):
            print(f"[Lark] [2026-08-08 19:52:50,{index % 1000:03d}] [INFO] "
                  f"websocket frame batch {index} dispatched", flush=False)
        sys.stdout.flush()
        os.write(int(os.environ["HYPRIAL_LARK_READY_FD"]),
                 json.dumps({"status": "online", "name": name}).encode() + b"\\n")
        while True:
            time.sleep(0.05)
        """,
    )
    # The budget only bounds how long a genuinely broken drainer takes to
    # fail; it is not the assertion.  Sized well above the load-amplified
    # startup path so a busy host cannot turn this into a false failure.
    launcher = _launcher(tmp_path, script, startup_timeout=30.0)
    process = launcher.start(_gateway())
    try:
        assert process.running
        drained = process.last_sdk_output
        assert drained is not None and "websocket frame batch" in drained
    finally:
        process.stop()
        assert process._drainer is not None
        process._drainer.join(timeout=10.0)
        assert not process._drainer.is_alive(), (
            "drainer still logging the killed worker's backlog -- its stdout "
            "pipe fd would show up as a teardown leak"
        )
        stdout = process._process.stdout
        assert stdout is not None and stdout.closed


def test_worker_that_never_signals_fails_by_timeout(tmp_path: Path) -> None:
    """A worker that stays alive but never sends a ready signal must fail the
    start by timeout; the launcher must not wait forever."""

    script = _write(
        tmp_path,
        """
        import time

        print("[Lark] connecting ...", flush=True)
        while True:
            time.sleep(0.05)
        """,
    )
    launcher = _launcher(tmp_path, script, startup_timeout=0.3)
    started = time.monotonic()
    with pytest.raises(AdapterStartError, match="did not become online"):
        launcher.start(_gateway())
    assert time.monotonic() - started < 5.0


def test_worker_exiting_without_a_signal_fails_immediately(tmp_path: Path) -> None:
    """EOF on the ready channel (worker crashed before signaling) is a startup
    failure, not a hang until the timeout."""

    script = _write(tmp_path, "raise SystemExit(3)\n")
    launcher = _launcher(tmp_path, script, startup_timeout=30.0)
    started = time.monotonic()
    with pytest.raises(AdapterStartError, match="failed before becoming online"):
        launcher.start(_gateway())
    assert time.monotonic() - started < 5.0


def test_failed_start_closes_control_socket_when_child_exits_after_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hyprial.daemon.impl.adapters.lark.worker.process as runtime

    script = _write(tmp_path, """
        import json, os, sys
        os.write(int(os.environ["HYPRIAL_LARK_READY_FD"]),
                 json.dumps({"status": "online", "name": sys.argv[1]}).encode() + b"\\n")
    """)
    launcher = _launcher(tmp_path, script, startup_timeout=5.0)
    exited = threading.Event()
    read_ready = runtime._read_ready
    spawn = launcher.spawn
    held = []

    def read_after_exit(*args):
        result = read_ready(*args)
        assert exited.wait(5.0), "child exit was not observed"
        return result

    def capture(gateway):
        process = spawn(gateway)
        held.append(process)
        try:
            process._process.wait(timeout=5.0)
        finally:
            exited.set()
        return process

    monkeypatch.setattr(runtime, "_read_ready", read_after_exit)
    monkeypatch.setattr(launcher, "spawn", capture)
    try:
        with pytest.raises(AdapterStartError, match="failed before becoming online"):
            launcher.start(_gateway())
        process = held[0]
        assert process._control_socket is None, "failed start retained its control socket"
        assert not process._ready_thread.is_alive()
        assert process._process.stdout.closed
    finally:
        for process in held:
            process.stop()


def test_worker_self_exit_releases_stdout_pipe_while_process_is_held(
    tmp_path: Path,
) -> None:
    """The resident-daemon shape: the launcher result is held for health
    tracking long after the child exits ON ITS OWN, and stop() is never
    called.  The drainer must release the child's stdout pipe at EOF;
    otherwise every worker self-exit leaks one fd for the lifetime of the
    process object (card #77).

    Asserted on the stream itself, not via the fd gate: the gate's forced GC
    would collect the dropped process object and mask this leak, which is
    exactly why the assertion must fire while the object is still held."""

    script = _write(
        tmp_path,
        """
        import json, os, sys, time
        from pathlib import Path

        os.write(int(os.environ["HYPRIAL_LARK_READY_FD"]),
                 json.dumps({"status": "online", "name": sys.argv[1]}).encode() + b"\\n")
        print("[Lark] one line then exit", flush=True)
        # The test releases us only after start() has returned a confirmed
        # worker. Immediate exit races the launcher's valid liveness check.
        while not Path(__file__).with_suffix(".exit").exists():
            time.sleep(0.01)
        sys.exit(0)
        """,
    )
    launcher = _launcher(tmp_path, script, startup_timeout=5.0)
    process = launcher.start(_gateway())
    try:
        script.with_suffix(".exit").touch()
        deadline = time.monotonic() + 5.0
        while process.running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not process.running, "child should have exited on its own"
        assert process._drainer is not None
        process._drainer.join(timeout=5.0)
        assert not process._drainer.is_alive(), "drainer should reach EOF"
        stdout = process._process.stdout
        assert stdout is not None and stdout.closed, (
            "child stdout pipe still open after EOF with the process object "
            "held -- the resident daemon would keep this fd forever"
        )
    finally:
        process.stop()


def test_non_json_bytes_on_the_ready_channel_are_an_invalid_signal(
    tmp_path: Path,
) -> None:
    """Anything non-JSON on the dedicated ready channel means a broken worker;
    fail fast instead of skipping lines."""

    script = _write(
        tmp_path,
        """
        import os, time

        os.write(int(os.environ["HYPRIAL_LARK_READY_FD"]), b"not json\\n")
        while True:
            time.sleep(0.05)
        """,
    )
    launcher = _launcher(tmp_path, script, startup_timeout=5.0)
    with pytest.raises(AdapterStartError, match="invalid ready signal"):
        launcher.start(_gateway())


def test_readiness_programming_error_is_visible_and_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        stdout = None
        pid = 12345

        def __init__(self) -> None:
            self.alive = True

        def poll(self) -> int | None:
            return None if self.alive else 1

        def terminate(self) -> None:
            self.alive = False

        def wait(self, timeout: float) -> int:
            del timeout
            self.alive = False
            return 1

    errors: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: errors.append(args.exc_value),
    )
    monkeypatch.setattr(
        LarkWorkerProcess,
        "_run_ready",
        lambda _self: (_ for _ in ()).throw(ImportError("missing readiness import")),
    )
    ready_read, ready_write = os.pipe()
    process = Process()
    worker = LarkWorkerProcess(  # type: ignore[arg-type]
        process,
        name="lark-test",
        ready_fd=ready_read,
        startup_timeout=0.1,
    )
    try:
        assert worker.wait_ready(1.0) == ERROR
        worker._ready_thread.join(timeout=1.0)
    finally:
        os.close(ready_write)

    assert process.alive is False
    assert len(errors) == 1
    assert isinstance(errors[0], ImportError)


def test_a_worker_that_dies_before_ready_says_why(tmp_path: Path) -> None:
    """allen-channel, 2026-09-26: the worker's own reason went to /dev/null
    (stderr), and `adapter start` said only "failed before becoming online";
    the real cause (tenant token 10014) needed a manual `adapter scopes authorize`."""

    script = _write(
        tmp_path,
        "import sys\n"
        "print('cannot obtain Lark tenant token (10014): app secret invalid', file=sys.stderr, flush=True)\n"
        "raise SystemExit(3)\n",
    )
    launcher = _launcher(tmp_path, script, startup_timeout=30.0)
    with pytest.raises(AdapterStartError) as raised:
        launcher.start(_gateway())
    message = str(raised.value)
    assert "failed before becoming online" in message
    assert "10014" in message


def test_ready_is_logged_before_start_returns_even_when_the_write_is_slow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``adapter.ready`` is logged before readiness resolves (2026-10-07).

    Readiness resolved first and the record was written after, so a caller
    that stopped right after ``start()`` returned could log ``adapter.stopped``
    before ``adapter.ready`` (CI run 22309, a 3-CPU-throttled container).
    A slow ready write reproduces that window deterministically.
    """

    from hyprial.daemon.impl.adapters.lark.worker import process as worker_process

    real_adapter = worker_process.Logger.adapter

    class _SlowReady:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def info(self, event: str, **fields: object) -> None:
            if event == "adapter.ready":
                time.sleep(0.3)
            self._inner.info(event, **fields)

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    monkeypatch.setattr(
        worker_process.Logger,
        "adapter",
        staticmethod(lambda *args, **kwargs: _SlowReady(real_adapter(*args, **kwargs))),
    )
    script = _write(
        tmp_path,
        """
        import json, os, sys, time

        os.write(int(os.environ["HYPRIAL_LARK_READY_FD"]),
                 json.dumps({"status": "online", "name": sys.argv[1]}).encode() + b"\\n")
        while True:
            time.sleep(0.05)
        """,
    )
    process = _launcher(tmp_path, script).start(_gateway())
    process.stop()

    log_path = tmp_path / "state" / "logs" / "lark-gateway.jsonl"
    deadline = time.monotonic() + 2.0
    events: list[str] = []
    while time.monotonic() < deadline:
        events = [
            json.loads(line)["event"]
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        if "adapter.ready" in events and "adapter.stopped" in events:
            break
        time.sleep(0.01)
    assert [
        event
        for event in events
        if event in {"adapter.started", "adapter.ready", "adapter.stopped"}
    ] == ["adapter.started", "adapter.ready", "adapter.stopped"]
