"""Daemon-compatible harness launcher composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

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
from .dsh import DshHarnessProcess
from .pi import PiConnector
from .pi_rpc import PiRpcProcess
from .worker_channel import WorkerChannel

WorkerChannelFactory = Callable[[HarnessLaunchSpec], WorkerChannel | None]

# Mechanisms whose HEADLESS_EXEC entry gives a managed streaming process
# (Agent SDK, pi RPC mode, and Codex app-server).
_STREAMING_MECHANISMS = frozenset({"agent_sdk", "rpc", "app_server", "dsh_api"})


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
        worker_channel_factory: WorkerChannelFactory | None = None,
    ) -> None:
        configured = commands or {}
        self._claude_command_overridden = "claude" in configured
        self._pi_command_overridden = "pi" in configured
        self._codex_command_overridden = "codex" in configured
        self._codex_startup_timeout_seconds = codex_startup_timeout_seconds
        # Absent a factory the worker channel is None, so the managed worker
        # keeps its pre-fix ambient configuration; only a daemon that wires the
        # factory gives each worker its own canonical Harness identity.
        self._worker_channel_factory = worker_channel_factory
        self._agent_sdk_factory = agent_sdk_factory or (
            lambda spec: ClaudeAgentSdkProcess(
                spec, env=env, worker_channel=self._worker_channel_for(spec)
            )
        )
        self._pi_rpc_factory = pi_rpc_factory or (
            lambda spec: PiRpcProcess(
                spec,
                env=env,
                worker_channel=self._worker_channel_for(spec),
                command=spec.resolved_command(("pi",)),
            )
        )
        self._codex_app_server_factory = codex_app_server_factory or (
            lambda spec: CodexAppServerProcess(
                spec,
                env=env,
                worker_channel=self._worker_channel_for(spec),
                command=spec.resolved_command(("codex",)),
            )
        )
        self._dsh_api_factory = dsh_api_factory or (
            lambda spec: DshHarnessProcess(
                spec, worker_channel=self._worker_channel_for(spec)
            )
        )

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
                    return self._dsh_api_factory(spec)
        try:
            connector = self._connectors[spec.harness]
        except KeyError as error:
            raise HarnessStartError(
                spec.harness,
                (),
                "unsupported harness; expected claude, pi, codex, or dsh",
            ) from error
        return connector.launch(spec)
