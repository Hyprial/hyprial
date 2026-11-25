# Decision record (file header; no inline comments by repository rule):
# - ``source_service(app)`` is the only entry the request handler uses.  It
#   builds one ``AgentTeleportService`` per daemon application and keeps it:
#   the per-agent lock lives in that object, so a second service would let
#   two teleports of one agent run side by side.
# - The host adapter runs the operations of ``source_ops`` for admission, the
#   stop and the non-strict source restart, and ``LifecycleOperation.remove``
#   for the source cleanup.  ``LifecycleOperation.remove`` ends in ``registry.destroy``,
#   which revokes the home receipt and synchronously deletes the home tree
#   (``identity/impl/agents/registry/_records.py`` destroy -> ``cleanup_home``),
#   so no separate home destruction is called; ``remove`` still verifies that
#   the row and the home directory are gone and fails loudly if not.
# - The stopped spec is kept per agent between ``quiesce`` and ``restart`` or
#   ``remove``: after the stop the desired-state entry is gone, so a restart
#   cannot re-resolve it.
# - ``describe`` force-syncs the harness session refs before it reads the
#   launch spec, so the ref sent in ``receive`` is the freshest one; a stopped
#   agent (no managed harness) and a TUI session are described as such and
#   refused by the preflight, not by this adapter.
# - Heavy modules (identity errors excepted) are imported inside methods so the
#   request catalog can import this module without loading the composition.
# - Never exercised against real harness binaries (none on the dev machine):
#   see docs/design/agent-teleport-protocol.md §8.
"""Wire the teleport source ports onto the daemon application."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.daemon.impl.teleport.wiring import source_ops
from hyprial.daemon.impl.teleport.source import (
    AgentTeleportService,
    ExportedStream,
    RoutineExport,
    RoutineRef,
    SourceAgent,
    TeleportLink,
    TeleportRunError,
)
from hyprial.identity import AgentError
from hyprial.identity import AgentHomeError
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec, harden_private_path

SERVICE_ATTRIBUTE = "_teleport_source_service"
WORKDIR_NAME = "teleport-source"
_CREATE_LOCK = threading.Lock()


def _unsupported(message: str) -> TeleportError:
    return TeleportError("TELEPORT_RUNTIME_UNSUPPORTED", message)


class AppSourceHost:
    """``SourceHost`` over the daemon application's registry and lifecycle."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._stopped: dict[str, Any] = {}

    @property
    def node_id(self) -> str:
        return str(self._app.node_id)

    @property
    def owner(self) -> str:
        return str(self._app.owner)

    def describe(self, agent: str) -> SourceAgent:
        app = self._app
        try:
            record = app.agents.get(agent)
        except AgentError as error:
            raise LookupError(agent) from error
        if record is None:
            raise LookupError(agent)
        if app._harnesses is None:
            raise _unsupported("the harness runtime is not running on this daemon")
        app._harnesses.reconcile_session_refs()
        try:
            target = source_ops.resolve_source_target(
                app.desired_state.load(),
                name=record.actor,
                local_name=app.agents.local_actor,
            )
        except source_ops.AdmissionRefusal as error:
            raise _unsupported(
                f"agent {record.actor!r} runs no managed headless worker on this "
                f"node ({error}); start it first"
            ) from error
        spec = target.spec
        if spec is not None and spec.containerized:
            raise _unsupported(
                f"agent {record.actor!r} runs in a container runtime, "
                "which a teleport does not move"
            )
        return SourceAgent(
            name=record.actor,
            uri=record.uri,
            harness=target.harness,
            headless=spec is not None and bool(spec.headless),
            args=() if spec is None else tuple(spec.args),
            session_ref=None if spec is None else spec.session_ref,
            cwd=None if spec is None or not spec.cwd else Path(spec.cwd),
            home=self._home(record.actor),
            adapter_bindings=self._adapter_bindings(record),
        )

    def _adapter_bindings(self, record: Any) -> tuple[str, ...] | None:
        from hyprial.daemon.impl.composition.cells import (
            agent_adapter_bindings_or_unknown,
        )

        return agent_adapter_bindings_or_unknown(self._app, record)

    def _home(self, actor: str) -> Path | None:
        try:
            receipt = self._app._agent_registry.home_receipt(actor)
        except AgentHomeError as error:
            if error.category == "not-provisioned":
                return None
            raise _unsupported(f"the home of {actor!r} is unusable: {error}") from error
        return Path(receipt.path)

    def require_resumable(self, agent: SourceAgent) -> None:
        if agent.session_ref is None:
            return
        app = self._app
        try:
            spec = source_ops.desired_spec(app, agent.name, agent.harness)
            app._require_resumable_session(spec, agent.session_ref)
        except DaemonRequestError as error:
            raise _unsupported(str(error)) from error

    def quiesce(self, agent: SourceAgent) -> bool:
        app = self._app
        self._stopped[agent.name] = source_ops.desired_spec(
            app, agent.name, agent.harness
        ).to_json()
        spec, stopped = source_ops.quiesce(
            app, agent.name, agent.harness, f"teleport-quiesce:{uuid4().hex}"
        )
        self._stopped[agent.name] = spec
        return stopped

    def restart(self, agent: SourceAgent) -> None:
        spec = self._stopped.get(agent.name)
        if spec is None:
            raise RuntimeError("no stopped launch spec was recorded for this agent")
        source_ops.restart(self._app, spec, f"teleport-resume:{uuid4().hex}")
        self._stopped.pop(agent.name, None)

    def export_stream(
        self, agent: SourceAgent, *, recipient_public_key: bytes, workdir: Path
    ) -> ExportedStream:
        from hyprial.daemon.impl.teleport.wiring.source_export import (
            export_agent_stream,
        )

        spec = self._stopped.get(agent.name)
        if spec is None:
            raise RuntimeError("the agent was not stopped before its export")
        return export_agent_stream(
            self._app,
            agent,
            spec,
            recipient_public_key=recipient_public_key,
            workdir=workdir,
        )

    def remove(self, agent: SourceAgent) -> None:
        from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation

        app = self._app
        app._run_lifecycle_operation(
            LifecycleOperation.remove(
                f"teleport-remove:{uuid4().hex}",
                app._lifecycle_spec(HarnessLaunchSpec(agent.harness, agent.name, True)),
            )
        )
        if app.agents.get(agent.name) is not None:
            raise RuntimeError(f"agent {agent.name!r} is still registered")
        if agent.home is not None and Path(agent.home).exists():
            raise RuntimeError(f"agent home {agent.home} was not deleted")
        self._stopped.pop(agent.name, None)


class AppSourceRoutines:
    """``SourceRoutines`` over the daemon's routine service."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def _service(self) -> Any:
        service = self._app._routine_service
        if service is None:
            raise RuntimeError("the routine runtime is not available on this daemon")
        return service

    def executed_by(self, agent_uri: str) -> list[RoutineRef]:
        if self._app._routine_service is None:
            return []
        listed = self._service().list_executed_by(agent=agent_uri)["routines"]
        return [RoutineRef(item["name"], bool(item["enabled"])) for item in listed]

    def export(self, name: str) -> RoutineExport:
        row = self._service().export(name=name)
        return RoutineExport(row["name"], row["yaml"], bool(row["enabled"]))

    def pause(self, name: str) -> None:
        self._service().pause(name=name)

    def resume(self, name: str) -> None:
        self._service().resume(name=name)

    def wait_drained(self, names: Sequence[str], timeout: float) -> list[str]:
        return self._service().wait_in_flight_drained(
            names=names, timeout_seconds=timeout
        )

    def remove(self, name: str) -> None:
        self._service().remove(name=name)


def _prepare_workdir_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    harden_private_path(root, mode=0o700)
    for leftover in root.iterdir():
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)


def source_service(app: Any) -> AgentTeleportService:
    """The application's one teleport source service, built on first use."""

    with _CREATE_LOCK:
        service = getattr(app, SERVICE_ATTRIBUTE, None)
        if service is not None:
            return service
        session = app._transport
        if session is None:
            raise TeleportRunError(
                "TELEPORT_WRONG_TARGET",
                "this daemon has no transport session, so no other node can be reached",
            )
        root = Path(app.state_dir) / WORKDIR_NAME
        _prepare_workdir_root(root)
        service = AgentTeleportService(
            AppSourceHost(app),
            AppSourceRoutines(app),
            TeleportLink(session),
            workdir_root=root,
            new_operation_id=lambda: f"teleport-{uuid4().hex}",
        )
        setattr(app, SERVICE_ATTRIBUTE, service)
        return service
