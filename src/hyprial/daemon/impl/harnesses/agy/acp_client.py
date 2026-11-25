# Decision record (file header; no inline comments by repository rule):
# - Pure asyncio.create_subprocess_exec without POSIX spawn knobs or platform
#   branches ensures full W001-W020 Windows portability compliance.
# - One background stdout reader task owns all JSON-RPC demultiplexing, while an
#   asyncio.Lock and monotonically increasing turn_generation guarantee single-
#   flight turns and drop stale post-cancel notifications.
# hyprial-unbounded-wait-allow: acquire - awaited asyncio turn lock serializes turns and is cancellable by its owning task
# hyprial-unbounded-wait-allow: get - awaited asyncio turn queue is drained by the turn pump until done or eof; cancellable by its owning task
# hyprial-unbounded-wait-allow: result - the done-tag future is already resolved by the single stdout reader before its event is queued
# hyprial-unbounded-wait-allow: wait - the background exit watcher awaits process exit for the lifecycle log; cancellable by its owning task
"""ACP stdio TurnClient implementation for headless Antigravity (agy)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Self

from hyprial import __version__
from hyprial.daemon.impl.harnesses.agy.native_env import (
    AgyAgentHomeError,
    prepare_agy_runtime_context,
)
from hyprial.daemon.impl.harnesses.agy.projection import (
    project_agy_launch,
    select_permission_option,
    validate_session_config_choice,
)
from hyprial.daemon.impl.harnesses.agy.protocol import (
    ACP_AUTH_ERROR_CODE,
    ACP_CANCEL_TIMEOUT_SECONDS_DEFAULT,
    ACP_CLOSE_TOTAL_SECONDS,
    ACP_JSONRPC_UNKNOWN_REQUEST_CODE,
    ACP_REQUEST_TIMEOUT_SECONDS_DEFAULT,
    STREAM_LIMIT_BYTES,
    ActiveTurnState,
    AgyRpcError,
    AgySessionNotFoundError,
    AgyStartPreconditionError,
    JsonRpcId,
    build_initialize_params,
    build_rpc_error_outcome,
    build_session_params,
    build_turn_outcome,
    classify_jsonrpc_message,
    close_acp_subprocess,
    decode_jsonrpc_line,
    decode_rpc_error,
    drain_stderr_tail,
    encode_jsonrpc_error,
    encode_jsonrpc_notification,
    encode_jsonrpc_request,
    encode_jsonrpc_response,
    extract_known_session_ids,
    extract_progress_observation,
    extract_session_update_text,
    format_exited_error,
    request_id_key,
    supports_session_resume,
)
from hyprial.daemon.impl.harnesses.protocol.common import summarize_stderr
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel
from hyprial.identity import AgentRuntimeContext, ChildEnvironmentLaunch
from hyprial.kernel import HarnessLaunchSpec, Logger


class AgyAcpClient:
    """TurnClient speaking ACP stdio JSON-RPC 2.0 to ``agy acp``."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        session_ref: str | None = None,
        *,
        command: tuple[str, ...] = ("agy",),
        env: Mapping[str, str] | None = None,
        worker_channel: WorkerChannel | None = None,
        log_path: Path | None = None,
        request_timeout_seconds: float = ACP_REQUEST_TIMEOUT_SECONDS_DEFAULT,
        cancel_timeout_seconds: float = ACP_CANCEL_TIMEOUT_SECONDS_DEFAULT,
        complete_launch: ChildEnvironmentLaunch | None = None,
        runtime_launch_custody: (
            Callable[[AgentRuntimeContext], AbstractContextManager[None]] | None
        ) = None,
        provider_environment: Mapping[str, str] | None = None,
        strict_resume: bool = True,
        allow_load_fallback: bool = True,
        session_root: Path | None = None,
        logger: Logger | None = None,
    ) -> None:
        if complete_launch is not None and env is not None:
            raise ValueError("complete child environment cannot be combined with a partial env mapping")
        self.spec = spec
        self._complete_launch = complete_launch
        self._runtime_launch_custody = runtime_launch_custody
        base_environment = (
            complete_launch.environment.for_exec()
            if complete_launch is not None
            else {**os.environ, **(env or {})}
        )
        runtime_context = complete_launch.runtime_context if complete_launch is not None else None
        channel_context = worker_channel.runtime_context if worker_channel is not None else None
        if complete_launch is not None and worker_channel is not None and channel_context is not runtime_context:
            raise AgyAgentHomeError("agy worker channel and child launch disagree on runtime context")
        if runtime_context is not None:
            if complete_launch is None or runtime_context.actor != complete_launch.actor:
                raise AgyAgentHomeError("agy runtime context actor does not match the child launch")
            if runtime_context.harness != "agy":
                raise AgyAgentHomeError("agy runtime context requires harness 'agy'")
            if base_environment.get("HOME") != str(runtime_context.roots.tool_home):
                raise AgyAgentHomeError("complete child environment disagrees with agy runtime context")
            if session_root is not None and session_root != runtime_context.roots.session_root:
                raise AgyAgentHomeError("explicit session root disagrees with agy runtime context")
            resolved_session_root = runtime_context.roots.session_root
        else:
            if complete_launch is not None and complete_launch.agent_home_profile_applied:
                raise AgyAgentHomeError("P2 agy launch requires an AgentRuntimeContext")
            if complete_launch is None and channel_context is not None:
                raise AgyAgentHomeError("agy runtime context requires complete_launch")
            resolved_session_root = session_root
        self._runtime_context = runtime_context
        self._env = dict(env) if env is not None else None
        self._worker_channel = worker_channel
        self._provider_environment = dict(provider_environment or {})
        self._request_timeout_seconds = request_timeout_seconds
        self._cancel_timeout_seconds = cancel_timeout_seconds
        self._strict_resume = strict_resume
        self._allow_load_fallback = allow_load_fallback
        self._persisted_session_ref = session_ref if session_ref is not None else spec.session_ref

        self._projection = project_agy_launch(
            spec,
            command=command,
            session_root=resolved_session_root,
            worker_channel=worker_channel,
        )
        self.command = self._projection.argv

        state_dir = worker_channel.state_dir if worker_channel is not None else None
        if log_path is not None:
            state_dir = Path(log_path).parents[2]
        self._logger = logger or (
            Logger.worker(state_dir, runtime="agy", name=spec.name) if state_dir is not None else None
        )
        self._process: asyncio.subprocess.Process | None = None
        self._spawned = threading.Event()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._expected_stop = False
        self._next_request_id = 0
        self._pending_requests: dict[tuple[type[int] | type[str], JsonRpcId], asyncio.Future[object]] = {}
        self._write_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._turn_generation = 0
        self._active_turn: ActiveTurnState | None = None
        self._run_activity: tuple[int, bool, int | None] | None = None
        self._suppressing_replay = False
        self._session_tainted_after_cancel = False
        self._config_options: list[dict[str, object]] = []
        self.session_id: str | None = None
        self.reached_ready = False
        self.startup_warnings: tuple[str, ...] = ()

    def _spawn_environment(self) -> dict[str, str]:
        if self._complete_launch is not None:
            return {**self._complete_launch.environment.for_exec(), **self._provider_environment}
        channel_env = self._worker_channel.identity_environment() if self._worker_channel is not None else {}
        return {**os.environ, **(self._env or {}), **channel_env, **self._provider_environment}

    async def __aenter__(self) -> Self:
        environment = self._spawn_environment()
        if self._runtime_context is not None:
            prepare_agy_runtime_context(self._runtime_context)
        custody = (
            nullcontext()
            if self._runtime_context is None or self._runtime_launch_custody is None
            else self._runtime_launch_custody(self._runtime_context)
        )
        try:
            with custody:
                self._process = await asyncio.create_subprocess_exec(
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.spec.cwd,
                    env=environment,
                    limit=STREAM_LIMIT_BYTES,
                )
            self._spawned.set()
            self._expected_stop = False
            self._write_log("worker.started", pid=self._process.pid)
            self._stderr_task = asyncio.create_task(drain_stderr_tail(self._process, self._stderr_tail))
            self._stdout_task = asyncio.create_task(self._read_stdout_loop(self._process))
            self._exit_task = asyncio.create_task(self._watch_process_exit(self._process))
            if self._process.returncode is not None:
                raise format_exited_error(self._stderr_tail)

            init_result = await self._request(
                "initialize",
                build_initialize_params(__version__),
                timeout=self._request_timeout_seconds,
            )
            init_map = init_result if isinstance(init_result, Mapping) else {}
            if environment.get("GEMINI_API_KEY", "").strip():
                await self._authenticate_api_key()

            session_result = await self._open_session_with_auth_retry(init_map)
            if not isinstance(session_result, Mapping):
                raise ConnectionError("agy ACP session setup returned non-object result")
            session_id = session_result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise ConnectionError("agy ACP session setup missing sessionId")
            self.session_id = session_id
            await self._apply_session_config(session_id, session_result)
            self.reached_ready = True
            self._write_log("worker.ready", pid=self._process.pid, sessionId=session_id)
            return self
        except BaseException:
            await self._close()
            raise

    async def __aexit__(self, *args: object) -> bool:
        del args
        await self._close()
        return False

    async def _authenticate_api_key(self) -> None:
        try:
            await self._request("authenticate", {"methodId": "gemini-api-key"}, timeout=self._request_timeout_seconds)
        except AgyRpcError as error:
            raise AgyStartPreconditionError(f"authentication failed: {error.message}") from error

    async def _open_session_with_auth_retry(self, init_result: Mapping[str, object]) -> object:
        try:
            return await self._open_or_resume_session(init_result)
        except AgyRpcError as error:
            if error.code != ACP_AUTH_ERROR_CODE:
                raise
            await self._authenticate_api_key()
            return await self._open_or_resume_session(init_result)

    async def _open_or_resume_session(self, init_result: Mapping[str, object]) -> object:
        base_params = build_session_params(
            cwd=self.spec.cwd or os.getcwd(),
            mcp_servers=self._projection.mcp_servers,
            system_prompt_append=self._projection.system_prompt_append,
        )
        target_sid = self._persisted_session_ref
        if target_sid is None:
            return await self._request("session/new", base_params, timeout=self._request_timeout_seconds)
        list_result = await self._request("session/list", {"cwd": base_params["cwd"]}, timeout=self._request_timeout_seconds)
        if target_sid not in extract_known_session_ids(list_result):
            if self._strict_resume:
                raise AgySessionNotFoundError(f"agy session {target_sid!r} not found in session/list")
            return await self._request("session/new", base_params, timeout=self._request_timeout_seconds)
        resume_params = {**base_params, "sessionId": target_sid}
        if supports_session_resume(init_result):
            try:
                previous = self._suppressing_replay
                self._suppressing_replay = True
                try:
                    return await self._request("session/resume", resume_params, timeout=self._request_timeout_seconds)
                finally:
                    self._suppressing_replay = previous
            except AgyRpcError as error:
                if error.code != ACP_JSONRPC_UNKNOWN_REQUEST_CODE or not self._allow_load_fallback:
                    raise
        if not self._allow_load_fallback:
            raise AgyStartPreconditionError("agy session/resume is unsupported")
        previous = self._suppressing_replay
        self._suppressing_replay = True
        try:
            return await self._request("session/load", resume_params, timeout=self._request_timeout_seconds)
        finally:
            self._suppressing_replay = previous

    def _update_config_options(self, result: object) -> None:
        if isinstance(result, Mapping) and isinstance(result.get("configOptions"), list):
            self._config_options = [dict(item) for item in result["configOptions"] if isinstance(item, Mapping)]

    async def _set_config_option(self, session_id: str, config_id: str, value: str) -> None:
        try:
            result = await self._request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": config_id, "value": value},
                timeout=self._request_timeout_seconds,
            )
        except AgyRpcError as error:
            raise AgyStartPreconditionError(f"agy rejected config {config_id}={value!r}: {error.message}") from error
        self._update_config_options(result)

    async def _apply_session_config(self, session_id: str, session_result: Mapping[str, object]) -> None:
        self._update_config_options(session_result)
        model = self._projection.model
        for config_id, value in (
            ("model", model),
            ("thought_level", self._projection.thought_level),
            ("mode", self._projection.mode),
        ):
            if value is None:
                continue
            validate_session_config_choice(
                config_id=config_id,
                value=value,
                session_result=session_result,
                config_options=self._config_options,
                model_name=model,
            )
            await self._set_config_option(session_id, config_id, value)

    async def query(self, prompt: str) -> None:
        """Start one strictly serialized ACP session/prompt turn."""

        await self._turn_lock.acquire()
        try:
            if self._session_tainted_after_cancel:
                preserved_session = self.session_id
                await self._close()
                self._persisted_session_ref = preserved_session or self._persisted_session_ref
                self.session_id = None
                self.reached_ready = False
                await self.__aenter__()
                self._session_tainted_after_cancel = False
            if not self.running or self.session_id is None:
                raise ConnectionError("agy ACP session is not running")
            self._turn_generation += 1
            generation = self._turn_generation
            loop = asyncio.get_running_loop()
            turn = ActiveTurnState(
                generation=generation,
                queue=asyncio.Queue(),
                text_chunks=[],
                cancel_future=loop.create_future(),
            )
            self._active_turn = turn
            self._run_activity = (generation, False, time.time_ns() // 1_000_000)
            req_id = self._allocate_request_id()
            prompt_future: asyncio.Future[object] = loop.create_future()
            turn.prompt_future = prompt_future
            turn.prompt_request_id = req_id
            self._pending_requests[request_id_key(req_id)] = prompt_future

            def _on_prompt_done(done: asyncio.Future[object]) -> None:
                turn.queue.put_nowait(("done", generation, done))
                if turn.cancel_future is not None and not turn.cancel_future.done():
                    turn.cancel_future.set_result(None)

            prompt_future.add_done_callback(_on_prompt_done)
            frame = encode_jsonrpc_request(
                req_id,
                "session/prompt",
                {"sessionId": self.session_id, "prompt": [{"type": "text", "text": prompt}]},
            )
            await self._write_frame(frame)
        except BaseException:
            if self._active_turn is not None and self._active_turn.generation == self._turn_generation:
                self._active_turn = None
                self._run_activity = None
            if self._turn_lock.locked():
                self._turn_lock.release()
            raise

    async def receive_response(self) -> AsyncIterator[object]:
        """Yield ProgressObservation side-channel items and final AgyTurnOutcome."""

        turn = self._active_turn
        if turn is None:
            raise ConnectionError("agy ACP turn was not started")
        my_generation = turn.generation
        try:
            while True:
                tag, gen, payload = await turn.queue.get()
                if gen != my_generation:
                    continue
                if tag == "eof":
                    assert isinstance(payload, BaseException)
                    raise payload
                if tag == "update":
                    assert isinstance(payload, Mapping)
                    update = payload.get("update")
                    if not isinstance(update, Mapping):
                        continue
                    if update.get("sessionUpdate") == "config_option_update":
                        self._update_config_options(update)
                        continue
                    text = extract_session_update_text(update, "agent_message_chunk")
                    if text:
                        turn.text_chunks.append(text)
                    observation = extract_progress_observation(update)
                    if observation is not None:
                        yield observation
                    continue
                if tag == "done":
                    assert isinstance(payload, asyncio.Future)
                    exc = payload.exception()
                    if exc is not None:
                        if isinstance(exc, AgyRpcError):
                            yield build_rpc_error_outcome(exc)
                            return
                        raise exc
                    outcome = build_turn_outcome(payload.result(), turn.text_chunks)
                    if turn.cancel_sent or outcome.stop_reason == "cancelled":
                        self._session_tainted_after_cancel = True
                    yield outcome
                    return
        finally:
            if turn.cancel_sent:
                self._session_tainted_after_cancel = True
            if turn.cancel_future is not None and not turn.cancel_future.done():
                turn.cancel_future.set_result(None)
            if self._active_turn is turn:
                self._active_turn = None
            self._run_activity = None
            if self._turn_lock.locked():
                self._turn_lock.release()

    async def interrupt(self) -> None:
        """Idempotently cancel the active turn and wait up to cancel_timeout_seconds."""

        turn = self._active_turn
        if turn is None or self.session_id is None or turn.cancel_future is None:
            return
        cancel_future = turn.cancel_future
        if not turn.cancel_sent:
            turn.cancel_sent = True
            frame = encode_jsonrpc_notification("session/cancel", {"sessionId": self.session_id})
            try:
                await self._write_frame(frame)
            except ConnectionError:
                self._fail_active_turn(ConnectionError("agy ACP closed during cancel"))
                return
        try:
            await asyncio.wait_for(asyncio.shield(cancel_future), timeout=self._cancel_timeout_seconds)
        except TimeoutError:
            self.force_stop()
            self._fail_active_turn(ConnectionError("agy ACP turn did not settle within cancel timeout"))

    def _fail_active_turn(self, error: ConnectionError) -> None:
        turn = self._active_turn
        if turn is None:
            return
        if turn.prompt_future is not None and not turn.prompt_future.done():
            turn.prompt_future.set_exception(error)
        if turn.cancel_future is not None and not turn.cancel_future.done():
            turn.cancel_future.set_result(None)

    def read_run_activity(self) -> tuple[bool, int | None, int | None] | None:
        """Thread-safe snapshot of active turn liveness for BaseTurnProcess."""

        current = self._run_activity
        return None if current is None else (current[1], current[2], None)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None or process.returncode is not None else process.pid

    @property
    def exit_error(self) -> str | None:
        process = self._process
        if process is None or process.returncode is None:
            return None
        detail = summarize_stderr(bytes(self._stderr_tail))
        return f"agy ACP process exited with status {process.returncode}" + (f": {detail}" if detail else "")

    def force_stop(self) -> None:
        """Terminate the direct ACP child process cleanly without POSIX kill."""

        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return

    def force_stopped(self) -> bool:
        """Return True once the child is reaped and all reader/exit tasks settled."""

        process = self._process
        if process is None:
            return True
        if process.returncode is None:
            return False
        if any(t is not None and not t.done() for t in (self._stdout_task, self._stderr_task, self._exit_task)):
            return False
        self._process = None
        self._stdout_task = None
        self._stderr_task = None
        self._exit_task = None
        return True

    def _allocate_request_id(self) -> int:
        self._next_request_id += 1
        return self._next_request_id

    async def _request(self, method: str, params: Mapping[str, object], *, timeout: float | None) -> object:
        req_id = self._allocate_request_id()
        key = request_id_key(req_id)
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending_requests[key] = future
        try:
            await self._write_frame(encode_jsonrpc_request(req_id, method, params))
            return await (future if timeout is None else asyncio.wait_for(future, timeout=timeout))
        except TimeoutError as error:
            raise ConnectionError(f"agy ACP request {method!r} timed out") from error
        finally:
            self._pending_requests.pop(key, None)

    async def _write_frame(self, frame: Mapping[str, object]) -> None:
        async with self._write_lock:
            process = self._process
            if process is None or process.stdin is None:
                raise ConnectionError("agy ACP session is not running")
            if process.returncode is not None:
                raise format_exited_error(self._stderr_tail)
            process.stdin.write(json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n")
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise ConnectionError("agy ACP stdin closed") from error

    async def _handle_server_request(self, message: Mapping[str, object]) -> None:
        raw_id = message.get("id")
        if type(raw_id) is not int and not isinstance(raw_id, str):
            return
        req_id: JsonRpcId = raw_id
        method = message.get("method")
        params = message.get("params")
        param_map = params if isinstance(params, Mapping) else {}
        if method == "session/request_permission":
            outcome = select_permission_option(
                param_map,
                allow_all=self._projection.dangerously_skip_permissions,
                allowed_mcp_tools=self._projection.allowed_mcp_tools,
            )
            await self._write_frame(encode_jsonrpc_response(req_id, outcome))
            return
        await self._write_frame(encode_jsonrpc_error(req_id, ACP_JSONRPC_UNKNOWN_REQUEST_CODE, f"Method not found: {method}"))

    async def _read_stdout_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        eof_error: ConnectionError | None = None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    eof_error = format_exited_error(self._stderr_tail)
                    break
                try:
                    decoded = decode_jsonrpc_line(line)
                except ConnectionError as error:
                    eof_error = error
                    self._write_log("worker.acp.invalid_json", detail=str(error))
                    break
                kind = classify_jsonrpc_message(decoded)
                if kind == "response":
                    raw_id = decoded.get("id")
                    if type(raw_id) is int or isinstance(raw_id, str):
                        waiter = self._pending_requests.pop(request_id_key(raw_id), None)
                        if waiter is not None and not waiter.done():
                            err = decoded.get("error")
                            waiter.set_exception(decode_rpc_error(err)) if isinstance(err, Mapping) else waiter.set_result(decoded.get("result"))
                elif kind == "request":
                    try:
                        await self._handle_server_request(decoded)
                    except ConnectionError:
                        break
                elif kind == "notification" and decoded.get("method") == "session/update" and not self._suppressing_replay:
                    params = decoded.get("params")
                    if isinstance(params, Mapping):
                        sid = params.get("sessionId")
                        if self.session_id is not None and sid not in (None, self.session_id):
                            continue
                        turn = self._active_turn
                        if turn is not None and not turn.cancel_sent and not (turn.prompt_future is not None and turn.prompt_future.done()):
                            correlations = []
                            for source in (params, params.get("update") if isinstance(params.get("update"), Mapping) else {}):
                                for key in ("promptId", "turnId", "requestId"):
                                    if key in source and source[key] is not None:
                                        correlations.append(source[key])
                            if any(
                                isinstance(value, bool)
                                or value not in (turn.prompt_request_id, str(turn.prompt_request_id))
                                for value in correlations
                            ):
                                continue
                            self._run_activity = (turn.generation, False, time.time_ns() // 1_000_000)
                            turn.queue.put_nowait(("update", turn.generation, params))
        except Exception as error:
            eof_error = ConnectionError(f"agy ACP stdout reader failed: {error}")
        finally:
            final_error = eof_error or format_exited_error(self._stderr_tail)
            for key in list(self._pending_requests):
                waiter = self._pending_requests.pop(key, None)
                if waiter is not None and not waiter.done():
                    waiter.set_exception(final_error)
            turn = self._active_turn
            if turn is not None:
                turn.queue.put_nowait(("eof", turn.generation, final_error))
                if turn.cancel_future is not None and not turn.cancel_future.done():
                    turn.cancel_future.set_result(None)

    async def _watch_process_exit(self, process: asyncio.subprocess.Process) -> None:
        returncode = await process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        self._write_log(
            "worker.stopped" if self._expected_stop else "worker.exited",
            pid=process.pid,
            returnCode=returncode,
            stderrTail=summarize_stderr(bytes(self._stderr_tail)),
        )

    def _write_log(self, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger.log("error" if event == "worker.exited" else "info", event, **fields)

    async def _close(self) -> None:
        process = self._process
        if process is None:
            return
        self._expected_stop = True
        deadline = time.monotonic() + ACP_CLOSE_TOTAL_SECONDS
        if self.session_id is not None and process.returncode is None:
            try:
                await self._request(
                    "session/close",
                    {"sessionId": self.session_id},
                    timeout=min(0.4, max(0.05, deadline - time.monotonic())),
                )
            except (ConnectionError, AgyRpcError, TimeoutError):
                pass
        await close_acp_subprocess(process, (self._stdout_task, self._stderr_task, self._exit_task), deadline)
        if not self.force_stopped():
            self._process = None
            self._stdout_task = None
            self._stderr_task = None
            self._exit_task = None
