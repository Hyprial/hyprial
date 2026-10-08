"""CodexAppServerClient and the CodexAppServerProcess driver."""
from __future__ import annotations

from hyprial.daemon.impl.harnesses.smolvm_runtime  import SmolvmWorkerRuntime

import asyncio
import json
import os
import sys as sys
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Self

from hyprial import __version__
from hyprial.identity import (
    ChildEnvironmentLaunch,
    whitelist_replacement_environment,
)
from hyprial.identity import (
    AgentRuntimeContext,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.transfer.execution.container import wrap_worker_launch
from hyprial.kernel import Logger

from hyprial.daemon.impl.harnesses.protocol.common  import (
    summarize_stderr,
)
from hyprial.daemon.impl.harnesses.streaming.protocol  import (
    TURN_IDLE_TIMEOUT_ENV,
    ProgressObservation,
    TurnCompletedObserver,
    TurnClientFactory,
    TurnFailureSpecObserver,
    resolve_turn_timeout_seconds,
)
from hyprial.daemon.impl.harnesses.streaming.process  import (
    StreamingTurnProcess,
    )
from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel
from hyprial.daemon.impl.harnesses.model_provider  import codex_provider_configuration
from hyprial.kernel import (
    OwnedProcessGroup,
)
from hyprial.daemon.impl.harnesses.codex.native_env import (
    CodexAgentHomeError,
    CodexNativeLoadEvidence,
    _CodexSpawnEnvironmentMixin,
    _final_reply,
    _server_request_response,
    _thread_config,
    _turn_error,
    _validate_codex_native_load,
    prepare_codex_runtime_context,
)
from hyprial.daemon.impl.harnesses.codex.process import (
    MAX_PENDING_REQUESTS,
    REQUEST_TIMEOUT_SECONDS_DEFAULT,
    PROCESS_FORCE_JOIN_SECONDS,
    PROCESS_STOP_TIMEOUT_SECONDS,
    STREAM_LIMIT_BYTES,
    CodexAppServerRpcError,
    _CodexSession,
    _CodexTurnOutcome,
    _codex_progress_observation,
    _managed_process_group,
    _notification_turn_id,
    resolve_codex_executable,
    _spawn_managed_codex,
    _thread_id,
)
from hyprial.daemon.impl.harnesses.codex.app_server import (
    THREAD_START_TIMEOUT_SECONDS_DEFAULT,
    _thread_execution_params,
)
from hyprial.daemon.impl.harnesses.codex.process import _ClientCloseMixin
from hyprial.daemon.impl.harnesses.codex.carrier import (
    MANAGED_TURN_IDLE_TIMEOUT_SECONDS,
)


class CodexAppServerClient(
    _CodexSpawnEnvironmentMixin,
    _ClientCloseMixin,
):
        def __init__(
            self,
            spec: HarnessLaunchSpec,
            *,
            session_ref: str | None = None,
            session: _CodexSession | None = None,
            command: tuple[str, ...] = ("codex",),
            env: Mapping[str, str] | None = None,
            worker_channel: WorkerChannel | None = None,
            request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS_DEFAULT,
            thread_start_timeout_seconds: float = THREAD_START_TIMEOUT_SECONDS_DEFAULT,
            execution_runtime: "SmolvmWorkerRuntime | None" = None,
            turn_idle_timeout_seconds: float | None = None,
            process_group: OwnedProcessGroup | None = None,
            logger: Logger | None = None,
            complete_launch: ChildEnvironmentLaunch | None = None,
            runtime_launch_custody: (
                Callable[[AgentRuntimeContext], AbstractContextManager[None]] | None
            ) = None,
        ) -> None:
            self.spec = spec
            self._complete_launch = complete_launch
            self._execution_runtime = execution_runtime
            if spec.execution_runtime is not None and execution_runtime is None:
                raise ValueError("smolvm requires an owned runtime; no host fallback")
            if execution_runtime is not None:
                from hyprial.daemon.impl.harnesses.smolvm_runtime  import guest_channel
                assert worker_channel is not None and spec.execution_runtime is not None
                worker_channel = guest_channel(worker_channel, spec.execution_runtime)
            self._runtime_launch_custody = runtime_launch_custody
            if complete_launch is not None and env is not None:
                raise ValueError(
                    "complete child environment cannot be combined with a "
                    "partial env mapping"
                )
            if complete_launch is not None:
                base_environment = complete_launch.environment.for_exec()
            else:
                base_environment = whitelist_replacement_environment(
                    os.environ, env or {}
                )
            runtime_context = (
                complete_launch.runtime_context
                if complete_launch is not None
                else None
            )
            self._runtime_context = runtime_context
            channel_context = (
                worker_channel.runtime_context if worker_channel is not None else None
            )
            if worker_channel is not None and channel_context is not runtime_context:
                raise CodexAgentHomeError(
                    "Codex worker channel and child launch disagree on runtime context"
                )
            if runtime_context is not None:
                if complete_launch is None or runtime_context.actor != complete_launch.actor:
                    raise CodexAgentHomeError(
                        "Codex runtime context actor does not match the child launch"
                    )
                self._native_root = runtime_context.roots.native_root
                self._session_root = runtime_context.roots.session_root
                self._shared_credential = runtime_context.shared_credential
                if base_environment.get("CODEX_HOME") != str(self._native_root):
                    raise CodexAgentHomeError(
                        "complete child environment disagrees with Codex runtime context"
                    )
                # Lazily, like the home preparer: only an arg0 alias needs it,
                # and an unresolvable command must not pre-empt the provider
                # grant check below with an unmapped error.
                codex_executable = (
                    (lambda: resolve_codex_executable(base_environment, command[0]))
                    if execution_runtime is None
                    else Path(command[0])
                )
                prepare_codex_runtime_context(
                    runtime_context, codex_executable=codex_executable
                )
                launch_command = command
            else:
                if (
                    complete_launch is not None
                    and complete_launch.agent_home_profile_applied
                    and "CODEX_HOME" in base_environment
                ):
                    raise CodexAgentHomeError(
                        "P2 CODEX_HOME requires an AgentRuntimeContext"
                    )
                self._shared_credential = None
                inherited_root = base_environment.get("CODEX_HOME")
                self._native_root = (
                    Path(inherited_root) if inherited_root is not None else None
                )
                self._session_root = (
                    self._native_root / "sessions"
                    if self._native_root is not None
                    else None
                )
                launch_command = command
            self._p2_runtime = runtime_context is not None
            provider_args, provider_environment = codex_provider_configuration(
                spec,
                base_environment,
                allow_legacy_home_fallback=not self._p2_runtime,
            )
            self.command = (*launch_command, *provider_args, "app-server", "--stdio")
            self._session = session or _CodexSession(session_ref)
            self._worker_channel = worker_channel
            # Same daemon-bound identity the pi and claude carriers inject
            # (WorkerChannel.identity_environment): a codex worker's shell-outs to
            # ``hyprial`` must present the session binding to the fenced PAC write
            # methods, and the exec runtime's own subprocesses inherit the same
            # env -- the three harnesses stay isomorphic on this surface.
            identity_environment = (
                worker_channel.identity_environment() if worker_channel is not None else {}
            )
            if spec.containerized:
                if worker_channel is None:
                    raise ValueError(
                        "containerized codex workers require a worker channel"
                    )
                self.command = wrap_worker_launch(
                    spec,
                    inner_argv=self.command,
                    env_delta={**(env or {}), **provider_environment, **identity_environment},
                    state_dir=worker_channel.state_dir,
                )
                # Bare Docker ``-e KEY`` flags copy from this child environment;
                # values never enter argv or Docker error text.
                combined = {**(env or {}), **provider_environment, **identity_environment}
                self._env = combined or None
            else:
                combined = {
                    **(env or {}),
                    **provider_environment,
                    **identity_environment,
                }
                self._env = combined or None
            self._request_timeout_seconds = request_timeout_seconds
            self._thread_start_timeout_seconds = thread_start_timeout_seconds
            # spec.turn_timeout_seconds is deliberately NOT read: the wall-clock
            # cap is retired (#277) and the persisted field is tolerated only so
            # existing desired-state files keep loading.
            self._turn_idle_timeout_seconds = resolve_turn_timeout_seconds(
                turn_idle_timeout_seconds
                if turn_idle_timeout_seconds is not None
                else spec.idle_timeout_seconds,
                default=MANAGED_TURN_IDLE_TIMEOUT_SECONDS,
                env_var=TURN_IDLE_TIMEOUT_ENV,
            )
            self._process: asyncio.subprocess.Process | None = None
            self._process_group = process_group or _managed_process_group()
            self._process_group_id: int | None = None
            self._logger = logger
            self._reader_task: asyncio.Task[None] | None = None
            self._stderr_task: asyncio.Task[None] | None = None
            self._exit_task: asyncio.Task[None] | None = None
            self._stderr_tail = bytearray()
            self._expected_stop = False
            self._exit_logged = False
            self._write_lock = asyncio.Lock()
            self._pending: dict[int, asyncio.Future[object]] = {}
            self._notifications: asyncio.Queue[dict[str, object] | BaseException] = (
                asyncio.Queue()
            )
            self._next_request_id = 1
            self._active_turn_id: str | None = None
            self._native_load_evidence: CodexNativeLoadEvidence | None = None

        @property
        def session_ref(self) -> str | None:
            return self._session.thread_id

        @property
        def server_request_methods(self) -> tuple[str, ...]:
            return tuple(self._session.server_request_methods or ())

        @property
        def active_turn_id(self) -> str | None:
            return self._active_turn_id

        @property
        def native_load_evidence(self) -> CodexNativeLoadEvidence | None:
            return self._native_load_evidence

        @property
        def running(self) -> bool:
            process = self._process
            return process is not None and process.returncode is None

        @property
        def pid(self) -> int | None:
            process = self._process
            if process is None or process.returncode is not None:
                return None
            return process.pid

        @property
        def exit_error(self) -> str | None:
            process = self._process
            if process is None or process.returncode is None:
                return None
            detail = summarize_stderr(bytes(self._stderr_tail))
            return f"Codex app-server exited with status {process.returncode}" + (
                f": {detail}" if detail else ""
            )

        async def __aenter__(self) -> Self:
            try:
                return await self._enter_process()
            except BaseException:
                await self._close_process()
                raise

        async def _enter_process(self) -> Self:
            environment = self._spawn_environment()
            command = self.command
            if self._execution_runtime is not None:
                command, environment = self._execution_runtime.start(command, environment or {})
            custody = (
                nullcontext()
                if self._runtime_context is None or self._runtime_launch_custody is None
                else self._runtime_launch_custody(self._runtime_context)
            )
            with custody:
                self._process = await _spawn_managed_codex(
                    command,
                    self._process_group,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.spec.cwd,
                    env=environment,
                    limit=STREAM_LIMIT_BYTES,
                )
            if self._complete_launch is not None and self._logger is not None:
                self._logger.info(
                    "worker.environment.receipt",
                    actor=self._complete_launch.actor,
                    grants=[
                        {"grantId": grant_id, "revision": revision}
                        for grant_id, revision in self._complete_launch.grants
                    ],
                )
            self._expected_stop = False
            self._exit_logged = False
            if self._logger is not None:
                self._logger.info("worker.started", pid=self._process.pid)
            try:
                self._process_group.register(self._process.pid)
            except ConnectionError:
                # register() already drained the rejected generation's PGID.
                await self._finish_rejected_process(self._process)
                await self._finish_rejected_stderr(self._process)
                self._write_exit_log(self._process, self._process.returncode)
                self._process = None
                raise
            except BaseException:
                # Registration can fail after start_new_session() has spawned
                # descendants.  Drain the new PGID first; leader-only cleanup
                # would orphan those descendants.
                self._process_group._close_unregistered_group(self._process.pid)
                await self._finish_rejected_process(self._process)
                await self._finish_rejected_stderr(self._process)
                self._write_exit_log(self._process, self._process.returncode)
                self._process = None
                raise
            self._process_group_id = self._process.pid
            assert self._process.stderr is not None
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            self._exit_task = asyncio.create_task(self._watch_process_exit())
            self._reader_task = asyncio.create_task(self._read_loop())
            try:
                initialize = await self.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "harness_bridge",
                            "title": "Harness Bridge",
                            "version": __version__,
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                await self.notify("initialized", {})
                if self._p2_runtime:
                    assert self._native_root is not None
                    working_directory = Path(self._working_directory())
                    config_read = await self.request(
                        "config/read",
                        {"cwd": str(working_directory), "includeLayers": True},
                    )
                    account_read = await self.request("account/read", {})
                    skills_list = await self.request(
                        "skills/list",
                        {"cwds": [str(working_directory)], "forceReload": True},
                    )
                    self._native_load_evidence = _validate_codex_native_load(
                        initialize=initialize,
                        config_read=config_read,
                        account_read=account_read,
                        skills_list=skills_list,
                        native_root=self._native_root,
                        cwd=working_directory,
                        model_provider=self.spec.model_provider,
                        shared_credential=self._shared_credential,
                    )
                    if self._logger is not None:
                        evidence = self._native_load_evidence
                        self._logger.info(
                            "worker.config.loaded",
                            codexHome=evidence.codex_home,
                            layerTypes=list(evidence.layer_types),
                            effectiveModel=evidence.effective_model,
                            authStore=evidence.auth_store,
                            accountType=evidence.account_type,
                            requiresOpenaiAuth=evidence.requires_openai_auth,
                            userSkillCount=len(evidence.user_skills),
                            projectSkillCount=len(evidence.project_skills),
                        )
                if self._session.thread_id is None:
                    await self._start_thread()
                elif not await self._resume_thread():
                    if self._p2_runtime:
                        raise CodexAgentHomeError(
                            "Codex resume target was not found in the resolved "
                            "native root; refusing to cold-start under the old "
                            "session reference"
                        )
                    # The persisted thread is definitively gone (no rollout and
                    # not loaded).  Resume must never become a startup failure
                    # source for legacy workers: they retain the historical cold
                    # start.  P2 workers above fail loudly because their explicit
                    # root makes a missing target an authorization/locator result,
                    # not permission to create a replacement conversation.
                    if self._logger is not None:
                        self._logger.info(
                            "worker.session_ref.lost",
                            threadId=self._session.thread_id,
                        )
                    self._session.thread_id = None
                    await self._start_thread()
                if self._logger is not None:
                    self._logger.info("worker.ready", pid=self._process.pid)
                return self
            except BaseException:
                await self._close_process()
                raise

        async def _start_thread(self) -> None:
            execution = _thread_execution_params(self.spec.args)
            if self.spec.model is not None:
                execution["model"] = self.spec.model
            params: dict[str, object] = {
                "cwd": self._working_directory(),
                **execution,
            }
            config = _thread_config(
                self._working_directory(),
                execution,
                self._worker_channel,
                managed_environment=self._p2_runtime,
            )
            if config:
                params["config"] = config
            result = await self.request(
                "thread/start", params, timeout=self._thread_start_timeout_seconds
            )
            self._session.thread_id = _thread_id(result)

        async def _resume_thread(self) -> bool:
            """Resume the stored thread; False only when it is definitively gone.
    
            A transient resume failure still raises -- the reconnect loop retries
            it.  Only "no rollout found" combined with absence from the loaded
            list means the thread no longer exists, in which case the caller
            falls back to a cold start instead of failing the launch.
            """
    
            thread_id = self._require_thread_id()
            execution = _thread_execution_params(self.spec.args)
            if self.spec.model is not None:
                execution["model"] = self.spec.model
            params: dict[str, object] = {
                "threadId": thread_id,
                **execution,
            }
            # A reconnect rebuilds the thread config; without the override the
            # resumed thread would lose the harness tools AND the git writable
            # roots, silently reintroducing the very commit failure this change
            # exists to fix (the same door the pre-approval comment warns about).
            config = _thread_config(
                self._working_directory(),
                execution,
                self._worker_channel,
                managed_environment=self._p2_runtime,
            )
            if config:
                params["config"] = config
            try:
                result = await self.request(
                    "thread/resume", params, timeout=self._thread_start_timeout_seconds
                )
            except CodexAppServerRpcError as error:
                if "no rollout found" not in str(error).lower():
                    raise
                loaded = await self.request("thread/loaded/list", {})
                identifiers = loaded.get("data") if isinstance(loaded, dict) else None
                if not isinstance(identifiers, list) or thread_id not in identifiers:
                    return False
                result = {"thread": {"id": thread_id}}
            resumed = _thread_id(result)
            if resumed != thread_id:
                raise ConnectionError(
                    "Codex app-server resumed an unexpected thread "
                    f"{resumed!r} instead of {thread_id!r}"
                )
            return True

        async def __aexit__(self, *args: object) -> bool:
            self._expected_stop = True
            await self._close_process()
            return False

        async def query(self, prompt: str) -> None:
            thread_id = self._require_thread_id()
            if self._active_turn_id is not None:
                raise ConnectionError("Codex app-server already has an active turn")
            result = await self.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
            if not isinstance(result, dict):
                raise ConnectionError("Codex app-server returned an invalid turn/start result")
            turn = result.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise ConnectionError("Codex app-server did not return a turn ID")
            self._active_turn_id = turn_id

        async def steer_turn(self, prompt: str) -> str:
            thread_id = self._require_thread_id()
            turn_id = self._active_turn_id
            if turn_id is None:
                raise ConnectionError("Codex app-server has no active turn to steer")
            result = await self.request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
            steered = result.get("turnId") if isinstance(result, dict) else None
            if not isinstance(steered, str) or not steered:
                raise ConnectionError("Codex app-server did not return a steered turn ID")
            self._active_turn_id = steered
            return steered

        async def receive_response(self) -> AsyncIterator[object]:
            turn_id = self._active_turn_id
            if turn_id is None:
                raise ConnectionError("Codex app-server turn was not started")
            try:
                completed: dict[str, object] | None = None
                async for item in self._turn_events(turn_id):
                    if isinstance(item, ProgressObservation):
                        yield item
                        continue
                    completed = item
                    break
                if completed is None:
                    raise ConnectionError(
                        f"Codex app-server ended turn {turn_id!r} without a completion"
                    )
                status = completed.get("status")
                if status != "completed":
                    yield _CodexTurnOutcome(
                        _turn_error(completed, turn_id), is_error=True
                    )
                    return
                turn = await self._read_turn(turn_id)
                if turn.get("status") != "completed" or turn.get("error") is not None:
                    yield _CodexTurnOutcome(_turn_error(turn, turn_id), is_error=True)
                    return
                reply = _final_reply(turn)
                if reply is None:
                    yield _CodexTurnOutcome(
                        "Codex app-server returned no final agent message",
                        is_error=True,
                    )
                    return
                yield _CodexTurnOutcome(reply)
            finally:
                self._active_turn_id = None

        async def interrupt(self) -> None:
            turn_id = self._active_turn_id
            if turn_id is None:
                return
            await self.request(
                "turn/interrupt",
                {"threadId": self._require_thread_id(), "turnId": turn_id},
            )

        async def request(
            self, method: str, params: object = None, *, timeout: float | None = None
        ) -> object:
            if len(self._pending) >= MAX_PENDING_REQUESTS:
                raise ConnectionError("too many Codex app-server requests are pending")
            self._require_running_process()
            request_id = self._next_request_id
            self._next_request_id += 1
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            try:
                await self._write(
                    {"method": method, "id": request_id, "params": params or {}}
                )
                return await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=(
                        self._request_timeout_seconds if timeout is None else timeout
                    ),
                )
            except TimeoutError as error:
                future.cancel()
                raise ConnectionError(
                    f"timed out waiting for Codex app-server RPC {method}"
                ) from error
            finally:
                self._pending.pop(request_id, None)

        async def notify(self, method: str, params: object = None) -> None:
            await self._write({"method": method, "params": params or {}})

        async def _turn_events(self, turn_id: str) -> AsyncIterator[object]:
            """Yield correlated coarse progress, then the completed turn object."""
    
            # Producer-local quiet-period watch (#277: no timeout ever kills a
            # turn; liveness will be the connector's job via steer probing).
            # Activity is ONLY a new app-server notification correlated with
            # this turn, observed here in the client -- before the route-C
            # progress channel's drop-oldest queue and coalescing, whose silence
            # therefore never means anything.  Crossing the threshold reports
            # ``turn-stalled``; a report re-fires every further threshold of
            # continued silence so the signal survives log tails and cannot be
            # missed, and the first new correlated notification reports
            # ``turn-resumed`` and clears the condition.
            threshold = self._turn_idle_timeout_seconds
            last_activity = asyncio.get_running_loop().time()
            next_stall_report = last_activity + threshold
            stalled = False
            while True:
                now = asyncio.get_running_loop().time()
                if now >= next_stall_report:
                    stalled = True
                    next_stall_report = now + threshold
                    quiet = now - last_activity
                    yield ProgressObservation(
                        phase="turn-stalled",
                        summary=(
                            f"Codex turn quiet for {quiet:.0f}s (no correlated "
                            f"app-server activity; sensitivity {threshold:g}s)"
                        ),
                        detail={
                            "quietSeconds": round(quiet, 3),
                            "thresholdSeconds": threshold,
                            "detector": "producer-quiet-watch",
                        },
                    )
                try:
                    message = await asyncio.wait_for(
                        self._notifications.get(), timeout=0.5
                    )
                except TimeoutError:
                    # This poll recovers a dropped terminal notification.  Its
                    # "inProgress" answer is level state that can be stuck
                    # forever, so it never counts as activity.
                    try:
                        polled = await self._read_turn(turn_id)
                    except ConnectionError as error:
                        if "was not found" not in str(error):
                            raise
                    else:
                        if polled.get("status") not in {None, "inProgress"}:
                            yield ProgressObservation(
                                phase="turn-end",
                                summary=(
                                    "Codex turn completed"
                                    if polled.get("status") == "completed"
                                    else f"Codex turn ended ({polled.get('status')})"
                                ),
                                terminal=True,
                            )
                            yield polled
                            return
                    continue
                if isinstance(message, BaseException):
                    raise message
                params = message.get("params")
                if (
                    isinstance(params, dict)
                    and _notification_turn_id(params) == turn_id
                ):
                    if stalled:
                        stalled = False
                        quiet = asyncio.get_running_loop().time() - last_activity
                        yield ProgressObservation(
                            phase="turn-resumed",
                            summary=(
                                f"Codex turn active again after {quiet:.0f}s quiet"
                            ),
                            detail={
                                "quietSeconds": round(quiet, 3),
                                "thresholdSeconds": threshold,
                                "detector": "producer-quiet-watch",
                            },
                        )
                    last_activity = asyncio.get_running_loop().time()
                    next_stall_report = last_activity + threshold
                if message.get("method") != "turn/completed":
                    observation = _codex_progress_observation(
                        message,
                        thread_id=self._require_thread_id(),
                        turn_id=turn_id,
                    )
                    if observation is not None:
                        yield observation
                    continue
                turn = params.get("turn") if isinstance(params, dict) else None
                if isinstance(turn, dict) and turn.get("id") == turn_id:
                    observation = _codex_progress_observation(
                        message,
                        thread_id=self._require_thread_id(),
                        turn_id=turn_id,
                    )
                    if observation is not None:
                        yield observation
                    yield turn
                    return

        async def _read_turn(self, turn_id: str) -> dict[str, object]:
            thread_id = self._require_thread_id()
            result = await self.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            turns = thread.get("turns") if isinstance(thread, dict) else None
            if isinstance(turns, list):
                for candidate in turns:
                    if isinstance(candidate, dict) and candidate.get("id") == turn_id:
                        return candidate
            raise ConnectionError(
                f"completed Codex turn {turn_id!r} was not found in thread {thread_id!r}"
            )

        async def _write(self, message: dict[str, object]) -> None:
            process = self._require_running_process()
            assert process.stdin is not None
            payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
            async with self._write_lock:
                process.stdin.write(payload)
                try:
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError) as error:
                    raise ConnectionError("Codex app-server input closed") from error

        async def _read_loop(self) -> None:
            process = self._process
            assert process is not None and process.stdout is not None
            failure: BaseException
            try:
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError as error:
                        raise ConnectionError(
                            "Codex app-server emitted an oversized line"
                        ) from error
                    if not line:
                        detail = self._stderr_tail.decode("utf-8", errors="replace")[-4096:]
                        raise ConnectionError(
                            "Codex app-server exited unexpectedly"
                            + (f": {detail}" if detail else "")
                        )
                    try:
                        message = json.loads(line.rstrip(b"\r\n"))
                    except json.JSONDecodeError as error:
                        raise ConnectionError(
                            "Codex app-server emitted invalid JSON"
                        ) from error
                    if not isinstance(message, dict):
                        raise ConnectionError("Codex app-server message must be an object")
                    method = message.get("method")
                    identifier = message.get("id")
                    if isinstance(method, str) and identifier is not None:
                        if self._session.server_request_methods is None:
                            self._session.server_request_methods = []
                        self._session.server_request_methods.append(method)
                        answer = _server_request_response(
                            identifier, method, message.get("params")
                        )
                        failure = answer.get("error")
                        if (
                            self._logger is not None
                            and isinstance(failure, dict)
                            and failure.get("code") == -32601
                        ):
                            # An UNKNOWN server request is answered fail-closed
                            # only by a transport error; name it in the worker
                            # log so a newly introduced request that could leave
                            # the turn waiting is observable instead of silent.
                            self._logger.error(
                                "codex.server_request.unsupported", method=method
                            )
                        await self._write(answer)
                        continue
                    if isinstance(identifier, int):
                        future = self._pending.get(identifier)
                        if future is None or future.done():
                            continue
                        rpc_error = message.get("error")
                        if isinstance(rpc_error, dict):
                            error_message = rpc_error.get("message")
                            code = rpc_error.get("code")
                            future.set_exception(
                                CodexAppServerRpcError(
                                    str(error_message or "Codex app-server RPC failed"),
                                    code=code if isinstance(code, int) else None,
                                    data=rpc_error.get("data"),
                                )
                            )
                        else:
                            future.set_result(message.get("result"))
                        continue
                    if isinstance(method, str):
                        await self._notifications.put(message)
            except asyncio.CancelledError:
                failure = ConnectionError("Codex app-server reader stopped")
            except BaseException as error:  # noqa: BLE001 - subprocess boundary
                failure = error
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(failure)
            await self._notifications.put(failure)

        def _working_directory(self) -> str:
            return str(Path(self.spec.cwd or os.getcwd()).resolve())

        def _require_running_process(self) -> asyncio.subprocess.Process:
            process = self._process
            if process is None or process.stdin is None or process.returncode is not None:
                raise ConnectionError("Codex app-server is not running")
            return process

        def _require_thread_id(self) -> str:
            thread_id = self._session.thread_id
            if thread_id is None:
                raise ConnectionError("Codex app-server thread is not initialized")
            return thread_id

class CodexAppServerProcess(StreamingTurnProcess):
    """Serialize daemon deliveries through one persistent Codex thread."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        client_factory: TurnClientFactory | None = None,
        command: tuple[str, ...] = ("codex",),
        env: Mapping[str, str] | None = None,
        worker_channel: WorkerChannel | None = None,
        daemon_epoch: str | None = None,
        reconnect_delay_seconds: float = 0.25,
        reconnect_delay_max_seconds: float = 30.0,
        max_delivery_attempts: int = 5,
        complete_launch: ChildEnvironmentLaunch | None = None,
        on_turn_failure_for_spec: TurnFailureSpecObserver | None = None,
        on_turn_completed: TurnCompletedObserver | None = None,
        runtime_launch_custody: (
            Callable[[AgentRuntimeContext], AbstractContextManager[None]] | None
        ) = None,
    ) -> None:
        if spec.harness != "codex" or not spec.headless:
            raise ValueError("Codex app-server requires a managed headless spec")
        if spec.endpoint is not None:
            raise ValueError("Codex app-server managed stdio does not accept an endpoint")
        self.spec = spec
        self._session = _CodexSession(spec.session_ref)
        self._process_group = _managed_process_group()
        self.worker_channel = worker_channel
        self._complete_launch = complete_launch
        self._runtime_launch_custody = runtime_launch_custody
        if complete_launch is not None and env is not None:
            raise ValueError(
                "complete child environment cannot be combined with a "
                "partial env mapping"
            )
        logger = (
            Logger.worker(worker_channel.state_dir, runtime="codex", name=spec.name)
            if worker_channel is not None
            else None
        )
        self._execution_runtime = None

        def make_client():
            runtime = None
            if spec.execution_runtime is not None:
                if worker_channel is None or complete_launch is None or not daemon_epoch:
                    raise ValueError("smolvm requires daemon epoch, P2 channel and complete environment")
                runtime = SmolvmWorkerRuntime(spec.execution_runtime, worker_channel, daemon_epoch, spec.cwd)
                self._execution_runtime = runtime
            return CodexAppServerClient(
                spec, session=self._session, command=command, env=env,
                worker_channel=worker_channel, process_group=self._process_group,
                logger=logger, complete_launch=self._complete_launch,
                execution_runtime=runtime,
                runtime_launch_custody=self._runtime_launch_custody,
            )

        def force_stop():
            self._process_group.force_close()
            if self._execution_runtime is not None:
                self._execution_runtime.close()

        super().__init__(
            harness="codex",
            label="Codex app-server",
            client_factory=client_factory or make_client,
            thread_name=f"hyprial-codex-app-server-{spec.name}",
            logger=logger,
            reconnect_delay_seconds=reconnect_delay_seconds,
            reconnect_delay_max_seconds=reconnect_delay_max_seconds,
            max_delivery_attempts=max_delivery_attempts,
            stop_timeout_seconds=PROCESS_STOP_TIMEOUT_SECONDS,
            force_stop=force_stop,
            force_stopped=lambda: self._process_group.stopped() and (
                self._execution_runtime is None or self._execution_runtime.cleanup_complete
            ),
            force_stop_join_seconds=PROCESS_FORCE_JOIN_SECONDS,
            liveness_probe=self._process_group.liveness,
            on_turn_failure=(
                (
                    lambda failure: on_turn_failure_for_spec(
                        failure,
                        harness="codex",
                        provider=spec.model_provider,
                        model=spec.model,
                        worker=spec.name,
                        runtime_context=(
                            None
                            if worker_channel is None
                            else worker_channel.runtime_context
                        ),
                    )
                )
                if on_turn_failure_for_spec is not None
                else None
            ),
            on_turn_completed=on_turn_completed,
        )

    @property
    def session_ref(self) -> str | None:
        return self._session.thread_id

    @property
    def server_request_methods(self) -> tuple[str, ...]:
        return tuple(self._session.server_request_methods or ())
