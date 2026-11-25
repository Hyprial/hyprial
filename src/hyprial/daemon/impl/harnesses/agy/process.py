# Decision record (file header; no inline comments by repository rule):
# - AgyAcpProcess subclasses StreamingTurnProcess without importing cells.actor
#   or constructing delivery envelopes, obeying test_cell_entry_gate.py.
# - On client reconnect after a session_id was established, _build_client passes
#   that session_id so StreamingTurnProcess resumes the same ACP session.
"""StreamingTurnProcess driver wrapping AgyAcpClient."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING

from hyprial.daemon.impl.harnesses.agy.acp_client import AgyAcpClient
from hyprial.daemon.impl.harnesses.agy.native_env import AgyAgentHomeError, prepare_agy_runtime_context
from hyprial.daemon.impl.harnesses.agy.projection import (
    project_agy_launch,
    validate_agy_provider_environment,
)
from hyprial.daemon.impl.harnesses.agy.protocol import (
    ACP_CANCEL_TIMEOUT_SECONDS_DEFAULT,
    ACP_REQUEST_TIMEOUT_SECONDS_DEFAULT,
)
from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess
from hyprial.daemon.impl.harnesses.streaming.protocol import (
    TurnClientFactory,
    TurnCompletedObserver,
    TurnFailureSpecObserver,
)
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel
from hyprial.identity import AgentRuntimeContext, ChildEnvironmentLaunch
from hyprial.kernel import HarnessLaunchSpec, Logger

if TYPE_CHECKING:
    from hyprial.kernel import ActorHost


class AgyAcpProcess(StreamingTurnProcess):
    """Serialize daemon deliveries through one persistent agy ACP session."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        client_factory: TurnClientFactory | None = None,
        command: tuple[str, ...] = ("agy",),
        env: Mapping[str, str] | None = None,
        worker_channel: WorkerChannel | None = None,
        reconnect_delay_seconds: float = 0.25,
        complete_launch: ChildEnvironmentLaunch | None = None,
        on_turn_failure_for_spec: TurnFailureSpecObserver | None = None,
        on_turn_completed: TurnCompletedObserver | None = None,
        runtime_launch_custody: (
            Callable[[AgentRuntimeContext], AbstractContextManager[None]] | None
        ) = None,
        actor_host: ActorHost | None = None,
        request_timeout_seconds: float = ACP_REQUEST_TIMEOUT_SECONDS_DEFAULT,
        cancel_timeout_seconds: float = ACP_CANCEL_TIMEOUT_SECONDS_DEFAULT,
        strict_resume: bool = True,
        session_root: Path | None = None,
    ) -> None:
        if spec.harness != "agy" or not spec.headless:
            raise ValueError("agy ACP requires a managed headless spec")
        if complete_launch is not None and env is not None:
            raise ValueError(
                "complete child environment cannot be combined with a partial env mapping"
            )
        self.spec = spec
        self._command = command
        self._env = dict(env) if env is not None else None
        self._complete_launch = complete_launch
        self._runtime_launch_custody = runtime_launch_custody
        self._request_timeout_seconds = request_timeout_seconds
        self._cancel_timeout_seconds = cancel_timeout_seconds
        self._strict_resume = strict_resume
        self.worker_channel = worker_channel
        base_env = complete_launch.environment.for_exec() if complete_launch is not None else {**os.environ, **(env or {})}
        runtime_context = complete_launch.runtime_context if complete_launch is not None else None
        channel_context = worker_channel.runtime_context if worker_channel is not None else None
        if complete_launch is not None and worker_channel is not None and channel_context is not runtime_context:
            raise AgyAgentHomeError("agy worker channel and child launch disagree on runtime context")
        if runtime_context is not None:
            if complete_launch is None or runtime_context.actor != complete_launch.actor:
                raise AgyAgentHomeError("agy runtime context actor does not match the child launch")
            if runtime_context.harness != "agy":
                raise AgyAgentHomeError("agy runtime context requires harness 'agy'")
            if base_env.get("HOME") != str(runtime_context.roots.tool_home):
                raise AgyAgentHomeError("complete child environment disagrees with agy runtime context")
            if session_root is not None and session_root != runtime_context.roots.session_root:
                raise AgyAgentHomeError("explicit session root disagrees with agy runtime context")
            resolved_session_root = runtime_context.roots.session_root
            prepare_agy_runtime_context(runtime_context)
        else:
            if complete_launch is not None and complete_launch.agent_home_profile_applied:
                raise AgyAgentHomeError("P2 agy launch requires an AgentRuntimeContext")
            if complete_launch is None and channel_context is not None:
                raise AgyAgentHomeError("agy runtime context requires complete_launch")
            resolved_session_root = session_root
        self._runtime_context = runtime_context
        self._session_root = (
            resolved_session_root
        )
        project_agy_launch(
            spec,
            command=command,
            session_root=self._session_root,
            worker_channel=worker_channel,
        )
        base_env = (
            complete_launch.environment.for_exec()
            if complete_launch is not None
            else {**os.environ, **(env or {})}
        )
        self._provider_environment = validate_agy_provider_environment(
            spec,
            base_env,
            allow_missing_google_key=False,
        )
        self._session_ref = spec.session_ref
        self._last_client: AgyAcpClient | None = None

        logger = (
            Logger.worker(worker_channel.state_dir, runtime="agy", name=spec.name)
            if worker_channel is not None
            else None
        )
        adapted_observer = (
            (
                lambda failure: on_turn_failure_for_spec(
                    failure,
                    harness="agy",
                    provider=spec.model_provider,
                    model=spec.model,
                    worker=spec.name,
                    runtime_context=(
                        runtime_context
                    ),
                )
            )
            if on_turn_failure_for_spec is not None
            else None
        )
        super().__init__(
            harness="agy",
            label="Agy ACP",
            client_factory=client_factory or self._build_client,
            thread_name=f"hyprial-agy-acp-{spec.name}",
            logger=logger,
            reconnect_delay_seconds=reconnect_delay_seconds,
            force_stop=self._force_stop_client,
            force_stopped=self._force_stopped_client,
            on_turn_failure=adapted_observer,
            on_turn_completed=on_turn_completed,
            actor_host=actor_host,
        )

    @property
    def session_ref(self) -> str | None:
        """The latest ACP session id established or targeted by this process."""

        with self._lock:
            client = self._client
        if client is not None:
            sid = getattr(client, "session_id", None)
            if isinstance(sid, str) and sid:
                return sid
        if self._last_client is not None and self._last_client.session_id:
            return self._last_client.session_id
        return self._session_ref

    def read_run_activity(self) -> tuple[bool, int | None, int | None] | None:
        """Expose current client activity to observability readers."""

        with self._lock:
            client = self._client
        if client is None:
            return None
        reader = getattr(client, "read_run_activity", None)
        return reader() if callable(reader) else None

    def _force_stop_client(self) -> None:
        with self._lock:
            client = self._client or self._connecting_client
        force_stop = getattr(client, "force_stop", None)
        if callable(force_stop):
            force_stop()

    def _force_stopped_client(self) -> bool:
        with self._lock:
            client = self._client or self._connecting_client
        if client is None:
            return True
        force_stopped = getattr(client, "force_stopped", None)
        return bool(force_stopped()) if callable(force_stopped) else True

    def _build_client(self) -> AgyAcpClient:
        if self._last_client is not None and self._last_client.session_id:
            self._session_ref = self._last_client.session_id
        client = AgyAcpClient(
            self.spec,
            self._session_ref,
            command=self._command,
            env=self._env,
            worker_channel=self.worker_channel,
            request_timeout_seconds=self._request_timeout_seconds,
            cancel_timeout_seconds=self._cancel_timeout_seconds,
            complete_launch=self._complete_launch,
            session_root=self._session_root,
            runtime_launch_custody=self._runtime_launch_custody,
            provider_environment=self._provider_environment,
            strict_resume=self._strict_resume,
        )
        self._last_client = client
        return client
