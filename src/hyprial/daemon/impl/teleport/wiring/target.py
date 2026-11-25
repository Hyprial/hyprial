# Decision record (file header; no inline comments by repository rule):
# - ``RegistryTargetHost`` adapts the teleport ``TargetHost`` port to this
#   daemon's agent registry, activation and lifecycle.  ``land`` and
#   ``activate`` reuse the P2 landing and ``RuntimeContextActivation`` exactly
#   as the P2 target does; ``start`` and ``stop`` go through the daemon's own
#   ``lifecycle.start`` and ``down`` entries, whose resume path already runs
#   ``require_resumable_session``, the lifecycle create saga and
#   ``strict_resume``.  They arrive as plain callables (``launch``, ``down``)
#   so the adapter can be built without a daemon; a missing one raises
#   ``TELEPORT_RUNTIME_UNSUPPORTED`` instead of pretending success.
# - ``registry`` is the agent domain's own registry port (the same view the
#   P2 landing uses), so a landed row is observed by the domain projection
#   that ``lifecycle.start`` reads; ``activation_registry`` is the raw
#   registry the activation seam needs for home receipts, as in P2; its
#   config binding is routed back through the domain view so the projection
#   sees the bound config.
# - UNSIGNED LANDING, ONE PLACE: teleport v1 has no peer authentication and
#   lands bundles without source attribution (agent teleport v1, no peer
#   authentication: KP-047 / docs/design/agent-teleport-handoff.md section 6,
#   Allen 2026-11-12).  ``_UNSIGNED_V1`` below is the only production
#   construction of an unsigned landing policy; the signature-gate guard test
#   names this file in an explicit allowlist and asserts the literal occurs
#   exactly once.
# - The landing cwd is always ``<home>/workspace`` under
#   ``<hyprial home>/agents/<name>``; the source's cwd is never honoured.
# - ``start_teleport_target`` purges staging (and landing residue) BEFORE it
#   declares the handler: a request can then never meet a half-written
#   operation from a previous daemon.  The empty private staging root is
#   created at the same time, so an idle target has a well-defined root.
"""Registry-backed teleport target host and the startup entry that binds it."""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.daemon.impl.lifecycle.primitives import RuntimeContextActivation
from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.daemon.impl.teleport.staging import StagingReceiver, staging_root
from hyprial.daemon.impl.teleport.target import (
    Landed,
    TargetRoutines,
    TeleportTargetService,
    bind_target_service,
)
from hyprial.daemon.impl.transfer.archive.envelope import (
    ensure_recipient_key,
    recipient_key_path,
)
from hyprial.daemon.impl.transfer.landing.registry import (
    LandingNameTaken,
    land_bundle_into_registry,
)
from hyprial.daemon.impl.transport import KeySpace, Registration, TransportSession
from hyprial.kernel import canonical_user_uri, secure_private_directory

DECLARED_HARNESSES = frozenset({"claude", "codex", "pi"})


def landing_root(
    hyprial_home: str | os.PathLike[str],
    state_dir: str | os.PathLike[str] | None = None,
) -> Path:
    base = Path(hyprial_home) / "state" if state_dir is None else Path(state_dir)
    return base / "teleport" / "landing"


def _ensure_private_teleport_dir(
    hyprial_home: str | os.PathLike[str],
    state_dir: str | os.PathLike[str] | None,
    target: Path,
) -> Path:
    base = Path(hyprial_home) / "state" if state_dir is None else Path(state_dir)
    for directory in (base, target.parent, target):
        secure_private_directory(directory, create=True, mode=0o700)
    return target


class _ActivationRegistry:
    """Raw registry reads plus the agent domain's own ``bind_agent_config``.

    Activation binds the landed config; doing that through the raw registry
    would change the durable row behind the agent domain's projection.
    """

    def __init__(self, domain: Any, raw: Any) -> None:
        self._domain = domain
        self._raw = raw

    def bind_agent_config(self, *args: Any, **kwargs: Any) -> Any:
        return self._domain.bind_agent_config(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class RegistryTargetHost:
    def __init__(
        self,
        *,
        node_id: str,
        owner: str,
        registry: Any,
        activation_registry: Any | None = None,
        hyprial_home: str | os.PathLike[str],
        state_dir: str | os.PathLike[str] | None = None,
        runtime_available: Callable[[str], bool],
        launch: Callable[[dict[str, Any]], Any] | None = None,
        down: Callable[[dict[str, Any]], Any] | None = None,
        environment: Mapping[str, str] | None = None,
        notice_reporter: Callable[[Any], None] | None = None,
    ) -> None:
        self.node_id = node_id
        self.owner = owner
        self.registry = registry
        self._activation_registry = (
            registry
            if activation_registry is None
            else _ActivationRegistry(registry, activation_registry)
        )
        self.hyprial_home = Path(hyprial_home)
        self.state_dir = None if state_dir is None else Path(state_dir)
        self._runtime_available = runtime_available
        self._launch = launch
        self._down = down
        self._environment = dict(os.environ if environment is None else environment)
        self._notice_reporter = notice_reporter
        self._activations: dict[tuple[str, str], RuntimeContextActivation] = {}
        self._lock = threading.Lock()

    def agent_exists(self, name: str) -> bool:
        return bool(self.registry.exists(name))

    def runtime_supported(self, harness: str) -> bool:
        return harness in DECLARED_HARNESSES and bool(self._runtime_available(harness))

    def recipient_public_key(self) -> bytes:
        return ensure_recipient_key(recipient_key_path(self.hyprial_home))[1]

    def land(self, bundle_dir: Path, *, name: str, harness: str) -> Landed:
        home_root = self.hyprial_home / "agents"
        cwd = home_root / name / "workspace"
        parent = landing_root(self.hyprial_home, self.state_dir)
        try:
            _ensure_private_teleport_dir(self.hyprial_home, self.state_dir, parent)
            land_bundle_into_registry(
                bundle_dir,
                registry=self.registry,
                home_root=home_root,
                machine_home=self.hyprial_home,
                staging=parent / uuid4().hex,
                granted_by=canonical_user_uri(self.owner),
                expected_actor=name,
                cwd=str(cwd),
                harness=harness,
            )
        except LandingNameTaken as error:
            raise TeleportError("TELEPORT_AGENT_EXISTS", str(error)) from error
        except Exception as error:
            raise TeleportError(
                "TELEPORT_LANDING_FAILED", f"{type(error).__name__}: {error}"
            ) from error
        row = self.registry.require(name)
        return Landed(
            name=name,
            uri=row.uri,
            entity_token=row.entity_token,
            home=home_root / name,
            cwd=cwd,
            harness=harness,
        )

    def undo_landing(self, landed: Landed) -> None:
        self._activations.pop((landed.name, landed.entity_token), None)
        self.registry.destroy_landing_home(
            landed.name, expected_entity_token=landed.entity_token
        )
        self._cleanup_landed_exclusions(landed)

    def _cleanup_landed_exclusions(self, landed: Landed) -> None:
        from hyprial.daemon.impl.org.network.member_agents.exclusions import (
            OrgAgentExclusionStore,
            exclusions_path,
        )
        from hyprial.identity import agent_id_for_entity_token

        path = exclusions_path(self.hyprial_home)
        if not os.path.lexists(path):
            return
        agent_id = agent_id_for_entity_token(landed.entity_token)
        OrgAgentExclusionStore(self.hyprial_home).remove_all_for_agent(agent_id)

    def activate(self, landed: Landed) -> None:
        activation = self._activation(landed)
        with self._lock:
            self._activations[(landed.name, landed.entity_token)] = activation
        activation.activate(landed.name, landed.entity_token)

    def deactivate(self, landed: Landed) -> None:
        with self._lock:
            activation = self._activations.pop((landed.name, landed.entity_token), None)
        (activation or self._activation(landed)).deactivate(
            landed.name, landed.entity_token
        )

    def start(
        self,
        landed: Landed,
        *,
        args: Sequence[str],
        session_ref: str | None,
        timeout: float,
    ) -> str | None:
        if self._launch is None:
            raise TeleportError(
                "TELEPORT_RUNTIME_UNSUPPORTED", "this host has no lifecycle start entry"
            )
        params: dict[str, Any] = {
            "provider": landed.harness,
            "name": landed.name,
            "headless": True,
            "args": list(args),
            "cwd": str(landed.cwd),
            "operationId": f"teleport-start:{uuid4().hex}",
        }
        if session_ref is not None:
            params["sessionRef"] = session_ref
        try:
            result = self._launch(params)
        except TeleportError:
            raise
        except Exception as error:
            raise TeleportError(
                "TELEPORT_STRICT_RESUME_FAILED", f"{type(error).__name__}: {error}"
            ) from error
        established = result.get("sessionRef") if isinstance(result, dict) else None
        return established if isinstance(established, str) else None

    def stop(self, landed: Landed) -> None:
        if self._down is None:
            raise TeleportError(
                "TELEPORT_RUNTIME_UNSUPPORTED", "this host has no lifecycle down entry"
            )
        self._down({"target": landed.name, "provider": landed.harness})

    def _activation(self, landed: Landed) -> RuntimeContextActivation:
        return RuntimeContextActivation(
            self._activation_registry,
            harnesses=(landed.harness,),
            tool_profile=None,
            base_environment=self._environment,
            cwd=str(landed.cwd),
            notice_reporter=self._notice_reporter,
        )


@dataclass
class TeleportTargetRuntime:
    service: TeleportTargetService
    receiver: StagingReceiver
    registration: Registration
    _closed: bool = field(default=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.registration.close()
        finally:
            target = self.service
            target.close()


def start_teleport_target(
    session: TransportSession,
    *,
    host: RegistryTargetHost | Any,
    hyprial_home: str | os.PathLike[str],
    state_dir: str | os.PathLike[str] | None = None,
    routines: TargetRoutines | None = None,
    key_space: KeySpace | None = None,
) -> TeleportTargetRuntime:
    receiver = StagingReceiver(staging_root(hyprial_home, state_dir))
    receiver.purge()
    _ensure_private_teleport_dir(hyprial_home, state_dir, receiver.root)
    shutil.rmtree(landing_root(hyprial_home, state_dir), ignore_errors=True)
    target = TeleportTargetService(
        host=host, receiver=receiver, routines=routines, key_space=key_space
    )
    try:
        registration = bind_target_service(session, target, key_space=key_space)
    except BaseException:
        target.close()
        raise
    return TeleportTargetRuntime(service=target, receiver=receiver, registration=registration)
