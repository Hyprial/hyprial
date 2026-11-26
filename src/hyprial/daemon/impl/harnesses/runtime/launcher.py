"""Daemon-compatible harness launcher composition."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING

from hyprial.kernel import ManagedHarnessProcess
from hyprial.kernel import HarnessLaunchSpec

if TYPE_CHECKING:
    from hyprial.kernel import ActorHost

from hyprial.daemon.impl.harnesses.agent_sdk  import ClaudeAgentSdkProcess
from hyprial.daemon.impl.harnesses.agy import AgyAcpProcess
from hyprial.daemon.impl.harnesses.agy.protocol import ACP_REQUEST_TIMEOUT_SECONDS_DEFAULT
from hyprial.daemon.impl.harnesses.model_provider import agy_provider_environment
from hyprial.daemon.impl.correlation.readiness_budget import START_TIMEOUT_SECONDS_DEFAULT
from hyprial.daemon.impl.harnesses.capabilities  import CapabilitySupport, support
from hyprial.kernel import SupportLevel
from hyprial.kernel import Capability
from hyprial.daemon.impl.harnesses.codex.app_server  import (
    APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT,
    )
from hyprial.daemon.impl.harnesses.codex.client  import (
    CodexAppServerProcess,
    )
from hyprial.daemon.impl.harnesses.protocol.common  import HarnessStartError
from hyprial.daemon.impl.harnesses.pi.rpc_process import (
    PI_RPC_STARTUP_TIMEOUT_SECONDS_DEFAULT,
    PiRpcProcess,
)
from hyprial.daemon.impl.harnesses.streaming.protocol import TurnFailureSpecObserver
from hyprial.daemon.impl.harnesses.python_worker  import (
    PYTHON_WORKER_STARTUP_TIMEOUT_SECONDS_DEFAULT,
    PythonHarnessProcess,
)
from hyprial.daemon.impl.harnesses.streaming.protocol  import TurnCompletedObserver
from hyprial.identity import ChildEnvironmentLaunch
from hyprial.identity import AgentRuntimeContext
from .native_auth import native_auth_observer
from hyprial.daemon.impl.harnesses.model_routes.connection import BoundChildEnvironmentLaunch
from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel

WorkerChannelFactory = Callable[[HarnessLaunchSpec], WorkerChannel | None]

#: P1b B1: (spec, worker channel) → the complete child environment launch.
#: Returning ``None`` keeps the carrier's legacy env handling; raising fails
#: the worker start loudly (no ambient fallback exists by construction).
ChildEnvironmentFactory = Callable[
    [HarnessLaunchSpec, WorkerChannel], "ChildEnvironmentLaunch | None"
]
RuntimeLaunchCustody = Callable[
    [AgentRuntimeContext], AbstractContextManager[None]
]

# Mechanisms whose HEADLESS_EXEC entry gives a managed streaming process
# (Agent SDK, pi RPC mode, and Codex app-server).
_STREAMING_MECHANISMS = frozenset(
    {"agent_sdk", "rpc", "app_server", "python_worker", "acp"}
)


def _headless_exec(harness: str) -> CapabilitySupport | None:
    """The declared HEADLESS_EXEC support, or None when undeclared/omitted."""

    try:
        return support(harness, headless=True, capability=Capability.HEADLESS_EXEC)
    except KeyError:
        return None


def is_streaming_spec(spec: HarnessLaunchSpec) -> bool:
    """True when the default launcher gives this spec a streaming process."""

    declared = _headless_exec(spec.harness)
    return bool(
        spec.headless
        and declared is not None
        and declared.level is SupportLevel.NATIVE
        and declared.mechanism in _STREAMING_MECHANISMS
    )


class HarnessLauncher:
    """Concrete implementation of ``hyprial.daemon.impl.api.HarnessLauncher``."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        codex_startup_timeout_seconds: float = (
            APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT
        ),
        agy_request_timeout_seconds: float = ACP_REQUEST_TIMEOUT_SECONDS_DEFAULT,
        last_session_id_reader: Callable[[HarnessLaunchSpec], str | None] | None = None,
        agent_sdk_factory: Callable[[HarnessLaunchSpec], ManagedHarnessProcess]
        | None = None,
        pi_rpc_factory: Callable[[HarnessLaunchSpec], ManagedHarnessProcess]
        | None = None,
        codex_app_server_factory: Callable[
            [HarnessLaunchSpec], ManagedHarnessProcess
        ]
        | None = None,
        python_worker_factory: Callable[[HarnessLaunchSpec], ManagedHarnessProcess]
        | None = None,
        acp_factory: Callable[[HarnessLaunchSpec], ManagedHarnessProcess]
        | None = None,
        worker_channel_factory: WorkerChannelFactory | None = None,
        child_environment_factory: "ChildEnvironmentFactory | None" = None,
        runtime_launch_custody: "RuntimeLaunchCustody | None" = None,
        state_dir: Path | None = None,
        daemon_epoch: str | None = None,
        turn_failure_observer: TurnFailureSpecObserver | None = None,
        turn_completed_observer: TurnCompletedObserver | None = None,
        process_observer: Callable[[HarnessLaunchSpec, ManagedHarnessProcess], None]
        | None = None,
        actor_host: ActorHost | None = None,
    ) -> None:
        del state_dir
        self._last_session_id_reader = last_session_id_reader
        # P7: every managed process this launcher builds takes its turn-pump
        # cells from the daemon's one host instead of starting an engine per
        # session. Standalone launchers (tests/tools) leave it None and keep
        # the custody private-host fallback.
        self._actor_host = actor_host
        self._codex_startup_timeout_seconds = codex_startup_timeout_seconds
        self._agy_request_timeout_seconds = agy_request_timeout_seconds
        # Absent a factory the worker channel is None, so the managed worker
        # keeps its pre-fix ambient configuration; only a daemon that wires the
        # factory gives each worker its own canonical Harness identity.
        self._worker_channel_factory = worker_channel_factory
        # P1b B1: per-worker complete child environment (resolver →
        # LaunchSpec private values → build_complete_child_environment).
        self._child_environment_factory = child_environment_factory
        self._runtime_launch_custody = runtime_launch_custody
        self._legacy_env = env
        self._daemon_epoch = daemon_epoch
        # provider_auth's coordinator, when the daemon wires one; passed
        # through to every managed streaming runtime that can report a
        # model-vendor failed turn.
        self._turn_failure_observer = turn_failure_observer
        self._turn_completed_observer = turn_completed_observer
        self._process_observer = process_observer
        self._agent_sdk_factory = agent_sdk_factory or self._make_agent_sdk_process
        self._pi_rpc_factory = pi_rpc_factory or self._make_pi_process
        self._codex_app_server_factory = (
            codex_app_server_factory or self._make_codex_process
        )
        self._python_worker_factory = python_worker_factory or self._make_python_process
        self._acp_factory = acp_factory or self._make_agy_process

    def _worker_channel_for(self, spec: HarnessLaunchSpec) -> WorkerChannel | None:
        if self._worker_channel_factory is None:
            return None
        return self._worker_channel_factory(spec)

    def _complete_launch_for(
        self, spec: HarnessLaunchSpec, channel: WorkerChannel | None
    ) -> "ChildEnvironmentLaunch | None":
        """The per-worker complete environment, or None to keep legacy env.

        A factory returning a launch makes the carrier spawn under complete
        replacement; a factory refusing (raising) fails the start loudly —
        there is no ambient fallback path by construction.
        """

        launch = (None if self._child_environment_factory is None or channel is None
                  else self._child_environment_factory(spec, channel))
        if spec.model_binding is not None and (
            not isinstance(launch, BoundChildEnvironmentLaunch) or launch.model_binding is None
        ):
            from hyprial.error import codes
            from hyprial.daemon.impl.harnesses.pi.bindings.errors import PiModelBindingError

            raise PiModelBindingError(codes.MODEL_AUTH_UNAVAILABLE, "authorize", "unreadable")
        return launch

    def _make_agent_sdk_process(
        self, spec: HarnessLaunchSpec
    ) -> "ClaudeAgentSdkProcess":
        """ONE channel mint, ONE environment from that channel (B2)."""

        channel = self._worker_channel_for(spec)
        launch = self._complete_launch_for(spec, channel)
        return ClaudeAgentSdkProcess(
            spec,
            env=self._legacy_env,
            worker_channel=channel,
            complete_launch=launch,
            on_turn_failure_for_spec=self._turn_failure_observer,
            on_turn_completed=native_auth_observer(launch, self._turn_completed_observer),
            runtime_launch_custody=self._runtime_launch_custody,
            last_session_id=(
                self._last_session_id_reader(spec)
                if self._last_session_id_reader is not None else None
            ),
            actor_host=self._actor_host,
        )

    def _make_codex_process(self, spec: HarnessLaunchSpec) -> "CodexAppServerProcess":
        """ONE channel mint, ONE environment from that channel (B2)."""

        channel = self._worker_channel_for(spec)
        launch = self._complete_launch_for(spec, channel)
        return CodexAppServerProcess(
            spec,
            env=self._legacy_env if launch is None else None,
            worker_channel=channel,
            command=spec.resolved_command(("codex",)),
            complete_launch=launch,
            on_turn_failure_for_spec=self._turn_failure_observer,
            on_turn_completed=native_auth_observer(launch, self._turn_completed_observer),
            runtime_launch_custody=self._runtime_launch_custody,
            actor_host=self._actor_host,
        )

    def _make_pi_process(self, spec: HarnessLaunchSpec) -> "PiRpcProcess":
        """Build one pi worker: ONE channel mint, ONE environment built from
        that same channel.

        The complete environment and the carrier must share the identical
        worker identity — minting the channel twice would bind the env to
        one session ref and the bridge to another (B1: found live as a
        double-mint in this wiring; the restore-isolation e2e caught it).
        A factory refusing (raising) fails the start loudly — there is no
        ambient fallback path by construction.
        """

        channel = self._worker_channel_for(spec)
        complete_launch = self._complete_launch_for(spec, channel)
        env = self._legacy_env if complete_launch is None else None
        return PiRpcProcess(
            spec,
            env=env,
            worker_channel=channel,
            command=spec.resolved_command(("pi",)),
            complete_launch=complete_launch,
            **({"model_binding": complete_launch.model_binding}
               if isinstance(complete_launch, BoundChildEnvironmentLaunch) else {}),
            on_turn_failure_for_spec=self._turn_failure_observer,
            on_turn_completed=native_auth_observer(complete_launch, self._turn_completed_observer),
            runtime_launch_custody=self._runtime_launch_custody,
            actor_host=self._actor_host,
        )

    def _make_python_process(self, spec: HarnessLaunchSpec) -> "PythonHarnessProcess":
        """Build the Jev worker with one identity and one complete env."""

        channel = self._worker_channel_for(spec)
        complete_launch = None
        env = self._legacy_env
        if self._child_environment_factory is not None and channel is not None:
            complete_launch = self._child_environment_factory(spec, channel)
            if complete_launch is not None:
                env = None
        return PythonHarnessProcess(
            spec,
            env=env,
            worker_channel=channel,
            command=spec.command or None,
            complete_launch=complete_launch,
            logger=None,
            on_turn_completed=self._turn_completed_observer,
        )

    def _make_agy_process(self, spec: HarnessLaunchSpec) -> "AgyAcpProcess":
        """Build one agy ACP worker from one channel and its complete environment."""

        channel = self._worker_channel_for(spec)
        complete_launch = self._complete_launch_for(spec, channel)
        base_env = (
            complete_launch.environment.for_exec()
            if complete_launch is not None
            else {**os.environ, **(self._legacy_env or {})}
        )
        agy_provider_environment(
            spec,
            base_env,
            allow_legacy_home_fallback=complete_launch is None,
        )
        return AgyAcpProcess(
            spec,
            env=self._legacy_env if complete_launch is None else None,
            worker_channel=channel,
            command=spec.resolved_command(("agy",)),
            complete_launch=complete_launch,
            request_timeout_seconds=self._agy_request_timeout_seconds,
            on_turn_failure_for_spec=self._turn_failure_observer,
            on_turn_completed=native_auth_observer(
                complete_launch, self._turn_completed_observer
            ),
            runtime_launch_custody=self._runtime_launch_custody,
            actor_host=self._actor_host,
        )
    def start(self, spec: HarnessLaunchSpec) -> ManagedHarnessProcess:
        process = self._start_process(spec)
        if self._process_observer is not None:
            self._process_observer(spec, process)
        return process

    def _start_process(self, spec: HarnessLaunchSpec) -> ManagedHarnessProcess:
        if spec.headless:
            declared = _headless_exec(spec.harness)
            if declared is not None and declared.level is SupportLevel.NATIVE:
                if declared.mechanism == "agent_sdk":
                    process = self._agent_sdk_factory(spec)
                    wait_ready = getattr(process, "wait_ready", None)
                    if not callable(wait_ready) or not wait_ready(
                        timeout=START_TIMEOUT_SECONDS_DEFAULT
                    ):
                        detail = getattr(process, "last_error", None)
                        process.stop()
                        raise HarnessStartError(
                            "claude", spec.command,
                            str(detail or "Claude SDK worker did not become ready"),
                        )
                    return process
                if declared.mechanism == "rpc":
                    process = self._pi_rpc_factory(spec)
                    # Same readiness gate the codex/dsh/python-worker branches
                    # apply: a pi RPC worker is not "started" until its ready
                    # probe answered.  Without this, the actor recorded
                    # success for a worker still in its reconnect loop and
                    # ``hyprial start`` returned ok while no child existed.
                    wait_ready = getattr(process, "wait_ready", None)
                    if not getattr(process, "p2_managed", False):
                        # Legacy (non-P2) pi processes keep the pre-fix return
                        # contract; only the P2 SDK bridge added a validation
                        # step whose failure used to be swallowed.
                        return process
                    if not callable(wait_ready) or not wait_ready(
                        timeout=PI_RPC_STARTUP_TIMEOUT_SECONDS_DEFAULT
                    ):
                        process.stop()
                        detail = getattr(process, "last_error", None)
                        failure = getattr(process, "last_failure", None)
                        if isinstance(failure, Exception) and getattr(failure, "permanent_start_failure", False):
                            raise failure
                        raise HarnessStartError(
                            "pi",
                            spec.command,
                            str(detail or "pi RPC worker did not become ready"),
                        )
                    return process
                if declared.mechanism == "app_server":
                    process = self._codex_app_server_factory(spec)
                    wait_ready = getattr(process, "wait_ready", None)
                    if not callable(wait_ready) or not wait_ready(
                        timeout=self._codex_startup_timeout_seconds
                    ):
                        process.stop()
                        detail = getattr(process, "last_error", None)
                        raise HarnessStartError(
                            "codex",
                            (),
                            str(detail or "app-server did not become ready"),
                        )
                    return process
                if declared.mechanism == "python_worker":
                    process = self._python_worker_factory(spec)
                    wait_ready = getattr(process, "wait_ready", None)
                    if not callable(wait_ready) or not wait_ready(
                        timeout=PYTHON_WORKER_STARTUP_TIMEOUT_SECONDS_DEFAULT
                    ):
                        process.stop()
                        detail = getattr(process, "last_error", None)
                        raise HarnessStartError(
                            spec.harness,
                            spec.command,
                            str(detail or "python worker did not become ready"),
                        )
                    return process
                if declared.mechanism == "acp":
                    process = self._acp_factory(spec)
                    wait_ready = getattr(process, "wait_ready", None)
                    if not callable(wait_ready) or not wait_ready(
                        timeout=self._agy_request_timeout_seconds
                    ):
                        process.stop()
                        detail = getattr(process, "last_error", None)
                        failure = getattr(process, "last_failure", None)
                        if isinstance(failure, Exception) and getattr(
                            failure, "permanent_start_failure", False
                        ):
                            raise failure
                        raise HarnessStartError(
                            "agy",
                            spec.command,
                            str(detail or "agy ACP worker did not become ready"),
                        )
                    return process
        raise HarnessStartError(
            spec.harness,
            (),
            "daemon only starts headless harnesses "
            "(claude, pi, codex, jev, agy); "
            "interactive sessions are started by the CLI",
        )
