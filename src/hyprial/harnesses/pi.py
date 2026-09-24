"""Pi harness connector."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from hyprial.daemon.desired_state import HarnessLaunchSpec

from .common import (
    ConnectorOptions,
    HarnessStartError,
    PtyHarnessProcess,
    without_session_arguments,
)
from .model_provider import pi_model_args
from .pi_session import pi_session_id

if TYPE_CHECKING:
    from hyprial.agents.environment import ChildEnvironmentLaunch

# The interactive attach carrier: a TUI launched with this extension
# registers itself with the daemon and relays Harness messages into the
# user's session (see pi_harness_attach.ts).  Shipped as package data next to
# this module.
PI_HARNESS_ATTACH_EXTENSION = Path(__file__).with_name("pi_harness_attach.ts")


class PiConnector:
    def __init__(
        self,
        options: ConnectorOptions | None = None,
        *,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.options = options or ConnectorOptions(("pi",))
        self._session_id_factory = session_id_factory or (lambda: str(uuid4()))

    def build_argv(self, spec: HarnessLaunchSpec) -> tuple[str, ...]:
        if spec.harness != "pi":
            raise HarnessStartError(spec.harness, (), "Pi connector mismatch")
        passthrough = without_session_arguments(spec.args)
        session_id = spec.session_ref or self._session_id_factory()
        # pi's --session-id charset gate applies to interactive pty launches
        # exactly as it does to RPC ones; translate at the boundary (#192).
        return (
            *spec.resolved_command(self.options.command),
            *pi_model_args(spec),
            *passthrough,
            "--session-id",
            pi_session_id(session_id),
        )

    def launch(
        self,
        spec: HarnessLaunchSpec,
        *,
        complete_launch: "ChildEnvironmentLaunch | None" = None,
    ) -> PtyHarnessProcess:
        argv = self.build_argv(spec)
        return PtyHarnessProcess.spawn(
            "pi",
            argv,
            cwd=spec.cwd,
            env=(
                complete_launch.environment.for_exec()
                if complete_launch is not None
                else self.options.env
            ),
            complete_environment=complete_launch is not None,
            startup_probe_seconds=self.options.startup_probe_seconds,
            stop_grace_seconds=self.options.stop_grace_seconds,
        )
