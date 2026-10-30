"""Claude harness connector."""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from hyprial.kernel import HarnessLaunchSpec, Logger

from hyprial.daemon.impl.harnesses.protocol.common  import (
    ConnectorOptions,
    HarnessStartError,
    PtyHarnessProcess,
    without_session_arguments,
)
from hyprial.daemon.impl.harnesses.model_provider  import claude_provider_environment

if TYPE_CHECKING:
    from hyprial.identity import ChildEnvironmentLaunch


class ClaudeConnector:
    """Launch Claude with connector-owned, per-spawn session identity."""

    def __init__(
        self,
        options: ConnectorOptions | None = None,
        *,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.options = options or ConnectorOptions(("claude",))
        self._session_id_factory = session_id_factory or (lambda: str(uuid4()))

    def build_argv(self, spec: HarnessLaunchSpec) -> tuple[str, ...]:
        if spec.harness != "claude":
            raise HarnessStartError(spec.harness, (), "Claude connector mismatch")
        passthrough = without_session_arguments(spec.args)
        model_args = (
            ("--model", spec.model)
            if spec.model is not None
            and spec.model_provider in {None, "anthropic"}
            else ()
        )
        return (
            *spec.resolved_command(self.options.command),
            *model_args,
            *passthrough,
            "--session-id",
            self._session_id_factory(),
        )

    def launch(
        self,
        spec: HarnessLaunchSpec,
        *,
        complete_launch: "ChildEnvironmentLaunch | None" = None,
    ) -> PtyHarnessProcess:
        from hyprial.identity import (
            whitelist_replacement_environment,
        )

        base = (
            complete_launch.environment.for_exec()
            if complete_launch is not None
            else whitelist_replacement_environment(os.environ, self.options.env or {})
        )
        provider_environment = claude_provider_environment(
            spec,
            base,
            allow_legacy_home_fallback=(
                complete_launch is None or complete_launch.runtime_context is None
            ),
        )
        argv = self.build_argv(spec)
        environment = (
            {**base, **provider_environment}
            if complete_launch is not None
            else whitelist_replacement_environment(
                os.environ, self.options.env or {}, provider_environment
            )
        )
        environment = self._admit_credentials(spec, complete_launch, environment)
        return PtyHarnessProcess.spawn(
            "claude",
            argv,
            cwd=spec.cwd,
            env=environment or None,
            complete_environment=complete_launch is not None,
            startup_probe_seconds=self.options.startup_probe_seconds,
            stop_grace_seconds=self.options.stop_grace_seconds,
        )

    @staticmethod
    def _admit_credentials(
        spec: HarnessLaunchSpec,
        complete_launch: "ChildEnvironmentLaunch | None",
        environment: dict[str, str],
    ) -> dict[str, str]:
        from hyprial.daemon.impl.pac.actors.marker import pac_worker_marker
        from .runtime import ClaudeRuntimeError, validate_legacy_claude_auth

        guarded = (
            (complete_launch is not None and complete_launch.managed_creation)
            or pac_worker_marker(spec.nickname) is not None
        )
        source = "missing" if guarded else "ambient"
        try:
            if guarded and complete_launch is not None:
                # Share SDK admission, including P1 and P2 root/auth checks.
                from hyprial.daemon.impl.harnesses.agent_sdk import _validated_claude_launch_environment

                _, base, provider = _validated_claude_launch_environment(
                    spec, env=None, worker_channel=None, complete_launch=complete_launch,
                )
                environment = {**base, **provider}
                source = "env-grant" if any(environment.get(name) for name in (
                    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                )) else "agent-native"
            else:
                candidate = dict(environment)
                try:
                    source = validate_legacy_claude_auth(candidate)
                except ClaudeRuntimeError as error:
                    if guarded or (complete_launch is not None
                                   and error.code != "CLAUDE_CREDENTIAL_MISSING"):
                        raise
                    # Resident PTY admission remains compatible. Do not inspect
                    # ambient credentials or infer that an ambient login exists.
                else:
                    if complete_launch is None or complete_launch.runtime_context is None:
                        environment = candidate
            return environment
        finally:
            state_dir = environment.get("HARNESS_STATE_DIR")
            if state_dir:
                Logger.worker(Path(state_dir), runtime="claude", name=spec.name).info(
                    "worker.claude.credential_source", credentialSource=source,
                    actor=complete_launch.actor if complete_launch is not None else spec.name,
                    runtimeMode=("p2" if complete_launch is not None
                                 and complete_launch.runtime_context is not None else "legacy"),
                )
