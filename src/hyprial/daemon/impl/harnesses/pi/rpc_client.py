"""PiRpcClient: the JSONL RPC client over managed pi pipes."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Self, cast
from uuid import uuid4

from hyprial.kernel import ipc_errors
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.transfer.execution.container import CONTAINER_PI_EXTENSION, wrap_worker_launch
from hyprial.kernel import Logger

from hyprial.daemon.impl.harnesses.protocol.common  import summarize_stderr
from hyprial.daemon.impl.harnesses.pi.session  import pi_session_id as _pi_session_id
from hyprial.daemon.impl.harnesses.streaming.protocol  import (
    ProgressObservation,
)
from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel
from hyprial.identity import ChildEnvironmentLaunch
from hyprial.identity import AgentRuntimeContext
from hyprial.daemon.impl.harnesses.model_provider  import pi_model_args
from hyprial.daemon.impl.harnesses.pi.loader  import (
    find_pi_package_root,
    pi_sdk_launch_from_runtime_context,
    resolve_approved_pi_project,
)
from hyprial.daemon.impl.harnesses.pi.messages import (
    _PiTurnOutcome,
    _assistant_context_exhaustion,
    _assistant_failure_detail,
    _assistant_provider_error,
    _bounded,
    _final_assistant_text,
    _pi_progress_observation,
)


# Pi has no MCP support by design (its README defers MCP to extensions), so a
# managed pi worker cannot receive the harness-bridge MCP server a Claude
# worker gets.  The pi carrier for the same WorkerChannel identity is this
# bundled extension: it registers the harness_* toolset and signs daemon IPC
# with the worker's own canonical actor, read from the injected environment.
PI_HARNESS_BRIDGE_EXTENSION = Path(__file__).parent / "resources" / "pi_harness_bridge.ts"


class PiRpcClient:
        def __init__(
            self,
            spec: HarnessLaunchSpec,
            session_ref: str,
            *,
            command: tuple[str, ...] = ("pi",),
            env: Mapping[str, str] | None = None,
            worker_channel: WorkerChannel | None = None,
            log_path: Path | None = None,
            startup_timeout_seconds: float = 30.0,
            settle_grace_seconds: float = 1.5,
            complete_launch: "ChildEnvironmentLaunch | None" = None,
            runtime_launch_custody: (
                Callable[[AgentRuntimeContext], AbstractContextManager[None]] | None
            ) = None,
        ) -> None:
            self.spec = spec
            self.session_ref = session_ref
            # P1b B1: a complete child environment REPLACES the ambient one
            # (design §3.1).  It is kept verbatim; the spawn seam never merges
            # ``os.environ`` into it, and it is never blended with ``env``.
            self._complete_launch = complete_launch
            runtime_context = (
                None if worker_channel is None else worker_channel.runtime_context
            )
            self._runtime_context = runtime_context
            self._runtime_launch_custody = runtime_launch_custody
            complete_environment = (
                None
                if self._complete_launch is None
                else self._complete_launch.environment.for_exec()
            )
            if runtime_context is not None:
                if self._complete_launch is None or complete_environment is None:
                    raise ValueError("Pi P2 SDK bridge requires a complete environment")
                if self._complete_launch.runtime_context is not runtime_context:
                    raise ValueError("Pi P2 channel and environment contexts differ")
                if spec.containerized:
                    raise ValueError("Pi P2 SDK bridge is not supported in containers")
                trust = resolve_approved_pi_project(
                    cwd=spec.cwd,
                    runtime_args=spec.args,
                )
                sdk_launch = pi_sdk_launch_from_runtime_context(
                    runtime_context,
                    trust=trust,
                    mode="rpc",
                    session_id=_pi_session_id(session_ref),
                    pi_package_root=find_pi_package_root(command, complete_environment),
                    model_provider=spec.model_provider,
                    model=spec.model,
                    additional_extension_paths=(PI_HARNESS_BRIDGE_EXTENSION,),
                )
                self.command = sdk_launch.argv
            else:
                launch = [
                    *command,
                    *pi_model_args(spec),
                    *without_pi_reserved_arguments(spec.args),
                ]
                if worker_channel is not None:
                    # Inject the worker's own harness identity: the extension carries
                    # the harness_* toolset, its env pins the worker's canonical actor
                    # and THIS daemon's socket (never an ambient production daemon).
                    # A containerized worker reads the extension INSIDE the image.
                    extension = (
                        CONTAINER_PI_EXTENSION
                        if spec.containerized
                        else str(PI_HARNESS_BRIDGE_EXTENSION)
                    )
                    launch += ["--extension", extension]
                self.command = (
                    *launch,
                    "--mode",
                    "rpc",
                    "--session-id",
                    _pi_session_id(session_ref),
                )
            channel_env = (
                worker_channel.pi_environment() if worker_channel is not None else {}
            )
            if self._complete_launch is not None:
                # Complete-replacement mode (B1): the launch already carries the
                # channel's generated identity values inside its GENERATED set,
                # so no second merge happens here either.  Legacy ``env`` inputs
                # are refused rather than blended — a caller that hands a
                # complete environment AND a partial one has misunderstood the
                # contract, and the failure must be loud, not a precedence rule.
                if env is not None:
                    raise ValueError(
                        "complete child environment cannot be combined with a "
                        "partial env mapping"
                    )
                assert complete_environment is not None
                complete = complete_environment
                if spec.containerized:
                    if worker_channel is None:
                        raise ValueError(
                            "containerized pi workers require a worker channel"
                        )
                    self.command = wrap_worker_launch(
                        spec,
                        inner_argv=self.command,
                        env_delta=complete,
                        state_dir=worker_channel.state_dir,
                    )
                # Bare Docker ``-e KEY`` flags copy from this child environment.
                self._env = complete
            else:
                self._apply_legacy_environment(env, channel_env, worker_channel)
            self._startup_timeout_seconds = startup_timeout_seconds
            self._settle_grace_seconds = settle_grace_seconds
            state_dir = worker_channel.state_dir if worker_channel is not None else None
            if log_path is not None:
                state_dir = Path(log_path).parents[2]
            self._logger = (
                Logger.worker(state_dir, runtime="pi", name=spec.name)
                if state_dir is not None
                else None
            )
            if (
                log_path is not None
                and self._logger is not None
                and self._logger.path.resolve() != Path(log_path).resolve()
            ):
                raise ValueError("pi log_path must match the shared worker route")
            self._process: asyncio.subprocess.Process | None = None
            self._process_protocol: _PiSubprocessProtocol | None = None
            self._spawned = threading.Event()
            self._stdout_task: asyncio.Task[None] | None = None
            self._stderr_task: asyncio.Task[None] | None = None
            self._exit_task: asyncio.Task[None] | None = None
            self._stderr_tail = bytearray()
            self._turn_id: str | None = None
            self._turn_prompt: str | None = None
            self._expected_stop = False
            # Set only when the startup probe answered: the difference between
            # "never came up" and "came up, then died" decides whether a stored
            # session ref is retried or abandoned for a cold start.
            self.reached_ready = False

        def _apply_legacy_environment(
            self,
            env: Mapping[str, str] | None,
            channel_env: dict[str, str],
            worker_channel: "WorkerChannel | None",
        ) -> None:
            """Pre-B1 behaviour for callers without a complete environment."""
    
            combined_environment = {**(env or {}), **channel_env}
            if self.spec.containerized:
                if worker_channel is None:
                    raise ValueError(
                        "containerized pi workers require a worker channel"
                    )
                self.command = wrap_worker_launch(
                    self.spec,
                    inner_argv=self.command,
                    env_delta=combined_environment,
                    state_dir=worker_channel.state_dir,
                )
                self._env = combined_environment or None
            else:
                self._env = (
                    combined_environment
                    if (env is not None or channel_env)
                    else None
                )

        def _spawn_environment(self) -> dict[str, str] | None:
            """The exact env mapping the exec consumer receives.
    
            Complete-replacement mode (B1) returns the frozen mapping verbatim —
            the one place the ambient merge is refused for this carrier.  Legacy
            callers without a complete environment keep today's behaviour until
            their sites are collected in B2.
            """
    
            if self._complete_launch is not None:
                return self._complete_launch.environment.for_exec()
            return None if self._env is None else {**os.environ, **self._env}

        async def __aenter__(self) -> Self:
            environment = self._spawn_environment()
            loop = asyncio.get_running_loop()
            custody = (
                nullcontext()
                if self._runtime_context is None or self._runtime_launch_custody is None
                else self._runtime_launch_custody(self._runtime_context)
            )
            with custody:
                transport, protocol = await loop.subprocess_exec(
                    lambda: _PiSubprocessProtocol(STREAM_LIMIT_BYTES, loop),
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.spec.cwd,
                    env=environment,
                )
            assert isinstance(protocol, _PiSubprocessProtocol)
            self._process_protocol = protocol
            self._process = asyncio.subprocess.Process(transport, protocol, loop)
            self._spawned.set()
            if self._complete_launch is not None:
                # Completion receipt (design §3.1.5): only NOW — the process
                # exists with the frozen mapping — is the launch complete.  The
                # receipt names the bound identity and every grant revision;
                # values never appear.
                self._write_log(
                    "worker.environment.receipt",
                    actor=self._complete_launch.actor,
                    grants=[
                        {"grantId": grant_id, "revision": revision}
                        for grant_id, revision in self._complete_launch.grants
                    ],
                )
            try:
                self._expected_stop = False
                self._write_log("worker.started", pid=self._process.pid)
                assert self._process.stderr is not None
                self._stderr_task = asyncio.create_task(
                    self._drain_stderr(self._process)
                )
                self._exit_task = asyncio.create_task(self._watch_process_exit())
                # Pi prints no ready marker in RPC mode; a get_state round trip
                # proves both that the process is up and that it speaks the protocol.
                probe = f"hyprial-ready-{uuid4().hex}"
                await self._write({"type": "get_state", "id": probe})
                response = await self._read_response(
                    probe, timeout=self._startup_timeout_seconds
                )
                if response.get("success") is not True:
                    raise ConnectionError(
                        f"pi RPC session failed to start: {response.get('error')!r}"
                    )
                self.reached_ready = True
                self._write_log("worker.ready", pid=self._process.pid)
                return self
            except BaseException:
                # ``async with`` does not invoke __aexit__ when __aenter__ fails.
                # The ready probe is therefore an explicit resource boundary.
                await self._close()
                raise

        async def __aexit__(self, *args: object) -> bool:
            del args
            await self._close()
            return False

        def _close_budget(self, deadline: float) -> float:
            """This step's ceiling: the per-step limit, capped by what is left."""
    
            return max(0.0, min(_CLOSE_STEP_SECONDS, deadline - time.monotonic()))

        async def _await_barrier(
            self, awaitable: object, step: str, deadline: float
        ) -> bool:
            """Await one close-barrier step; report whether it actually settled.
    
            Returns ``False`` on timeout -- including when the total budget is
            already spent, in which case the step is not awaited at all. The
            caller needs this: a step that did not settle means somebody still
            holds the resource, and reporting a clean stop then would hide a live
            child behind a cleared reference.
            """
    
            budget = self._close_budget(deadline)
            if budget <= 0:
                self._write_log(
                    "worker.close_barrier_timeout", step=step, reason="budget_exhausted"
                )
                return False
            try:
                # shield: a barrier step is a *persistent* task or future that a
                # later close must be able to await again. wait_for cancels what it
                # times out on, so the previous version left the retained
                # _stderr_task and protocol.closed cancelled -- the next _close()
                # then raised CancelledError immediately, which made the whole
                # "keep the handles so a later close can finish" claim false.
                await asyncio.wait_for(
                    asyncio.shield(cast("Awaitable[object]", awaitable)),
                    timeout=budget,
                )
            except TimeoutError:
                self._write_log("worker.close_barrier_timeout", step=step)
                return False
            except asyncio.CancelledError:
                # Cancelled by something other than this wait; the resource is
                # still unaccounted for, so report it as unsettled rather than
                # letting the exception read as a clean close.
                self._write_log("worker.close_barrier_timeout", step=step, reason="cancelled")
                return False
            return True

        async def _close(self) -> None:
            process = self._process
            if process is None:
                return
            self._expected_stop = True
            deadline = time.monotonic() + _CLOSE_TOTAL_SECONDS
            protocol = self._process_protocol
            # Persisted like stderr/exit: after a shielded timeout the drain is
            # still running, and creating a second one on the next close put two
            # readers on the same stream (RuntimeError: concurrent stdout
            # reader). Reuse the live one instead.
            if self._stdout_task is None or self._stdout_task.done():
                self._stdout_task = asyncio.create_task(
                    self._drain_stdout(process, protocol)
                )
            stdout_task = self._stdout_task
            if self._stderr_task is None:
                self._stderr_task = asyncio.create_task(self._drain_stderr(process))
            if process.returncode is None:
                if process.stdin is not None:
                    process.stdin.close()
                    # Bounded: a child that never drains its stdin leaves
                    # wait_closed() pending forever, and this runs BEFORE the
                    # terminate/kill escalation -- so an unbounded await here
                    # means force-close is never reached at all.
                    try:
                        await asyncio.wait_for(
                            process.stdin.wait_closed(),
                            timeout=self._close_budget(deadline),
                        )
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    except TimeoutError:
                        self._write_log(
                            "worker.close_barrier_timeout", step="stdin_wait_closed"
                        )
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self._close_budget(deadline)
                    )
                except TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(
                            process.wait(), timeout=self._close_budget(deadline)
                        )
                    except TimeoutError:
                        process.kill()
                        # Even SIGKILL leaves a window (and an uninterruptible
                        # child never reaps); closing must still return.
                        try:
                            await asyncio.wait_for(
                                process.wait(), timeout=self._close_budget(deadline)
                            )
                        except TimeoutError:
                            self._write_log(
                                "worker.close_barrier_timeout", step="wait_after_kill"
                            )
            settled = True
            try:
                # The FD barrier stays a barrier -- returncode alone is
                # insufficient, the subprocess protocol closes only after all
                # three pipe protocols have delivered connection_lost -- but each
                # wait is bounded so a stuck pipe degrades to a reported dirty
                # close instead of hanging the caller forever.
                settled &= await self._await_barrier(
                    stdout_task, "stdout_drain", deadline
                )
                if self._stderr_task is not None:
                    settled &= await self._await_barrier(
                        self._stderr_task, "stderr_drain", deadline
                    )
                if protocol is not None:
                    settled &= await self._await_barrier(
                        protocol.closed, "protocol_closed", deadline
                    )
                if self._exit_task is not None:
                    settled &= await self._await_barrier(
                        self._exit_task, "exit_task", deadline
                    )
            finally:
                # Custody is released only when the child is reaped AND every pipe
                # has disconnected.  Clearing these references unconditionally made
                # force_stopped() report true off ``_process is None`` while the
                # child was still alive holding its FDs -- the absence of our
                # handle read as proof the process had stopped.  Observability
                # answers "what happened"; custody answers "who still holds it",
                # and the first does not imply the second, so logging the timeout
                # was honest but not sufficient.  On an unsettled close we keep the
                # handles, which makes force_stopped() false and running true on
                # their own, and leaves a later close able to finish the job.
                if settled and process.returncode is not None:
                    self._process = None
                    self._process_protocol = None
                    self._stdout_task = None
                    self._stderr_task = None
                    self._exit_task = None
                else:
                    self._write_log(
                        "worker.close_unsettled",
                        returncode=process.returncode,
                        barriers_settled=bool(settled),
                    )

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
            return f"pi RPC process exited with status {process.returncode}" + (
                f": {detail}" if detail else ""
            )

        async def query(self, prompt: str) -> None:
            self._turn_id = f"hyprial-turn-{uuid4().hex}"
            self._turn_prompt = prompt
            await self._write(
                {"id": self._turn_id, "type": "prompt", "message": prompt}
            )

        async def receive_response(self) -> AsyncIterator[object]:
            turn_id = self._turn_id
            if turn_id is None:
                raise ConnectionError("pi RPC turn was not started")
            prompt = self._turn_prompt
            if prompt is None:
                raise ConnectionError("pi RPC turn prompt was not retained")
            self._turn_id = None
            self._turn_prompt = None
            recovery_attempted = False
            while True:
                last_run_messages: list[object] = []
                pending_settle = False
                while True:
                    if pending_settle:
                        try:
                            message = await self._read(
                                timeout=self._settle_grace_seconds
                            )
                        except TimeoutError:
                            # Older builds never emit agent_settled; a quiet grace
                            # window after a non-retrying agent_end is the fallback
                            # completion check.
                            yield ProgressObservation(
                                phase="turn-end",
                                summary="pi turn settled",
                                terminal=True,
                            )
                            break
                    else:
                        message = await self._read()
                    kind = message.get("type")
                    observation = _pi_progress_observation(message)
                    if observation is not None:
                        yield observation
                    if kind == "response":
                        if (
                            message.get("id") == turn_id
                            and message.get("success") is not True
                        ):
                            yield _PiTurnOutcome(
                                str(message.get("error") or "pi rejected the prompt"),
                                is_error=True,
                            )
                            return
                        continue
                    if kind == "extension_ui_request":
                        await self._cancel_ui_dialog(message)
                        continue
                    if kind == "agent_end":
                        messages = message.get("messages")
                        if isinstance(messages, list):
                            last_run_messages = messages
                        pending_settle = message.get("willRetry") is not True
                        continue
                    if kind == "agent_settled":
                        break
                    if kind in _CONTINUATION_EVENTS:
                        pending_settle = False
    
                # A provider-level stop error outranks any text: the run whose last
                # assistant message errored did not answer, agent_settled means pi
                # will not continue it, and the stale-session fallback must never
                # stand in as the reply.  Before this check the same message object
                # already carried the provider's own errorMessage and it was never
                # read -- two machines retried a dead token three times on the
                # generic "no final assistant text" wording (card 260).
                provider_error = _assistant_provider_error(last_run_messages)
                if provider_error is not None:
                    yield _PiTurnOutcome(
                        provider_error, is_error=True, provider_error=provider_error
                    )
                    return
    
                exhaustion = _assistant_context_exhaustion(last_run_messages)
                if exhaustion is not None:
                    if recovery_attempted:
                        yield _PiTurnOutcome(
                            exhaustion,
                            is_error=True,
                            failure_code=ipc_errors.CONTEXT_EXHAUSTED,
                        )
                        return
                    compact_error = await self._compact_session()
                    if compact_error is not None:
                        yield _PiTurnOutcome(
                            f"{exhaustion}; compaction unavailable: {compact_error}",
                            is_error=True,
                            failure_code=ipc_errors.CONTEXT_EXHAUSTED,
                        )
                        return
                    recovery_attempted = True
                    turn_id = f"hyprial-turn-{uuid4().hex}"
                    await self._write(
                        {"id": turn_id, "type": "prompt", "message": prompt}
                    )
                    continue
    
                text = _final_assistant_text(last_run_messages)
                if text is None:
                    text = await self._last_assistant_text_fallback()
                if text is None:
                    yield _PiTurnOutcome(
                        "pi returned no final assistant text; "
                        f"{_assistant_failure_detail(last_run_messages)}",
                        is_error=True,
                    )
                    return
                yield _PiTurnOutcome(text)
                return

        async def interrupt(self) -> None:
            await self._write({"type": "abort"})

        def force_stop(self) -> None:
            """Terminate the direct RPC child; called from the manager thread."""
    
            process = self._process
            if process is None or process.returncode is not None:
                return
            try:
                process.terminate()
            except ProcessLookupError:
                return

        def _settle_if_complete(self) -> None:
            """Release custody once the outstanding barriers have finished.
    
            The product never calls _close() a second time: after an unsettled
            close the outer stop only calls force_stop()/force_stopped(). So
            convergence has to be reachable from the predicate itself, or a close
            that timed out could never finish even after its barriers completed --
            the retained handles would sit there and every later stop would keep
            reporting "not stopped".
    
            Every check here is a non-blocking ``done()``; nothing is awaited, so
            this is safe to call from the manager thread.
            """
    
            process = self._process
            if process is None or process.returncode is None:
                return
            for barrier in (self._stdout_task, self._stderr_task, self._exit_task):
                if barrier is not None and not barrier.done():
                    return
            protocol = self._process_protocol
            if protocol is not None and not protocol.closed.done():
                return
            self._process = None
            self._process_protocol = None
            self._stdout_task = None
            self._stderr_task = None
            self._exit_task = None
            self._write_log("worker.close_settled_late")

        def force_stopped(self) -> bool:
            """True only when custody is fully settled.
    
            Reaping the child is not enough: stdout/stderr drains and the
            subprocess protocol can still be outstanding, and those hold the pipe
            FDs. Answering off ``returncode is not None`` told the caller the
            worker was gone while its pipes were unaccounted for, so the outer
            stop stopped waiting for custody it never received.
    
            ``_process`` is the single truth, because custody is released only
            when every barrier settled AND the child was reaped -- either inside
            _close(), or here once late-finishing barriers complete.
            """
    
            self._settle_if_complete()
            return self._process is None

        async def _last_assistant_text_fallback(self) -> str | None:
            request_id = f"hyprial-last-text-{uuid4().hex}"
            await self._write({"type": "get_last_assistant_text", "id": request_id})
            response = await self._read_response(
                request_id, timeout=self._startup_timeout_seconds
            )
            data = response.get("data")
            text = data.get("text") if isinstance(data, dict) else None
            return text if isinstance(text, str) and text else None

        async def _compact_session(self) -> str | None:
            """Ask pi to compact once; return failure detail when unsupported."""
    
            request_id = f"hyprial-compact-{uuid4().hex}"
            try:
                await self._write({"type": "compact", "id": request_id})
                # Compaction summarises the whole context through the model, which
                # takes far longer than a startup probe; like the turn's own reads
                # it is bounded by the outer turn deadline, not by a local one.
                response = await self._read_response(request_id, timeout=None)
            except (ConnectionError, TimeoutError) as error:
                return _bounded(error, fallback="compact RPC unavailable")
            if response.get("success") is True:
                return None
            return _bounded(response.get("error"), fallback="compact RPC failed")

        async def _cancel_ui_dialog(self, message: dict[str, object]) -> None:
            if message.get("method") not in _UI_DIALOG_METHODS:
                return
            identifier = message.get("id")
            if isinstance(identifier, str):
                await self._write(
                    {
                        "type": "extension_ui_response",
                        "id": identifier,
                        "cancelled": True,
                    }
                )

        async def _read_response(
            self, request_id: str, *, timeout: float | None
        ) -> dict[str, object]:
            deadline = (
                None if timeout is None else asyncio.get_running_loop().time() + timeout
            )
            while True:
                remaining = (
                    None if deadline is None else deadline - asyncio.get_running_loop().time()
                )
                if remaining is not None and remaining <= 0:
                    raise ConnectionError(
                        f"pi RPC did not answer request '{request_id}' in time"
                    )
                message = await self._read(timeout=remaining)
                if message.get("type") == "response" and message.get("id") == request_id:
                    return message
                if message.get("type") == "extension_ui_request":
                    await self._cancel_ui_dialog(message)

        async def _write(self, message: dict[str, object]) -> None:
            process = self._process
            if process is None or process.stdin is None or process.returncode is not None:
                raise ConnectionError("pi RPC session is not running")
            process.stdin.write(
                json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise ConnectionError("pi RPC session input closed") from error

        async def _read(self, *, timeout: float | None = None) -> dict[str, object]:
            process = self._process
            if process is None or process.stdout is None:
                raise ConnectionError("pi RPC session is not running")
            read = process.stdout.readline()
            try:
                line = (
                    await read if timeout is None else await asyncio.wait_for(read, timeout)
                )
            except ValueError as error:
                raise ConnectionError("pi RPC emitted an oversized line") from error
            if not line:
                detail = self._stderr_tail.decode("utf-8", errors="replace")[-4096:]
                raise ConnectionError(
                    "pi RPC session exited unexpectedly"
                    + (f": {detail}" if detail else "")
                )
            # Strict JSONL framing: LF terminates a record and an optional
            # trailing CR is stripped; no other separator splits records.
            try:
                value = json.loads(line.rstrip(b"\r\n"))
            except json.JSONDecodeError as error:
                raise ConnectionError("pi RPC emitted invalid JSON") from error
            if not isinstance(value, dict):
                raise ConnectionError("pi RPC message must be an object")
            return value

        async def _drain_stdout(
            self,
            process: asyncio.subprocess.Process,
            protocol: _PiSubprocessProtocol | None,
        ) -> None:
            if process.stdout is None:
                return
            try:
                while await process.stdout.read(4096):
                    pass
            finally:
                if protocol is not None:
                    protocol.close_pipe(1)

        async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
            if process.stderr is None:
                return
            try:
                while chunk := await process.stderr.read(4096):
                    self._stderr_tail.extend(chunk)
                    if len(self._stderr_tail) > STDERR_TAIL_BYTES:
                        del self._stderr_tail[: len(self._stderr_tail) - STDERR_TAIL_BYTES]
            finally:
                protocol = self._process_protocol
                if protocol is not None:
                    protocol.close_pipe(2)

        async def _watch_process_exit(self) -> None:
            process = self._process
            if process is None:
                return
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
            logger = self._logger
            if logger is None:
                return
            logger.log(
                "error" if event == "worker.exited" else "info", event, **fields
            )

class _PiSubprocessProtocol(asyncio.subprocess.SubprocessStreamProtocol):
    """Expose the actual all-pipes-disconnected subprocess close barrier."""

    def __init__(
        self, limit: int, loop: asyncio.AbstractEventLoop
    ) -> None:
        super().__init__(limit, loop)
        self.closed: asyncio.Future[None] = loop.create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        try:
            super().connection_lost(exc)
        finally:
            if not self.closed.done():
                self.closed.set_result(None)

    def close_pipe(self, fd: int) -> None:
        transport = self._transport
        if transport is None:
            return
        pipe = transport.get_pipe_transport(fd)
        if pipe is not None:
            pipe.close()

def without_pi_reserved_arguments(args: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in _RESERVED_VALUE_OPTIONS:
            index += 2
            continue
        if value in _RESERVED_FLAG_OPTIONS:
            index += 1
            continue
        if any(value.startswith(f"{option}=") for option in _RESERVED_VALUE_OPTIONS):
            index += 1
            continue
        result.append(value)
        index += 1
    return tuple(result)

#: Observer seam for turn failures with spec context lives in
#: ``streaming.py`` (TurnFailureSpecObserver); PiRpcProcess adapts it below.

# RPC lines carry whole assistant messages; the asyncio default of 64 KiB
# would sever the session mid-turn on a long answer.
STREAM_LIMIT_BYTES = 8 * 1024 * 1024

STDERR_TAIL_BYTES = 16 * 1024

#: Per-step ceiling for every await in the close path.  Each step is bounded
#: separately so one stuck pipe cannot starve the escalation that follows it.
_CLOSE_STEP_SECONDS = 1.0

#: Ceiling for the WHOLE close path.  Per-step limits are not a total limit:
#: this path has six to eight bounded awaits, so step ceilings alone permit
#: ~8s while ``StreamingTurnProcess.stop()`` joins this thread with
#: ``stop_timeout_seconds`` (default 2.0s) -- a 4x overrun, which the caller
#: reports as a stop failure rather than as a slow close.  (That message is
#: itself being corrected on another branch, from an assertion about a
#: missing force-stop capability to a plain statement of the timeout, so this
#: comment describes the behaviour instead of quoting the text.)
#:
#: The margin is stated as arithmetic rather than as "a conservative value",
#: so a later edit can check it instead of re-deciding it:
#:
#:     stop_timeout_seconds  2.0s   caller's join window
#:   - _CLOSE_TOTAL_SECONDS  1.2s   this whole path, worst case
#:   ------------------------------
#:                           0.8s   left for the join itself and for
#:                                  scheduler jitter on a loaded host
#:
#: ``tests/test_pi_rpc_process.py`` asserts the inequality against the real
#: default, so raising either number without re-checking the other fails.
_CLOSE_TOTAL_SECONDS = 1.2

#: Slack that must remain for the caller's join plus scheduling jitter.
_CLOSE_CALLER_MARGIN_SECONDS = 0.8

# The connector owns the output mode and the session identity; strip the
# caller's session controls while preserving unrelated passthrough flags.
_RESERVED_VALUE_OPTIONS = {"--mode", "--session-id", "--session", "--fork"}

_RESERVED_FLAG_OPTIONS = {"--print", "-p", "--continue", "-c", "--resume", "-r"}

# Events that mean the run continues after an agent_end (auto retry,
# overflow compaction, or another queued run) — they disarm the
# settled-fallback grace window.
_CONTINUATION_EVENTS = {
    "agent_start",
    "auto_retry_start",
    "compaction_start",
    "summarization_retry_attempt_start",
}

# Dialog-style extension UI requests block the agent until answered; a
# headless connector always cancels them.  Fire-and-forget methods
# (notify, setStatus, ...) expect no response.
_UI_DIALOG_METHODS = {"select", "confirm", "input", "editor"}
