"""Daemon-compatible harness launcher composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from hyprial.daemon.api import ManagedHarnessProcess
from hyprial.daemon.desired_state import HarnessLaunchSpec

from .agent_sdk import ClaudeAgentSdkProcess
from .capabilities import Capability, CapabilitySupport, SupportLevel, support
from .claude import ClaudeConnector
from .codex import (
    APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT,
    CodexAppServerProcess,
    CodexConnector,
)
from .common import ConnectorOptions, HarnessStartError
from .dsh import DSH_STARTUP_TIMEOUT_SECONDS_DEFAULT, DshHarnessProcess
from .pi import PiConnector
from .pi_rpc import PiRpcProcess, TurnFailureSpecObserver
from .python_worker import PythonHarnessProcess
from hyprial.agents.environment import ChildEnvironmentLaunch
from .worker_channel import WorkerChannel

WorkerChannelFactory = Callable[[HarnessLaunchSpec], WorkerChannel | None]

#: P1b B1: (spec, worker channel) → the complete child environment launch.
#: Returning ``None`` keeps the carrier's legacy env handling; raising fails
#: the worker start loudly (no ambient fallback exists by construction).
ChildEnvironmentFactory = Callable[
    [HarnessLaunchSpec, WorkerChannel], "ChildEnvironmentLaunch | None"
]

# Mechanisms whose HEADLESS_EXEC entry gives a managed streaming process
# (Agent SDK, pi RPC mode, and Codex app-server).
_STREAMING_MECHANISMS = frozenset(
    {"agent_sdk", "rpc", "app_server", "dsh_api", "python_worker"}
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
    """Concrete implementation of ``hyprial.daemon.api.HarnessLauncher``."""

    def __init__(
        self,
        *,
        commands: Mapping[str, Sequence[str]] | None = None,
        env: Mapping[str, str] | None = None,
        startup_probe_seconds: float = 0.08,
        stop_grace_seconds: float = 1.0,
        codex_startup_timeout_seconds: float = (
            APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT
        ),
        dsh_startup_timeout_seconds: float = DSH_STARTUP_TIMEOUT_SECONDS_DEFAULT,
        agent_sdk_factory: Callable[[HarnessLaunchSpec], ManagedHarnessProcess]
        | None = None,
        pi_rpc_factory: Callable[[HarnessLaunchSpec], ManagedHarnessProcess]
        | None = None,
        codex_app_server_factory: Callable[
            [HarnessLaunchSpec], ManagedHarnessProcess
        ]
        | None = None,
        dsh_api_factory: Callable[[HarnessLaunchSpec], ManagedHarnessProcess]
        | None = None,
        python_worker_factory: Callable[[HarnessLaunchSpec], ManagedHarnessProcess]
        | None = None,
        worker_channel_factory: WorkerChannelFactory | None = None,
        child_environment_factory: "ChildEnvironmentFactory | None" = None,
        state_dir: Path | None = None,
        turn_failure_observer: TurnFailureSpecObserver | None = None,
    ) -> None:
        configured = commands or {}
        self._claude_command_overridden = "claude" in configured
        self._pi_command_overridden = "pi" in configured
        self._codex_command_overridden = "codex" in configured
        self._codex_startup_timeout_seconds = codex_startup_timeout_seconds
        self._dsh_startup_timeout_seconds = dsh_startup_timeout_seconds
        # Absent a factory the worker channel is None, so the managed worker
        # keeps its pre-fix ambient configuration; only a daemon that wires the
        # factory gives each worker its own canonical Harness identity.
        self._worker_channel_factory = worker_channel_factory
        # P1b B1: per-worker complete child environment (resolver →
        # LaunchSpec private values → build_complete_child_environment).
        # Applied to the pi carrier only in this slice; the remaining spawn
        # sites keep today's behaviour until B2 collects them.
        self._child_environment_factory = child_environment_factory
        self._legacy_env = env
        # provider_auth's coordinator, when the daemon wires one; passed
        # straight through to every PiRpcProcess.
        self._turn_failure_observer = turn_failure_observer
        self._agent_sdk_factory = agent_sdk_factory or self._make_agent_sdk_process
        self._pi_rpc_factory = pi_rpc_factory or self._make_pi_process
        self._codex_app_server_factory = (
            codex_app_server_factory or self._make_codex_process
        )
        self._dsh_api_factory = dsh_api_factory or (
            lambda spec: DshHarnessProcess(
                spec,
                env=env,
                state_dir=state_dir,
                worker_channel=self._worker_channel_for(spec),
            )
        )
        self._python_worker_factory = python_worker_factory or self._make_python_process

        def options(harness: str, default: str) -> ConnectorOptions:
            return ConnectorOptions(
                tuple(configured.get(harness, (default,))),
                env=env,
                startup_probe_seconds=startup_probe_seconds,
                stop_grace_seconds=stop_grace_seconds,
            )

        self._connectors = {
            "claude": ClaudeConnector(options("claude", "claude")),
            "pi": PiConnector(options("pi", "pi")),
            "codex": CodexConnector(options("codex", "codex")),
        }

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

        if self._child_environment_factory is None or channel is None:
            return None
        return self._child_environment_factory(spec, channel)

    def _make_agent_sdk_process(
        self, spec: HarnessLaunchSpec
    ) -> "ClaudeAgentSdkProcess":
        """ONE channel mint, ONE environment from that channel (B2)."""

        channel = self._worker_channel_for(spec)
        return ClaudeAgentSdkProcess(
            spec,
            env=self._legacy_env,
            worker_channel=channel,
            complete_launch=self._complete_launch_for(spec, channel),
            on_turn_failure_for_spec=self._turn_failure_observer,
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
            on_turn_failure_for_spec=self._turn_failure_observer,
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
        )

    def _command_overridden(self, harness: str) -> bool:
        return {
            "claude": self._claude_command_overridden,
            "pi": self._pi_command_overridden,
            "codex": self._codex_command_overridden,
        }.get(harness, False)

    def start(self, spec: HarnessLaunchSpec) -> ManagedHarnessProcess:
        # Dispatch on the declared HEADLESS_EXEC mechanism; a command
        # override still falls back to the PTY connector as before.
        if spec.headless and not self._command_overridden(spec.harness):
            declared = _headless_exec(spec.harness)
            if declared is not None and declared.level is SupportLevel.NATIVE:
                if declared.mechanism == "agent_sdk":
                    return self._agent_sdk_factory(spec)
                if declared.mechanism == "rpc":
                    return self._pi_rpc_factory(spec)
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
                if declared.mechanism == "dsh_api":
                    process = self._dsh_api_factory(spec)
                    # Mirror the app_server branch: the actor captures the
                    # child's PID + birth identity right after start() returns,
                    # so readiness must be established first (otherwise the
                    # OrphanProcessRegistry records ``unidentified`` and stop
                    # cannot fence PID reuse).
                    wait_ready = getattr(process, "wait_ready", None)
                    if not callable(wait_ready) or not wait_ready(
                        timeout=self._dsh_startup_timeout_seconds
                    ):
                        process.stop()
                        detail = getattr(process, "last_error", None)
                        raise HarnessStartError(
                            "dsh",
                            (),
                            str(detail or "dsh did not become ready"),
                        )
                    return process
                if declared.mechanism == "python_worker":
                    process = self._python_worker_factory(spec)
                    wait_ready = getattr(process, "wait_ready", None)
                    if not callable(wait_ready) or not wait_ready(timeout=10.0):
                        process.stop()
                        detail = getattr(process, "last_error", None)
                        raise HarnessStartError(
                            spec.harness,
                            spec.command,
                            str(detail or "python worker did not become ready"),
                        )
                    return process
        try:
            connector = self._connectors[spec.harness]
        except KeyError as error:
            raise HarnessStartError(
                spec.harness,
                (),
                "unsupported harness; expected claude, pi, codex, dsh, or jev",
            ) from error
        return connector.launch(spec)
