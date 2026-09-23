"""Managed parent process for the packaged python workers (jev, user-proxy).

Both kinds share the v1 JSONL wire, admission, and custody.  They differ in
the ready frame, the call payload, and one terminal frame: user-proxy may
answer ``forward`` -- "send this AS me to X" -- which the daemon executes
(``HarnessResult.forward_to``); the child never holds a send path.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.daemon.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
    ProcessLiveness,
    ProcessLivenessState,
    classify_harness_failure,
)
from hyprial.daemon.desired_state import HarnessLaunchSpec
from hyprial.log import Logger

from .capabilities import concurrency
from .common import HarnessStartError, summarize_stderr
from .streaming import ConcurrentTurnProcess
from .worker_channel import WorkerChannel

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_MAX_LINE_BYTES = 8 * 1024 * 1024
_DEFAULT_COMMAND = (
    sys.executable,
    "-m",
    "hyprial.harnesses._python_worker",
    "--kind",
    "jev",
)
#: The kinds this parent can host.  user-proxy has no default command: its
#: child needs the person's ``--route``, which only the start request knows.
PYTHON_WORKER_KINDS = frozenset({"jev", "user-proxy"})


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


@dataclass(slots=True)
class _DeliveryState:
    delivery: HarnessDelivery
    accepted_at: float
    terminal: bool = False


class PythonWorkerTurnAdapter(ConcurrentTurnProcess):
    """A bounded, out-of-order JSONL worker with daemon-owned admission."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        command: tuple[str, ...] | None = None,
        env: Mapping[str, str] | None = None,
        worker_channel: WorkerChannel | None = None,
        complete_launch: Any | None = None,
        startup_timeout_seconds: float = 10.0,
        stop_timeout_seconds: float = 5.0,
        logger: Logger | None = None,
    ) -> None:
        if spec.harness not in PYTHON_WORKER_KINDS or not spec.headless:
            raise ValueError("python worker requires a managed headless jev or user-proxy spec")
        if complete_launch is not None and env is not None:
            raise ValueError("complete child environment cannot be combined with a partial env mapping")
        if startup_timeout_seconds <= 0 or stop_timeout_seconds <= 0:
            raise ValueError("worker timeouts must be positive")
        self.kind = spec.harness
        super().__init__(concurrency=concurrency(self.kind, headless=True).concurrency)
        self.spec = spec
        self.worker_channel = worker_channel
        self.max_in_flight = concurrency(self.kind, headless=True).concurrency
        self.command = tuple(
            command or spec.command or (_DEFAULT_COMMAND if self.kind == "jev" else ())
        )
        if not self.command:
            raise ValueError("python worker command must not be empty")
        self._env = (
            complete_launch.environment.for_exec()
            if complete_launch is not None
            else (dict(env) if env is not None else {})
        )
        self._logger = logger or self._make_logger()
        self._stop_timeout_seconds = stop_timeout_seconds
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._records: dict[str, _DeliveryState] = {}
        self._results: queue.Queue[HarnessResult] = queue.Queue()
        self._stderr = bytearray()
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._ready_error: str | None = None
        self._started = False
        self._terminal_ids: set[str] = set()
        self._process: subprocess.Popen[bytes] | None = None
        self.last_error: str | None = None
        self.startup: dict[str, object] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._start(startup_timeout_seconds)

    def _make_logger(self) -> Logger | None:
        state_dir = None
        if self.worker_channel is not None:
            state_dir = self.worker_channel.state_dir
        elif self._env.get("HARNESS_STATE_DIR"):
            state_dir = Path(self._env["HARNESS_STATE_DIR"])
        if state_dir is None:
            return None
        return Logger.worker(state_dir, runtime=self.kind, name=self.spec.name)

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    @property
    def running(self) -> bool:
        process = self._process
        return bool(
            process is not None
            and process.poll() is None
            and self._ready.is_set()
            and not self._stopping.is_set()
        )

    @property
    def in_flight(self) -> int:
        with self._lock:
            return sum(not record.terminal for record in self._records.values())

    @property
    def queue_depth(self) -> int:
        """The daemon's pending inbox projection is not owned by this child."""

        return 0

    def status_counters(self, *, queue_depth: int = 0) -> dict[str, int]:
        return {
            "maxInFlight": self.max_in_flight,
            "inFlight": self.in_flight,
            "queueDepth": max(0, int(queue_depth)),
        }

    def wait_ready(self, *, timeout: float | None = None) -> bool:
        ready = self._ready.wait(timeout)
        return ready and self.startup is not None

    def liveness(self) -> ProcessLiveness:
        process = self._process
        if process is None:
            return ProcessLiveness(ProcessLivenessState.DEAD, observed=True)
        code = process.poll()
        if code is None:
            return ProcessLiveness(ProcessLivenessState.ALIVE, observed=self._ready.is_set(), pid=process.pid)
        return ProcessLiveness(
            ProcessLivenessState.DEAD,
            observed=True,
            pid=process.pid,
            detail=self.last_error or f"python worker exited with code {code}",
        )

    def enqueue(self, delivery: HarnessDelivery) -> bool:
        if not delivery.delivery_id or not delivery.message:
            raise ValueError("harness delivery requires an id and message")
        with self._lock:
            if not self.running:
                return False
            if delivery.delivery_id in self._records or delivery.delivery_id in self._terminal_ids:
                return False
            if self.in_flight >= self.max_in_flight:
                return False
            payload: object
            if self.kind == "user-proxy":
                # A relay needs who sent it and the text, nothing else.
                payload = {"from": delivery.sender, "message": delivery.message}
            else:
                try:
                    payload = json.loads(delivery.message)
                except (TypeError, ValueError):
                    # Keep the turn accepted so the child owns the decode failure;
                    # the daemon must see a failed turn, not a lost inbox row.
                    payload = delivery.message
            self._records[delivery.delivery_id] = _DeliveryState(delivery, time.monotonic())
            try:
                self._write_frame(
                    {"v": 1, "op": "call", "id": delivery.delivery_id, "payload": payload}
                )
            except (BrokenPipeError, OSError) as error:
                self._records.pop(delivery.delivery_id, None)
                self._protocol_failure(f"worker input failed: {type(error).__name__}")
                return False
        self._log_turn("worker.turn.started", delivery)
        return True

    def drain_results(self) -> tuple[HarnessResult, ...]:
        results: list[HarnessResult] = []
        while True:
            try:
                result = self._results.get_nowait()
                results.append(result)
                with self._lock:
                    record = self._records.get(result.delivery_id)
                    if record is not None and record.terminal:
                        self._records.pop(result.delivery_id, None)
            except queue.Empty:
                return tuple(results)

    def drain_progress(self) -> tuple[object, ...]:
        return ()

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
        del delivery_id, timeout
        return False

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                self._write_frame({"v": 1, "op": "stop"})
                process.wait(timeout=self._stop_timeout_seconds)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
        reader = self._reader_thread
        if reader is not None:
            reader.join(timeout=1.0)
        stderr = self._stderr_thread
        if stderr is not None:
            stderr.join(timeout=1.0)
        for stream in (
            getattr(process, "stdin", None),
            getattr(process, "stdout", None),
            getattr(process, "stderr", None),
        ):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _start(self, timeout: float) -> None:
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=self.spec.cwd or None,
                env=self._env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise HarnessStartError(self.kind, self.command, str(error)) from error
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._reader_thread = threading.Thread(target=self._read_stdout, name=f"hyprial-{self.kind}-reader-{self.spec.name}", daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, name=f"hyprial-{self.kind}-stderr-{self.spec.name}", daemon=True)
        self._reader_thread.start()
        self._stderr_thread.start()
        self._started = True
        if not self._ready.wait(timeout):
            detail = self._ready_error or summarize_stderr(bytes(self._stderr)) or "worker did not become ready"
            self.last_error = detail
            self.stop()
            raise HarnessStartError(self.kind, self.command, detail, stderr_tail=summarize_stderr(bytes(self._stderr)))
        if self.startup is None:
            detail = self._ready_error or "worker ready frame was invalid"
            self.stop()
            raise HarnessStartError(self.kind, self.command, detail)

    def _write_frame(self, frame: dict[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise BrokenPipeError("worker stdin is unavailable")
        payload = json.dumps(frame, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > _MAX_LINE_BYTES:
            raise ValueError("worker frame exceeds 8 MiB")
        with self._write_lock:
            process.stdin.write(payload)
            process.stdin.flush()

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                return
            with self._lock:
                self._stderr.extend(chunk)
                del self._stderr[:-16384]

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = process.stdout.readline(_MAX_LINE_BYTES + 1)
                if not line:
                    if not self._stopping.is_set():
                        self._protocol_failure("worker stdout closed")
                    return
                if len(line) > _MAX_LINE_BYTES or not line.endswith(b"\n") or line.endswith(b"\r\n"):
                    self._protocol_failure("worker emitted an invalid JSONL frame")
                    return
                try:
                    frame = json.loads(
                        line.decode("utf-8"),
                        object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=_reject_non_finite,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._protocol_failure("worker emitted invalid JSON")
                    return
                if not isinstance(frame, dict) or frame.get("v") != 1:
                    self._protocol_failure("worker emitted an unsupported protocol frame")
                    return
                self._handle_frame(frame)
        except (OSError, ValueError) as error:
            self._protocol_failure(f"worker output failed: {type(error).__name__}")

    def _handle_frame(self, frame: dict[str, object]) -> None:
        frame_type = frame.get("type")
        if frame_type == "ready":
            self._handle_ready(frame)
            return
        if frame_type == "metric":
            self._handle_metric(frame)
            return
        if frame_type in {"result", "error"} or (
            frame_type == "forward" and self.kind == "user-proxy"
        ):
            self._handle_terminal(frame)
            return
        self._protocol_failure("worker emitted an unknown frame type")

    def _handle_ready(self, frame: dict[str, object]) -> None:
        if self._ready.is_set():
            self._protocol_failure("worker emitted duplicate ready frame")
            return
        if self.kind == "user-proxy":
            self._handle_user_proxy_ready(frame)
            return
        required = {"v", "type", "kind", "pid", "effectiveConfig", "hyprialCommit", "typesafeSdkVersion", "scriptSha256", "startupHash", "credentialSource"}
        if set(frame) != required or frame.get("kind") != "jev":
            self._ready_error = "worker ready frame has invalid fields"
            self._protocol_failure(self._ready_error)
            return
        if not isinstance(frame.get("pid"), int) or frame["pid"] <= 0:
            self._ready_error = "worker ready pid is invalid"
            self._protocol_failure(self._ready_error)
            return
        if not isinstance(frame.get("hyprialCommit"), str) or not _HEX40.fullmatch(frame["hyprialCommit"]):
            self._ready_error = "worker ready commit is invalid"
            self._protocol_failure(self._ready_error)
            return
        for field in ("scriptSha256", "startupHash"):
            if not isinstance(frame.get(field), str) or not _HEX64.fullmatch(frame[field]):
                self._ready_error = f"worker ready {field} is invalid"
                self._protocol_failure(self._ready_error)
                return
        from . import _python_worker

        expected_script_hash = hashlib.sha256(
            Path(_python_worker.__file__).read_bytes()
        ).hexdigest()
        if frame["scriptSha256"] != expected_script_hash:
            self._ready_error = "worker ready script hash is invalid"
            self._protocol_failure(self._ready_error)
            return
        if frame.get("credentialSource") not in {None, "environment", "file"}:
            self._ready_error = "worker ready credential source is invalid"
            self._protocol_failure(self._ready_error)
            return
        if frame.get("typesafeSdkVersion") != "0.7.0":
            self._ready_error = "worker ready SDK version is invalid"
            self._protocol_failure(self._ready_error)
            return
        config = frame.get("effectiveConfig")
        if not isinstance(config, dict) or config.get("concurrency") != self.max_in_flight or config.get("mode") != "pool" or config.get("protocolVersion") != 1:
            self._ready_error = "worker ready effective config is invalid"
            self._protocol_failure(self._ready_error)
            return
        startup_input = {
            "hyprialCommit": frame["hyprialCommit"],
            "kind": frame["kind"],
            "effectiveConfig": config,
            "typesafeSdkVersion": frame["typesafeSdkVersion"],
            "scriptSha256": frame["scriptSha256"],
        }
        from ._python_worker import startup_hash

        expected_hash = startup_hash(startup_input)
        if frame["startupHash"] != expected_hash:
            self._ready_error = "worker ready startup hash is invalid"
            self._protocol_failure(self._ready_error)
            return
        self.startup = dict(frame)
        self._ready.set()
        self._log_event(
            "script.ready",
            node="script-worker",
            actorId=self._actor_id,
            kind=self.kind,
            pid=frame["pid"],
            hyprialCommit=frame["hyprialCommit"],
            effectiveConfig=config,
            typesafeSdkVersion=frame["typesafeSdkVersion"],
            scriptSha256=frame["scriptSha256"],
            startupHash=frame["startupHash"],
            credentialSource=frame["credentialSource"],
        )
        self._log_event(
            "worker.started",
            node="worker-process",
            actorId=self._actor_id,
            kind=self.kind,
            pid=frame["pid"],
            generation=1,
            hyprialCommit=frame["hyprialCommit"],
        )

    def _handle_user_proxy_ready(self, frame: dict[str, object]) -> None:
        if set(frame) != {"v", "type", "kind", "pid", "hyprialCommit", "scriptSha256"} or frame.get("kind") != "user-proxy":
            self._ready_error = "worker ready frame has invalid fields"
            self._protocol_failure(self._ready_error)
            return
        pid = frame.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            self._ready_error = "worker ready pid is invalid"
            self._protocol_failure(self._ready_error)
            return
        if not isinstance(frame.get("hyprialCommit"), str) or not _HEX40.fullmatch(frame["hyprialCommit"]):
            self._ready_error = "worker ready commit is invalid"
            self._protocol_failure(self._ready_error)
            return
        from . import _user_proxy_worker

        expected_script_hash = hashlib.sha256(
            Path(_user_proxy_worker.__file__).read_bytes()
        ).hexdigest()
        if frame.get("scriptSha256") != expected_script_hash:
            self._ready_error = "worker ready script hash is invalid"
            self._protocol_failure(self._ready_error)
            return
        self.startup = dict(frame)
        self._ready.set()
        self._log_event(
            "worker.started",
            node="worker-process",
            actorId=self._actor_id,
            kind=self.kind,
            pid=pid,
            generation=1,
            hyprialCommit=frame["hyprialCommit"],
        )

    def _handle_metric(self, frame: dict[str, object]) -> None:
        request_id = frame.get("id")
        segment = frame.get("segment")
        if not isinstance(request_id, str) or not request_id or segment not in {"decode", "call", "send"}:
            self._protocol_failure("worker metric frame is invalid")
            return
        if segment == "send":
            return
        with self._lock:
            record = self._records.get(request_id)
        if record is None:
            self._protocol_failure("worker metric id is not reserved")
            return
        if not isinstance(frame.get("durationMs"), int) or frame["durationMs"] < 0 or not isinstance(frame.get("ok"), bool):
            self._protocol_failure("worker metric fields are invalid")
            return
        self._log_script_metric(record.delivery, frame)

    def _handle_terminal(self, frame: dict[str, object]) -> None:
        request_id = frame.get("id")
        if not isinstance(request_id, str) or not request_id:
            self._protocol_failure("worker terminal id is invalid")
            return
        with self._lock:
            record = self._records.get(request_id)
            duplicate = record is not None and record.terminal
        if record is None or duplicate:
            self._protocol_failure("worker terminal id is not reserved")
            return
        send_started = time.monotonic()
        if frame.get("type") == "forward":
            to = frame.get("to")
            message = frame.get("message")
            if (
                set(frame) != {"v", "type", "id", "ok", "to", "message"}
                or frame.get("ok") is not True
                or not isinstance(to, str)
                or not to
                or not isinstance(message, str)
            ):
                self._protocol_failure("worker forward frame is invalid")
                return
            result = HarnessResult(
                request_id,
                record.delivery.recipient,
                HarnessResultStatus.COMPLETED,
                output=message,
                forward_to=to,
            )
            self._log_turn("worker.turn.completed", record.delivery, forwardTo=to)
        elif frame.get("type") == "result":
            output = frame.get("output")
            if frame.get("ok") is not True or not isinstance(output, dict):
                self._protocol_failure("worker result frame is invalid")
                return
            if self.kind == "user-proxy":
                # The person reads this directly (the "name a recipient"
                # hint), so it is the text, not a JSON rendering of it.
                if set(output) != {"message"} or not isinstance(output["message"], str):
                    self._protocol_failure("worker result frame is invalid")
                    return
                text = output["message"]
            else:
                text = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            result = HarnessResult(request_id, record.delivery.recipient, HarnessResultStatus.COMPLETED, output=text)
            self._log_turn("worker.turn.completed", record.delivery)
        else:
            code = frame.get("code")
            message = frame.get("message")
            stage = frame.get("stage")
            retryable = frame.get("retryable")
            if frame.get("ok") is not False or not isinstance(code, str) or not isinstance(message, str) or stage not in {"decode", "call", "send", "protocol"} or not isinstance(retryable, bool):
                self._protocol_failure("worker error frame is invalid")
                return
            result = HarnessResult(
                request_id,
                record.delivery.recipient,
                HarnessResultStatus.FAILED,
                error=message[:500],
                failure_code=code or classify_harness_failure(message),
            )
            self._log_turn("worker.turn.failed", record.delivery, failure=message[:500], failureCode=code)
            if stage == "decode":
                self._log_script_metric(
                    record.delivery,
                    {
                        "segment": "call",
                        "durationMs": 0,
                        "ok": False,
                        "skipped": True,
                    },
                )
            self._log_event(
                "script.error",
                **self._delivery_fields(record.delivery),
                node="script-worker",
                kind=self.kind,
                stage=stage,
                code=code,
                retryable=retryable,
                errorType="TypeSafeError" if stage == "call" and self.kind == "jev" else "WorkerError",
                message=message[:500],
            )
        with self._lock:
            record.terminal = True
            self._terminal_ids.add(request_id)
        self._results.put(result)
        self._log_script_send(record.delivery, result.status is HarnessResultStatus.COMPLETED, started=send_started)

    def _protocol_failure(self, detail: str) -> None:
        with self._lock:
            if self.last_error is not None and self.last_error.startswith("worker protocol"):
                return
            self.last_error = f"worker protocol failure: {detail}"
            records = tuple(
                record for record in self._records.values() if not record.terminal
            )
            for record in records:
                record.terminal = True
            self._terminal_ids.update(item.delivery.delivery_id for item in records)
        self._ready_error = self.last_error
        startup_failure = not self._ready.is_set()
        self._ready.set()
        if not records:
            self._log_event(
                "script.error",
                node="script-worker",
                kind=self.kind,
                stage="protocol",
                code="HARNESS_START_FAILED" if startup_failure else "HARNESS_TRANSIENT_FAILURE",
                retryable=not startup_failure,
                errorType="ProtocolError",
                message=detail[:500],
            )
        for record in records:
            self._log_event(
                "script.error",
                **self._delivery_fields(record.delivery),
                node="script-worker",
                kind=self.kind,
                stage="protocol",
                code="HARNESS_TRANSIENT_FAILURE",
                retryable=True,
                errorType="ProtocolError",
                message=detail,
            )
            self._results.put(
                HarnessResult(
                    record.delivery.delivery_id,
                    record.delivery.recipient,
                    HarnessResultStatus.FAILED,
                    error=detail[:500],
                    failure_code="HARNESS_TRANSIENT_FAILURE",
                )
            )

    @property
    def _actor_id(self) -> str | None:
        if self.worker_channel is not None:
            return self.worker_channel.actor
        return self._env.get("HYPRIAL_WORKER_ACTOR")

    @staticmethod
    def _delivery_fields(delivery: HarnessDelivery) -> dict[str, object]:
        return {
            "messageId": delivery.delivery_id,
            "correlationId": delivery.delivery_id,
            "actorId": delivery.recipient,
            "conversationId": delivery.conversation_id,
            "sender": delivery.sender,
            "recipient": delivery.recipient,
            "deliveryId": delivery.delivery_id,
        }

    def _log_turn(self, event: str, delivery: HarnessDelivery, **fields: object) -> None:
        self._log_event("worker.turn.started" if event.endswith("started") else event, **self._delivery_fields(delivery), node="worker-turn", **fields)

    def _log_script_metric(self, delivery: HarnessDelivery, frame: Mapping[str, object]) -> None:
        fields: dict[str, object] = {
            **self._delivery_fields(delivery),
            "node": "script-worker",
            "kind": self.kind,
            "segment": frame.get("segment"),
            "durationMs": frame.get("durationMs"),
            "ok": frame.get("ok"),
            "inFlight": frame.get("inFlight", self.in_flight),
            "maxInFlight": self.max_in_flight,
            "queueDepth": 0,
        }
        if frame.get("skipped") is True:
            fields["skipped"] = True
        self._log_event(
            "script.metric",
            **fields,
        )

    def _log_script_send(
        self, delivery: HarnessDelivery, ok: bool, *, started: float
    ) -> None:
        self._log_event(
            "script.metric",
            **self._delivery_fields(delivery),
            node="script-worker",
            kind=self.kind,
            segment="send",
            durationMs=max(0, int((time.monotonic() - started) * 1000)),
            ok=ok,
            inFlight=self.in_flight,
            maxInFlight=self.max_in_flight,
            queueDepth=0,
        )

    def _log_event(self, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        try:
            self._logger.info(event, **fields)
        except OSError:
            # Local observability is fail-open; turn custody is not.
            return


# Compatibility export for callers that used the first implementation slice;
# the concrete class is a turn-family adapter, not a third lifecycle family.
PythonHarnessProcess = PythonWorkerTurnAdapter


__all__ = ["PythonHarnessProcess", "PythonWorkerTurnAdapter"]
