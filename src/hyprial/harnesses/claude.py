"""Claude harness connector."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from hyprial.daemon.desired_state import HarnessLaunchSpec

from .common import (
    ConnectorOptions,
    HarnessStartError,
    PtyHarnessProcess,
    without_session_arguments,
)
from .model_provider import claude_provider_environment

if TYPE_CHECKING:
    from hyprial.agents.environment import ChildEnvironmentLaunch


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
        from hyprial.agents.environment import (
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
        return PtyHarnessProcess.spawn(
            "claude",
            argv,
            cwd=spec.cwd,
            env=environment or None,
            complete_environment=complete_launch is not None,
            startup_probe_seconds=self.options.startup_probe_seconds,
            stop_grace_seconds=self.options.stop_grace_seconds,
        )
