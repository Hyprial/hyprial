"""Headless worker session persistence: record -> restart -> resume.

The daemon persists each managed worker's native session/thread ref into
desired state (``sessionRef``) so a daemon restart resumes the conversation
instead of cold-starting every connector. Claude refuses a dead resume target;
the legacy Pi and Codex recovery contracts below remain separate.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl import (
    DaemonEventBridge,
    DesiredStateStore,
    HarnessLaunchSpec,
    ManagedHarnessRuntime,
)
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.daemon.impl.harnesses.agent_sdk import ClaudeAgentSdkProcess, IsolatedAgentSdkClient
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerProcess
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess

FAKE_PI = Path(__file__).resolve().parents[3] / 'tests/fixtures/harnesses/fake_pi_rpc.py'
FAKE_CODEX = Path(__file__).resolve().parents[3] / 'tests/fixtures/harnesses/fake_codex_app_server.py'
SDK_WORKER = (
    Path(__file__).resolve().parents[3] / 'src/hyprial/daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py'
)


# --------------------------------------------------------------------------- #
# Desired-state store: the dedicated session-ref write path
# --------------------------------------------------------------------------- #


def _pi_spec(name: str = "worker", **kwargs: object) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(harness="pi", name=name, headless=True, **kwargs)  # type: ignore[arg-type]


def test_sync_session_refs_updates_only_the_ref(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.upsert_harness(
        _pi_spec(args=("--model", "gpt-5.5"), cwd="/work", nickname="brick")
    )

    store.sync_harness_session_refs({("pi", "worker"): "session-1"})

    spec = store.load().harnesses[0]
    assert spec.session_ref == "session-1"
    # Every operator-owned field survives the runtime write-back untouched.
    assert spec.args == ("--model", "gpt-5.5")
    assert spec.cwd == "/work"
    assert spec.nickname == "brick"


def test_sync_session_refs_ignores_harnesses_absent_from_desired_state(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.upsert_harness(_pi_spec())

    store.sync_harness_session_refs({("codex", "ghost"): "thread-9"})

    assert store.load().harnesses[0].session_ref is None


def test_sync_session_refs_skips_the_write_when_nothing_changed(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow

    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.upsert_harness(_pi_spec())
    store.sync_harness_session_refs({("pi", "worker"): "session-1"})

    # "Nothing changed => nothing written": the store's only write path is
    # the SQLite transaction, so a no-op sync must never reach it.
    def _boom(document: object) -> None:
        raise AssertionError("a no-op sync must not write")

    original = DesiredStateSqliteShadow.write_document
    DesiredStateSqliteShadow.write_document = _boom  # type: ignore[method-assign]
    try:
        store.sync_harness_session_refs({("pi", "worker"): "session-1"})
    finally:
        DesiredStateSqliteShadow.write_document = original  # type: ignore[method-assign]
    assert store.load().harnesses[0].session_ref == "session-1"


def test_operator_restart_clears_the_persisted_ref(tmp_path: Path) -> None:
    """`hyprial agent start` replaces the whole spec (ref absent) -- the operator's
    escape hatch to a deliberately fresh session must keep working."""

    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.upsert_harness(_pi_spec())
    store.sync_harness_session_refs({("pi", "worker"): "session-1"})
    assert store.load().harnesses[0].session_ref == "session-1"

    store.upsert_harness(_pi_spec(args=("--model", "fresh")))

    spec = store.load().harnesses[0]
    assert spec.session_ref is None
    assert spec.args == ("--model", "fresh")


# --------------------------------------------------------------------------- #
# Runtime: learned refs are persisted, and a restart restores with the ref
# --------------------------------------------------------------------------- #


class _FakeTransport:
    def declare_liveliness(self, key: str) -> object:
        raise AssertionError("no mailbox expected")

    def close(self) -> None:
        pass


class _FakeInbox:
    def retry_due(self) -> list[object]:
        return []

    def prune_inbox(self) -> list[object]:
        return []

    def pending_messages(self, actor: str) -> list[object]:
        return []

    def close(self) -> None:
        pass


class _SessionProcess:
    """Minimal managed process exposing a runtime-learned session ref."""

    def __init__(self, session_ref: str | None) -> None:
        self.running = True
        self.session_ref = session_ref
        self.last_error = None

    def stop(self) -> None:
        self.running = False


class _SessionLauncher:
    def __init__(self, session_ref: str | None) -> None:
        self.session_ref = session_ref
        self.specs: list[HarnessLaunchSpec] = []
        self.processes: list[_SessionProcess] = []

    def start(self, spec: HarnessLaunchSpec) -> _SessionProcess:
        self.specs.append(spec)
        process = _SessionProcess(self.session_ref)
        self.processes.append(process)
        return process


def _runtime(
    tmp_path: Path, launcher: _SessionLauncher
) -> tuple[DaemonEventBridge, DesiredStateStore]:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    harnesses = ManagedHarnessRuntime(launcher, desired_state=store)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-1",
        desired_state=store,
        transport=_FakeTransport(),  # type: ignore[arg-type]
        inbox=_FakeInbox(),  # type: ignore[arg-type]
        harnesses=harnesses,
    )
    return runtime, store


def test_learned_session_ref_is_persisted_and_resumed_after_restart(
    tmp_path: Path,
) -> None:
    launcher = _SessionLauncher(session_ref=None)
    runtime, store = _runtime(tmp_path, launcher)
    store.upsert_harness(_pi_spec())
    runtime.harnesses.restore(store.load().harnesses)
    runtime.start()
    try:
        # The connector learns its session id only once it is up.
        launcher.processes[0].session_ref = "learned-session-1"
        runtime.on_timer()

        persisted = store.load().harnesses[0]
        assert persisted.session_ref == "learned-session-1"
    finally:
        runtime.stop()

    # Simulated daemon restart: a fresh runtime over the same state dir
    # restores the harness WITH the persisted ref on the launch spec.
    restart_launcher = _SessionLauncher(session_ref="learned-session-1")
    restarted, _store = _runtime(tmp_path, restart_launcher)
    restarted.harnesses.restore(_store.load().harnesses)
    restarted.start()
    try:
        [restored_spec] = restart_launcher.specs
        assert restored_spec.session_ref == "learned-session-1"
    finally:
        restarted.stop()


def test_session_ref_persist_failure_does_not_break_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[object, ...]] = []
    runtime, store = _runtime(tmp_path, _SessionLauncher(session_ref="s-1"))
    runtime._logger = lambda *args, **fields: events.append(args)
    store.upsert_harness(_pi_spec())
    runtime.harnesses.restore(store.load().harnesses)
    runtime.start()
    try:
        # Force the save to fail (a chmod-based approach is root-proof
        # neither in CI containers nor locally).
        def _boom(state: object) -> None:
            raise OSError("read-only state dir")

        monkeypatch.setattr(store, "save", _boom)
        runtime.on_timer()  # must not raise
        assert any("harness.session_ref.persist_failed" in args for args in events)
    finally:
        runtime.stop()


def test_an_unchanged_ref_is_not_sent_to_the_state_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production 2026-10-05: once any worker held a ref, every reconcile pass
    # (~0.6/s) sent SyncHarnessSessionRefs to the single state writer, which
    # loaded the whole desired state twice to find nothing changed -- ~10% of
    # the writer.  A ref already persisted must not reach the writer at all.
    launcher = _SessionLauncher(session_ref="s-1")
    runtime, store = _runtime(tmp_path, launcher)
    store.upsert_harness(_pi_spec())
    runtime.harnesses.restore(store.load().harnesses)
    runtime.start()
    try:
        runtime.on_timer()
        assert store.load().harnesses[0].session_ref == "s-1"

        syncs: list[object] = []
        original = store.sync_harness_session_refs
        monkeypatch.setattr(
            store,
            "sync_harness_session_refs",
            lambda refs: syncs.append(refs) or original(refs),
        )
        runtime.on_timer()
        runtime.on_timer()
        assert syncs == []

        # A ref the connector re-learns still reaches the writer.
        launcher.processes[0].session_ref = "s-2"
        runtime.on_timer()
        assert len(syncs) == 1
        assert store.load().harnesses[0].session_ref == "s-2"
    finally:
        runtime.stop()


# --------------------------------------------------------------------------- #
# pi: --session-id is create-or-resume; a stored ref that pi cannot open
# falls back to a fresh id exactly once
# --------------------------------------------------------------------------- #


def _pi_process(
    spec: HarnessLaunchSpec,
    *,
    env: dict[str, str],
) -> PiRpcProcess:
    return PiRpcProcess(
        spec,
        command=(sys.executable, str(FAKE_PI)),
        env=env,
        reconnect_delay_seconds=0.01,
    )


def _argv_logs(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _session_ids_seen(path: Path) -> list[str]:
    seen = []
    for argv in _argv_logs(path):
        seen.append(argv[argv.index("--session-id") + 1])
    return seen


def _wait_for(predicate, description: str, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {description}")


def _wait_results(process, count: int, timeout: float = 10.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    results = []
    while time.monotonic() < deadline:
        results.extend(process.drain_results())
        if len(results) >= count:
            return results
        time.sleep(0.01)
    raise AssertionError(f"pump produced {len(results)}/{count} results")


def test_pi_restart_launches_with_the_persisted_session_id(
    tmp_path: Path,
) -> None:
    argv_log = tmp_path / "argv.jsonl"
    process = _pi_process(
        _pi_spec(session_ref="persisted-pi-id"),
        env={"FAKE_PI_MODE": "settled", "FAKE_PI_ARGV_LOG": str(argv_log)},
    )
    try:
        assert process.wait_ready(timeout=10.0)
        assert process.session_ref == "persisted-pi-id"
        assert _session_ids_seen(argv_log) == ["persisted-pi-id"]
        assert process.enqueue(
            HarnessDelivery(
                delivery_id="d-1",
                conversation_id="c-1",
                sender="s",
                recipient="agent:op:node-1:worker",
                message="hello",
            )
        )
        [result] = _wait_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED
    finally:
        process.stop()


def test_pi_dead_session_ref_falls_back_to_a_fresh_id(tmp_path: Path) -> None:
    argv_log = tmp_path / "argv.jsonl"
    process = _pi_process(
        _pi_spec(session_ref="dead-pi-id"),
        env={
            "FAKE_PI_MODE": "reject-session",
            "FAKE_PI_DEAD_ID": "dead-pi-id",
            "FAKE_PI_ARGV_LOG": str(argv_log),
        },
    )
    try:
        assert process.wait_ready(timeout=10.0)
        # The dead ref was tried once, then abandoned for a fresh id; the
        # worker is up instead of looping on a session pi cannot open.
        assert process.session_ref != "dead-pi-id"
        assert _session_ids_seen(argv_log) == ["dead-pi-id", process.session_ref]
        assert process.enqueue(
            HarnessDelivery(
                delivery_id="d-1",
                conversation_id="c-1",
                sender="s",
                recipient="agent:op:node-1:worker",
                message="hello",
            )
        )
        [result] = _wait_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED
    finally:
        process.stop()


def test_pi_fresh_minted_uuid_passes_through_unsanitized(
    tmp_path: Path,
) -> None:
    """A fresh mint is pure uuid (hex + '-', all in pi's allowed charset):
    the #192 boundary translation must pass it through byte-for-byte."""

    argv_log = tmp_path / "argv.jsonl"
    process = _pi_process(
        _pi_spec(),
        env={"FAKE_PI_MODE": "settled", "FAKE_PI_ARGV_LOG": str(argv_log)},
    )
    try:
        assert process.wait_ready(timeout=10.0)
        [launched] = _session_ids_seen(argv_log)
        assert launched == process.session_ref  # byte-for-byte, no rewrite
    finally:
        process.stop()


def test_desired_state_keeps_a_colon_session_ref_byte_for_byte(
    tmp_path: Path,
) -> None:
    """The stored ref is OUR key: pi's charset rule never back-pollutes
    desired state (the other jaw of #192's no-back-pollution clamp)."""

    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.upsert_harness(_pi_spec())
    store.sync_harness_session_refs({("pi", "worker"): "squire:alw:usrb"})

    assert store.load().harnesses[0].session_ref == "squire:alw:usrb"


def test_pi_restart_with_a_colon_ref_resumes_the_same_sanitized_session(
    tmp_path: Path,
) -> None:
    """Combined path (#190 x #192): a stored ref containing characters pi
    rejects is translated deterministically at the pi boundary, so a daemon
    restart relaunches with the SAME sanitized id -- the session resumes,
    pi's >=0.76 launch gate passes, and the internal ref stays raw."""

    argv_log = tmp_path / "argv.jsonl"
    env = {
        "FAKE_PI_MODE": "settled",
        "FAKE_PI_ARGV_LOG": str(argv_log),
        "FAKE_PI_VALIDATE_SESSION_ID": "1",  # mirror pi >=0.76's launch gate
    }
    spec = _pi_spec(session_ref="squire:alw:usrb")

    first = _pi_process(spec, env=env)
    try:
        assert first.wait_ready(timeout=10.0)
        assert first.session_ref == "squire:alw:usrb"  # raw internally
        assert first.enqueue(
            HarnessDelivery(
                delivery_id="d-1",
                conversation_id="c-1",
                sender="s",
                recipient="agent:op:node-1:worker",
                message="before restart",
            )
        )
        [result] = _wait_results(first, 1)
        assert result.status is HarnessResultStatus.COMPLETED
    finally:
        first.stop()

    # Simulated daemon restart: a fresh process built from the persisted spec.
    second = _pi_process(spec, env=env)
    try:
        assert second.wait_ready(timeout=10.0)
        assert second.session_ref == "squire:alw:usrb"
        # Both launches passed pi's charset gate with the SAME deterministic
        # translation -- that sameness is what makes resume resume.
        assert _session_ids_seen(argv_log) == [
            "squire.alw.usrb",
            "squire.alw.usrb",
        ]
        assert second.enqueue(
            HarnessDelivery(
                delivery_id="d-2",
                conversation_id="c-1",
                sender="s",
                recipient="agent:op:node-1:worker",
                message="after restart",
            )
        )
        [result] = _wait_results(second, 1)
        assert result.status is HarnessResultStatus.COMPLETED
    finally:
        second.stop()


def test_pi_established_session_crash_reconnects_with_the_same_id(
    tmp_path: Path,
) -> None:
    """The cold-start fallback is only for a ref that never reached ready;
    a mid-session crash must resume the SAME id, not mint a new one."""

    argv_log = tmp_path / "argv.jsonl"
    marker = tmp_path / "crash-marker"
    process = _pi_process(
        _pi_spec(session_ref="good-pi-id"),
        env={
            "FAKE_PI_MODE": "crash",
            "FAKE_PI_STATE": str(marker),
            "FAKE_PI_ARGV_LOG": str(argv_log),
        },
    )
    try:
        assert process.wait_ready(timeout=10.0)
        assert process.enqueue(
            HarnessDelivery(
                delivery_id="d-1",
                conversation_id="c-1",
                sender="s",
                recipient="agent:op:node-1:worker",
                message="first",
            )
        )
        # The first fake pi dies mid-turn; the pump reconnects and the
        # delivery completes on the second process.
        [first] = _wait_results(process, 1)
        assert first.status is HarnessResultStatus.COMPLETED
        assert process.session_ref == "good-pi-id"
        assert _session_ids_seen(argv_log) == ["good-pi-id", "good-pi-id"]
    finally:
        process.stop()


# --------------------------------------------------------------------------- #
# codex: thread/resume on restart; a definitively-missing thread cold-starts
# --------------------------------------------------------------------------- #


def _codex_spec(session_ref: str | None) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="codex",
        name="codex-worker",
        headless=True,
        cwd=str(Path.cwd()),
        session_ref=session_ref,
    )


def _codex_process(
    mode: str, session_ref: str | None, log_path: Path
) -> CodexAppServerProcess:
    return CodexAppServerProcess(
        _codex_spec(session_ref),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_MODE": mode, "FAKE_CODEX_LOG": str(log_path)},
        reconnect_delay_seconds=0.01,
    )


def _wire_methods(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return [
        json.loads(line).get("method")
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _codex_delivery(delivery_id: str, message: str) -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient="agent:op:node-1:codex-worker",
        message=message,
    )


def test_codex_restart_resumes_the_persisted_thread(tmp_path: Path) -> None:
    log_path = tmp_path / "wire.jsonl"
    process = _codex_process("complete", "codex-thread-1", log_path)
    try:
        assert process.wait_ready(timeout=10.0)
        assert process.session_ref == "codex-thread-1"
        methods = _wire_methods(log_path)
        assert "thread/resume" in methods
        assert "thread/start" not in methods
        assert process.enqueue(_codex_delivery("d-1", "continue the thread"))
        [result] = _wait_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED
    finally:
        process.stop()


def test_codex_missing_thread_cold_starts_and_reports_the_new_id(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"
    process = _codex_process("resume-miss", "dead-thread", log_path)
    try:
        assert process.wait_ready(timeout=10.0)
        # Resume failed definitively (no rollout, not loaded): the worker
        # cold-started and now reports the replacement thread id, which the
        # daemon persists over the dead ref.
        assert process.session_ref == "codex-thread-1"
        methods = _wire_methods(log_path)
        assert "thread/resume" in methods
        assert "thread/start" in methods
        assert methods.index("thread/resume") < methods.index("thread/start")
        assert process.enqueue(_codex_delivery("d-1", "fresh start"))
        [result] = _wait_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED
    finally:
        process.stop()


# --------------------------------------------------------------------------- #
# claude: session_id pins fresh sessions, resume continues stored ones, and
# a dead resume target falls back to a fresh session inside the worker
# --------------------------------------------------------------------------- #


def _write_fake_sdk(package_dir: Path, record_path: Path) -> None:
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        """
import json
import os

_RECORD = os.environ.get("FAKE_SDK_RECORD")


def _record(options):
    if _RECORD:
        with open(_RECORD, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "resume": getattr(options, "resume", None),
                "session_id": getattr(options, "session_id", None),
            }) + "\\n")


class ClaudeAgentOptions:
    def __init__(self, cwd=None, model=None, system_prompt=None,
                 setting_sources=None, strict_mcp_config=None, extra_args=None,
                 mcp_servers=None, allowed_tools=None, resume=None,
                 session_id=None, disallowed_tools=None, max_buffer_size=None):
        self.resume = resume
        self.session_id = session_id


class ProcessError(Exception):
    def __init__(self, message, exit_code=None, stderr=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class ClaudeSDKClient:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        _record(self.options)
        dead = os.environ.get("FAKE_SDK_DEAD_RESUME", "")
        if dead and self.options.resume == dead:
            raise ProcessError(
                "Command failed", exit_code=1,
                stderr="Check stderr output for details",
            )
        return self

    async def __aexit__(self, *args):
        return False


class ResultMessage:
    def __init__(self, result=None, is_error=False):
        self.result = result
        self.is_error = is_error
""",
        encoding="utf-8",
    )


def _run_sdk_worker(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SDK_WORKER)],
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _clean_env(**extra: str) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", **extra}


def _ready_line(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    for line in result.stdout.splitlines():
        message = json.loads(line)
        if message.get("type") == "ready":
            return message
    raise AssertionError(f"worker never became ready: {result!r}")


def test_claude_worker_pins_a_fresh_session_id(tmp_path: Path) -> None:
    record = tmp_path / "sdk.jsonl"
    _write_fake_sdk(tmp_path / "claude_agent_sdk", record)

    result = _run_sdk_worker(
        _clean_env(
            PYTHONPATH=str(tmp_path),
            HYPRIAL_AGENT_SDK_OPTIONS=json.dumps({"sessionId": "minted-id"}),
            FAKE_SDK_RECORD=str(record),
        )
    )

    assert result.returncode == 0, result.stderr
    assert _ready_line(result)["sessionId"] == "minted-id"
    [seen] = [json.loads(line) for line in record.read_text().splitlines()]
    assert seen == {"resume": None, "session_id": "minted-id"}


def test_claude_worker_resumes_a_stored_session_id(tmp_path: Path) -> None:
    record = tmp_path / "sdk.jsonl"
    _write_fake_sdk(tmp_path / "claude_agent_sdk", record)

    result = _run_sdk_worker(
        _clean_env(
            PYTHONPATH=str(tmp_path),
            HYPRIAL_AGENT_SDK_OPTIONS=json.dumps({"resume": "stored-id"}),
            FAKE_SDK_RECORD=str(record),
        )
    )

    assert result.returncode == 0, result.stderr
    assert _ready_line(result)["sessionId"] == "stored-id"
    [seen] = [json.loads(line) for line in record.read_text().splitlines()]
    assert seen == {"resume": "stored-id", "session_id": None}


def test_claude_worker_dead_resume_fails_without_replacement(
    tmp_path: Path,
) -> None:
    record = tmp_path / "sdk.jsonl"
    _write_fake_sdk(tmp_path / "claude_agent_sdk", record)

    result = _run_sdk_worker(
        _clean_env(
            PYTHONPATH=str(tmp_path),
            HYPRIAL_AGENT_SDK_OPTIONS=json.dumps({"resume": "dead-id"}),
            FAKE_SDK_RECORD=str(record),
            FAKE_SDK_DEAD_RESUME="dead-id",
        )
    )

    assert result.returncode != 0
    assert "dead-id" in result.stderr
    assert '"type":"ready"' not in result.stdout.replace(" ", "")
    attempts = [json.loads(line) for line in record.read_text().splitlines()]
    assert attempts == [
        {"resume": "dead-id", "session_id": None},
    ]


def test_claude_client_passes_session_controls_into_worker_options() -> None:
    spec = HarnessLaunchSpec(harness="claude", name="cc", headless=True, cwd="/w")

    resumed = IsolatedAgentSdkClient(
        spec, worker_command=("true",), resume="stored-id"
    )
    assert resumed.options["resume"] == "stored-id"
    assert "sessionId" not in resumed.options

    fresh = IsolatedAgentSdkClient(
        spec, worker_command=("true",), session_id="minted-id"
    )
    assert fresh.options["sessionId"] == "minted-id"
    assert "resume" not in fresh.options

    plain = IsolatedAgentSdkClient(spec, worker_command=("true",))
    assert "resume" not in plain.options
    assert "sessionId" not in plain.options

    with pytest.raises(ValueError, match="mutually exclusive"):
        IsolatedAgentSdkClient(
            spec, worker_command=("true",), resume="a", session_id="b"
        )


def test_claude_process_holder_selects_session_id_then_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def capture(spec, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.agent_sdk.create_claude_sdk_client", capture
    )

    class _IdleClient:
        running = True
        pid = None

        async def __aenter__(self) -> "_IdleClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def query(self, prompt: str) -> None:
            raise AssertionError("no turns in this test")

        async def interrupt(self) -> None:
            pass

    fresh = ClaudeAgentSdkProcess(
        HarnessLaunchSpec(harness="claude", name="cc", headless=True, cwd="/w"),
        client_factory=lambda: _IdleClient(),
    )
    try:
        assert fresh.session_ref  # minted before the first connect
        fresh._build_client()
        assert captured[-1]["session_id"] == fresh.session_ref
        assert captured[-1]["resume"] is None
        # Once the worker reports the effective id, later clients resume it.
        fresh._session_established("established-id")
        fresh._build_client()
        assert captured[-1]["resume"] == "established-id"
        assert captured[-1]["session_id"] is None
        assert fresh.session_ref == "established-id"
    finally:
        fresh.stop()

    resumed = ClaudeAgentSdkProcess(
        HarnessLaunchSpec(
            harness="claude", name="cc2", headless=True, cwd="/w",
            session_ref="persisted-id",
        ),
        client_factory=lambda: _IdleClient(),
    )
    try:
        assert resumed.session_ref == "persisted-id"
        resumed._build_client()
        assert captured[-1]["resume"] == "persisted-id"
        assert captured[-1]["session_id"] is None
    finally:
        resumed.stop()
