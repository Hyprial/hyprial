"""Long-lived Claude Agent SDK streaming process owned by the harness layer."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from hyprial.daemon.desired_state import HarnessLaunchSpec
from hyprial.log import Logger
from hyprial.transfer.container import (
    CONTAINER_SDK_PYTHON,
    CONTAINER_SDK_WORKER,
    wrap_worker_launch,
)

from .codex import PROCESS_FORCE_JOIN_SECONDS, _OwnedProcessGroup
from .common import summarize_stderr
from .model_provider import claude_provider_environment
from .streaming import ProgressObservation, StreamingTurnProcess, TurnClient, TurnClientFactory
from .worker_channel import WorkerChannel

# The turn-client seam predates the shared pump under these names; retain
# them so existing composition and tests keep one import path.
AgentSdkClient = TurnClient
ClientFactory = TurnClientFactory
AGENT_SDK_VERSION = "0.2.125"


def _option_value(args: tuple[str, ...], option: str) -> str | None:
    for index, value in enumerate(args):
        if value == option and index + 1 < len(args):
            return args[index + 1]
        prefix = f"{option}="
        if value.startswith(prefix):
            return value[len(prefix) :]
    return None


def _extra_args(args: tuple[str, ...]) -> dict[str, str | None]:
    """Translate ordinary CLI flags into the SDK's explicit passthrough map."""

    result: dict[str, str | None] = {}
    index = 0
    while index < len(args):
        value = args[index]
        if not value.startswith("--"):
            index += 1
            continue
        name, separator, inline = value[2:].partition("=")
        if name in {"model", "session-id", "resume"}:
            index += 1 if separator else 2
            continue
        if separator:
            result[name] = inline
            index += 1
        elif index + 1 < len(args) and not args[index + 1].startswith("--"):
            result[name] = args[index + 1]
            index += 2
        else:
            result[name] = None
            index += 1
    return result


def sdk_worker_command(*, uv_executable: str | None = None) -> tuple[str, ...]:
    uv = uv_executable or shutil.which("uv")
    if uv is None:
        user_uv = Path.home() / ".local" / "bin" / (
            "uv.exe" if os.name == "nt" else "uv"
        )
        if user_uv.is_file() and os.access(user_uv, os.X_OK):
            uv = str(user_uv)
    if uv is None:
        raise RuntimeError("uv is required to launch the isolated Claude Agent SDK")
    worker = Path(__file__).with_name("_agent_sdk_worker.py")
    return (
        uv,
        "run",
        "--isolated",
        "--no-project",
        "--with",
        f"claude-agent-sdk=={AGENT_SDK_VERSION}",
        "python",
        str(worker),
    )


@dataclass(frozen=True, slots=True)
class _WireResult:
    result: str
    is_error: bool = False


#: Larger stderr budget for startup-critical failure records (startup
#: timeout, unexpected worker exit): a rate-limit error body the CLI kept
#: retrying against must not be truncated away from the explaining line.
_STARTUP_STDERR_TAIL_LINES = 32
_STARTUP_STDERR_TAIL_BYTES = 16 * 1024


def _startup_stderr_tail(raw: bytes) -> str:
    return summarize_stderr(
        raw,
        max_lines=_STARTUP_STDERR_TAIL_LINES,
        max_bytes=_STARTUP_STDERR_TAIL_BYTES,
    )


class IsolatedAgentSdkClient:
    """Agent SDK client facade backed by a dependency-isolated worker process."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        env: Mapping[str, str] | None = None,
        uv_executable: str | None = None,
        worker_command: Sequence[str] | None = None,
        worker_channel: WorkerChannel | None = None,
        logger: Logger | None = None,
        startup_timeout_seconds: float = 300.0,
        session_id: str | None = None,
        resume: str | None = None,
        on_session_established: Callable[[str], None] | None = None,
        process_group: _OwnedProcessGroup | None = None,
    ) -> None:
        if session_id is not None and resume is not None:
            raise ValueError("session_id and resume are mutually exclusive")
        self.spec = spec
        # OS-level custody of the worker's process group, shared with the
        # harness wrapper so a stop that the graceful protocol cannot complete
        # (a worker still in its ``uv`` install phase never reads "stop") can
        # be forced by PID group instead of hanging the lifecycle effect.
        self._process_group = process_group
        self._on_session_established = on_session_established
        self.command = (
            tuple(worker_command)
            if worker_command
            else (
                # The image carries a persistent SDK venv: no runtime uv
                # resolve, no startup network (P0 lesson #3).
                (CONTAINER_SDK_PYTHON, CONTAINER_SDK_WORKER)
                if spec.containerized
                else sdk_worker_command(uv_executable=uv_executable)
            )
        )
        base_environment = {**os.environ, **(env or {})}
        provider_environment = claude_provider_environment(spec, base_environment)
        self.options: dict[str, Any] = {
            "cwd": spec.cwd,
            "model": spec.model or _option_value(spec.args, "--model"),
            "extraArgs": _extra_args(spec.args),
        }
        # Session persistence, owned by the daemon: a stored ref resumes the
        # conversation after a daemon restart; a fresh mint pins the id so
        # the conversation can be resumed later.  The worker reports the
        # effective id in its ready message (a dead resume target falls back
        # to a fresh session there).
        if resume is not None:
            self.options["resume"] = resume
        elif session_id is not None:
            self.options["sessionId"] = session_id
        if worker_channel is not None:
            # Speak to the daemon as this worker's own canonical actor.  Two
            # mechanisms keep the coordinator's ambient harness-bridge server
            # out of the child: settingSources drops the user/local sources it
            # can arrive through, and strictMcpConfig makes the CLI use ONLY the
            # servers we inject via mcp_servers -- so no settings-file server
            # (from any source) can slip back in.  "project" is retained on
            # purpose: it is required to load the worker's CLAUDE.md and project
            # permissions, which for residents ARE the worker's behavior.
            self.options["settingSources"] = ["project"]
            self.options["strictMcpConfig"] = True
            self.options["mcpServers"] = {
                "harness-bridge": worker_channel.mcp_server
            }
            self.options["allowedTools"] = list(worker_channel.allowed_tools)
        self._env = {
            **base_environment,
            **provider_environment,
            "HYPRIAL_AGENT_SDK_OPTIONS": json.dumps(
                self.options,
                separators=(",", ":"),
            ),
        }
        if spec.containerized:
            if worker_channel is None:
                raise ValueError(
                    "containerized claude workers require a worker channel"
                )
            self.command = wrap_worker_launch(
                spec,
                inner_argv=self.command,
                # The container receives ONLY this delta (plus the proxy
                # whitelist) through -e flags; the docker run child itself
                # keeps the daemon environment, which docker ignores.
                env_delta={
                    **(env or {}),
                    **provider_environment,
                    "HYPRIAL_AGENT_SDK_OPTIONS": self._env[
                        "HYPRIAL_AGENT_SDK_OPTIONS"
                    ],
                },
                state_dir=worker_channel.state_dir,
            )
        self._startup_timeout_seconds = startup_timeout_seconds
        self._logger = logger
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._expected_stop = False
        self._exit_logged = False

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
        return f"Agent SDK worker exited with status {process.returncode}" + (
            f": {detail}" if detail else ""
        )

    async def __aenter__(self) -> Self:
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            # Own a distinct process group so a forced stop can drain the whole
            # ``uv run`` -> python worker tree by PGID without touching the
            # daemon (the worker otherwise inherits the daemon's group).
            start_new_session=True,
        )
        if self._process_group is not None:
            # Publishing ownership can raise if the group is already stopping or
            # its birth identity is unreadable; let that fail the client so the
            # reconnect loop treats it as an unhealthy start.
            self._process_group.register(self._process.pid)
        self._expected_stop = False
        self._exit_logged = False
        if self._logger is not None:
            self._logger.info("worker.started", pid=self._process.pid)
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._exit_task = asyncio.create_task(self._watch_process_exit())
        try:
            ready = await self._read_message(timeout=self._startup_timeout_seconds)
            if ready.get("type") != "ready":
                raise ConnectionError(f"Agent SDK worker failed to start: {ready!r}")
            session_id = ready.get("sessionId")
            if (
                isinstance(session_id, str)
                and session_id
                and self._on_session_established is not None
            ):
                self._on_session_established(session_id)
            if self._logger is not None:
                self._logger.info("worker.ready", pid=self._process.pid)
            return self
        except BaseException as error:
            if isinstance(error, TimeoutError) and self._logger is not None:
                # Distinct from worker.exited: the process is ALIVE but never
                # emitted ready (a hung connect, e.g. bad harness args leaving
                # the CLI on stdin, or a throttled handshake). Without this
                # event the restart loop is silent about why.
                self._logger.log(
                    "error",
                    "worker.startup_timeout",
                    pid=self._process.pid,
                    timeoutSeconds=self._startup_timeout_seconds,
                    stderrTail=_startup_stderr_tail(bytes(self._stderr_tail)),
                )
            await self._close_process()
            raise

    async def __aexit__(self, *args: object) -> bool:
        self._expected_stop = True
        await self._close_process()
        return False

    async def _close_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            try:
                await self._write_message({"op": "stop"})
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except (ConnectionError, TimeoutError):
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        if self._exit_task is not None:
            await self._exit_task
        self._write_exit_log(process, process.returncode)
        if self._process_group is not None:
            self._process_group.release_if_gone(process.pid)
        self._process = None
        self._stderr_task = None
        self._exit_task = None

    async def query(self, prompt: str) -> None:
        await self._write_message({"op": "query", "prompt": prompt})

    async def receive_response(self) -> AsyncIterator[object]:
        while True:
            message = await self._read_message()
            message_type = message.get("type")
            if message_type == "progress":
                phase = message.get("phase")
                summary = message.get("summary")
                if not isinstance(phase, str) or not isinstance(summary, str):
                    # Progress is advisory; a malformed side-channel frame from
                    # the worker must not fail the authoritative turn result.
                    continue
                detail = message.get("detail")
                tool_call_id = message.get("toolCallId")
                tool_name = message.get("toolName")
                yield ProgressObservation(
                    phase=phase,
                    summary=summary,
                    tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
                    tool_name=tool_name if isinstance(tool_name, str) else None,
                    detail=detail if isinstance(detail, dict) else None,
                    terminal=message.get("terminal") is True,
                )
                continue
            if message_type != "result":
                raise ConnectionError(
                    f"unexpected Agent SDK worker message: {message!r}"
                )
            output = message.get("result", "")
            if not isinstance(output, str):
                output = str(output)
            yield _WireResult(output, is_error=message.get("isError") is True)
            return

    async def interrupt(self) -> None:
        await self._write_message({"op": "interrupt"})

    async def _write_message(self, message: dict[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ConnectionError("Agent SDK worker is not running")
        process.stdin.write(
            json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise ConnectionError("Agent SDK worker input closed") from error

    async def _read_message(self, *, timeout: float | None = None) -> dict[str, object]:
        process = self._process
        if process is None or process.stdout is None:
            raise ConnectionError("Agent SDK worker is not running")
        read = process.stdout.readline()
        line = await read if timeout is None else await asyncio.wait_for(read, timeout)
        if not line:
            detail = self._stderr_tail.decode("utf-8", errors="replace")[-4096:]
            raise ConnectionError(
                "Agent SDK worker exited before replying"
                + (f": {detail}" if detail else "")
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ConnectionError("Agent SDK worker emitted invalid JSON") from error
        if not isinstance(value, dict):
            raise ConnectionError("Agent SDK worker message must be an object")
        return value

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.read(4096):
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > 16 * 1024:
                del self._stderr_tail[: len(self._stderr_tail) - 16 * 1024]

    async def _watch_process_exit(self) -> None:
        process = self._process
        if process is None:
            return
        returncode = await process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        if self._logger is not None:
            self._write_exit_log(process, returncode)

    def _write_exit_log(
        self, process: asyncio.subprocess.Process, returncode: int | None
    ) -> None:
        if self._logger is None or self._exit_logged or returncode is None:
            return
        self._exit_logged = True
        event = "worker.stopped" if self._expected_stop else "worker.exited"
        self._logger.log(
            "info" if self._expected_stop else "error",
            event,
            pid=process.pid,
            returnCode=returncode,
            stderrTail=(
                summarize_stderr(bytes(self._stderr_tail))
                if self._expected_stop
                else _startup_stderr_tail(bytes(self._stderr_tail))
            ),
        )


def create_claude_sdk_client(
    spec: HarnessLaunchSpec,
    *,
    env: Mapping[str, str] | None = None,
    worker_channel: WorkerChannel | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    on_session_established: Callable[[str], None] | None = None,
    process_group: _OwnedProcessGroup | None = None,
) -> AgentSdkClient:
    logger = (
        Logger.worker(worker_channel.state_dir, runtime="claude", name=spec.name)
        if worker_channel is not None
        else None
    )
    return IsolatedAgentSdkClient(
        spec,
        env=env,
        worker_channel=worker_channel,
        logger=logger,
        session_id=session_id,
        resume=resume,
        on_session_established=on_session_established,
        process_group=process_group,
    )


class ClaudeAgentSdkProcess(StreamingTurnProcess):
    """Serialize daemon deliveries through one persistent SDK conversation."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        client_factory: ClientFactory | None = None,
        env: Mapping[str, str] | None = None,
        worker_channel: WorkerChannel | None = None,
        reconnect_delay_seconds: float = 0.25,
    ) -> None:
        if spec.harness != "claude" or not spec.headless:
            raise ValueError("Claude Agent SDK requires a managed headless spec")
        self.spec = spec
        # Session identity, pinned by the daemon so a restart can resume the
        # conversation: a spec that carries a ref is a RESUME of a session a
        # previous daemon run persisted; a fresh worker mints its id here and
        # passes it as session_id until the first connect establishes it.
        # Reconnects then resume the established id.  A dead resume target is
        # replaced inside the worker (one fresh-session retry) and the
        # replacement flows back through _session_established.
        self._session_id = spec.session_ref or str(uuid4())
        self._established = spec.session_ref is not None
        self._env = env
        # One stable worker identity across client reconnects: every reconnected
        # SDK client re-injects the same canonical actor and session ref.
        self.worker_channel = worker_channel
        # Shared OS custody of the current client's worker group.  A graceful
        # stop can time out when the worker is still resolving its ``uv``
        # environment; force_stop then drains the group by PID so the lifecycle
        # effect settles instead of leaving the wrapper alive-but-unstoppable.
        self._process_group = _OwnedProcessGroup()
        logger = (
            Logger.worker(worker_channel.state_dir, runtime="claude", name=spec.name)
            if worker_channel is not None
            else None
        )
        super().__init__(
            harness="claude",
            label="Claude Agent SDK",
            client_factory=client_factory or self._build_client,
            thread_name=f"hyprial-claude-sdk-{spec.name}",
            logger=logger,
            reconnect_delay_seconds=reconnect_delay_seconds,
            force_stop=self._process_group.force_close,
            force_stopped=self._process_group.stopped,
            force_stop_join_seconds=PROCESS_FORCE_JOIN_SECONDS,
            liveness_probe=self._process_group.liveness,
        )

    @property
    def session_ref(self) -> str:
        """The session id the current (or next) client resumes or establishes."""
        return self._session_id

    def _session_established(self, session_id: str) -> None:
        self._session_id = session_id
        self._established = True

    def _build_client(self) -> AgentSdkClient:
        return create_claude_sdk_client(
            self.spec,
            env=self._env,
            worker_channel=self.worker_channel,
            session_id=None if self._established else self._session_id,
            resume=self._session_id if self._established else None,
            on_session_established=self._session_established,
            process_group=self._process_group,
        )
