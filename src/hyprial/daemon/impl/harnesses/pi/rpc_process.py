"""PiRpcProcess: the streaming process driver over PiRpcClient."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from uuid import uuid4

from hyprial.kernel import HarnessLaunchSpec
from hyprial.kernel import Logger

from hyprial.daemon.impl.harnesses.streaming.process  import (
    StreamingTurnProcess,
    )
from hyprial.daemon.impl.harnesses.streaming.protocol  import (
    TurnClientFactory,
    TurnCompletedObserver,
    TurnFailureSpecObserver,
)
from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel
from hyprial.identity import ChildEnvironmentLaunch
from hyprial.identity import AgentRuntimeContext
from hyprial.daemon.impl.harnesses.pi.rpc_client import (
    PiRpcClient,
)

class PiRpcProcess(StreamingTurnProcess):
    """Serialize daemon deliveries through one persistent pi RPC session."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        client_factory: TurnClientFactory | None = None,
        command: tuple[str, ...] = ("pi",),
        env: Mapping[str, str] | None = None,
        worker_channel: WorkerChannel | None = None,
        reconnect_delay_seconds: float = 0.25,
        complete_launch: "ChildEnvironmentLaunch | None" = None,
        on_turn_failure_for_spec: TurnFailureSpecObserver | None = None,
        on_turn_completed: TurnCompletedObserver | None = None,
        runtime_launch_custody: (
            Callable[[AgentRuntimeContext], AbstractContextManager[None]] | None
        ) = None,
    ) -> None:
        if spec.harness != "pi" or not spec.headless:
            raise ValueError("pi RPC requires a managed headless spec")
        if complete_launch is not None and env is not None:
            raise ValueError(
                "complete child environment cannot be combined with a "
                "partial env mapping"
            )
        self.spec = spec
        self._complete_launch = complete_launch
        self._runtime_launch_custody = runtime_launch_custody
        # One stable session id across client reconnects: pi's --session-id
        # creates the session if missing, so a crashed process resumes the
        # same conversation instead of starting over.  A spec that carries a
        # ref is a RESUME (the ref was persisted by a previous daemon run);
        # resume of a session pi cannot open must not become a startup
        # failure source, so _build_client falls back to a fresh id once.
        self._persisted_ref = spec.session_ref
        self._session_ref = spec.session_ref or str(uuid4())
        self._last_client: PiRpcClient | None = None
        self._command = command
        self._env = env
        # The complete environment is resolved ONCE per process (at factory
        # time) and re-bound verbatim into every reconnected RPC client: a
        # reconnect must not re-resolve grants behind a rotating credential
        # revision, and must not drift back toward the ambient environment.
        
        # One stable worker identity across client reconnects: every
        # reconnected RPC client re-injects the same canonical actor and
        # harness-bridge extension (same contract as ClaudeAgentSdkProcess).
        self.worker_channel = worker_channel
        logger = (
            Logger.worker(worker_channel.state_dir, runtime="pi", name=spec.name)
            if worker_channel is not None
            else None
        )
        # Adapt the generic failure-text observer to the provider_auth
        # coordinator's signature: the spec carries this worker's provider,
        # model, and short name, which the failure text alone does not.
        adapted_observer = (
            (
                lambda failure: on_turn_failure_for_spec(
                    failure,
                    harness="pi",
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
        )
        super().__init__(
            harness="pi",
            label="Pi RPC",
            client_factory=client_factory or self._build_client,
            thread_name=f"hyprial-pi-rpc-{spec.name}",
            logger=logger,
            reconnect_delay_seconds=reconnect_delay_seconds,
            force_stop=self._force_stop_client,
            force_stopped=self._force_stopped_client,
            on_turn_failure=adapted_observer,
            on_turn_completed=on_turn_completed,
        )

    @property
    def session_ref(self) -> str:
        """The session id the current (or next) client launches with."""
        return self._session_ref

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

    def _build_client(self) -> PiRpcClient:
        ref = self._session_ref
        last = self._last_client
        if (
            self._persisted_ref is not None
            and ref == self._persisted_ref
            and last is not None
            and not last.reached_ready
        ):
            # The persisted session ref could not be resumed (e.g. a corrupt
            # session file makes pi exit before answering the ready probe).
            # Fall back to a cold start with a fresh id exactly once; a
            # genuinely broken pi keeps failing with the fresh id and stays
            # visible as an error instead of churning through ids.
            ref = self._session_ref = str(uuid4())
            if self._logger is not None:
                self._logger.info(
                    "worker.session_ref.lost",
                    previousSessionId=self._persisted_ref,
                )
        client = PiRpcClient(
            self.spec,
            ref,
            command=self._command,
            env=self._env,
            worker_channel=self.worker_channel,
            complete_launch=self._complete_launch,
            runtime_launch_custody=self._runtime_launch_custody,
        )
        self._last_client = client
        return client
