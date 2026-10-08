"""Agent home migration IPC surface and its coordinator bindings."""

from __future__ import annotations

from __future__ import annotations
import os
from typing import Any, TYPE_CHECKING
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)


class _AgentMigrationMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _agent_migration_stopped(
        self, agent_uri: str, entity_token: str
    ) -> bool | None:
        """Prove one current local incarnation has no live daemon binding."""

        agent = self._agent_registry.get(agent_uri)
        if agent is None or agent.entity_token != entity_token:
            return None
        running = self._agent_liveness.verdict(agent_uri)
        if running is not None:
            return not running
        # For a local registry identity, an absent binding is the daemon's
        # durable post-`down`/never-started state.  A retained binding with no
        # verdict remains unknown and therefore fails closed in the
        # coordinator.
        return True if self._agent_liveness.binding(agent_uri) is None else None

    def _agent_migration_bindings(self, support: Any) -> Any:
        from hyprial.identity import (
            DEFAULT_AGENT_TOOL_PROFILE,
            AgentRuntimeMigrationBindings,
        )

        harnesses = tuple(dict.fromkeys(key.harness for key in support))
        return AgentRuntimeMigrationBindings(
            self._agent_registry,
            harnesses=harnesses,
            tool_profile=DEFAULT_AGENT_TOOL_PROFILE,
            base_environment=os.environ,
            containerized=False,
        )

    def _agent_migration_coordinator(self) -> Any:
        from hyprial.identity import AgentMigrationCoordinator
        from hyprial.identity import load_packaged_support_matrix

        return AgentMigrationCoordinator(
            self._agent_registry,
            load_packaged_support_matrix(),
            liveness_probe=self._agent_migration_stopped,
        )

    def _handle_agent_migration(self, method: str, params: JsonObject) -> Any:
        from hyprial.identity import (
            MigrationAuthorizationWindow,
            MigrationPreflightManifest,
        )

        requested = _required_string(params.get("agent"), "agent")
        try:
            actor = self.agents.require(requested).actor
            manifest = None
            migration_id = None
            authorization_window = None
            if method == "agent.migrate.preflight":
                manifest = MigrationPreflightManifest.from_json(params.get("manifest"))
            elif method in {"agent.migrate.execute", "agent.migrate.rollback"}:
                migration_id = _required_string(params.get("migrationId"), "migrationId")
                if method == "agent.migrate.rollback":
                    authorization_window = MigrationAuthorizationWindow.from_json(
                        params.get("authorizationWindow")
                    )
            else:
                raise DaemonRequestError(
                    ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}"
                )
            return self._agent_session_domains.agent.run_migration(
                method, actor, manifest=manifest, migration_id=migration_id,
                authorization_window=authorization_window,
            )
        except (TypeError, ValueError, OSError) as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
