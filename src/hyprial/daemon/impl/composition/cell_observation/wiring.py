"""Join existing immutable owner reads into a diagnostic cell attachment.

The lock serializes mount metadata only. No work is dispatched, no durable state
is created, and no identity or lifecycle decision is made at this boundary.
"""

import threading

from hyprial.kernel import parse_agent_uri

from hyprial.daemon.impl.cells.observability import CellSnapshot
from hyprial.daemon.impl.cells.observability.mount import CellMounts
from hyprial.daemon.impl.lifecycle.observation import LifecycleObservationOwner
from hyprial.daemon.impl.cells.observability.policy import RunPolicies
from hyprial.daemon.impl.cells.observability.projection import RunProjection
from .run_states import run_states, operation_fence


def _binding_key(binding):
    return None if binding is None else (binding.revision, binding.harness,
                                        binding.runtime, binding.session_id)


class CellObservationView:
    def __init__(self, agents, sessions, host: str, *, capacity: int = 4096, policies=None) -> None:
        self._mounts = CellMounts(host, capacity)
        self._agents, self._sessions = agents, sessions
        self._harnesses = None
        self._coordinator = None
        self._unknown = LifecycleObservationOwner(capacity=capacity)
        self._lock = threading.RLock()
        self._closed = False
        self._runs = RunProjection(capacity)
        self._policies = policies or RunPolicies.from_payload({})

    def attach_harnesses(self, projection) -> None:
        """Composition-only wiring, not a consumer operation or custody transfer."""
        with self._lock:
            self._harnesses = projection

    def attach_coordinator(self, coordinator) -> None:
        with self._lock:
            self._coordinator = coordinator

    def operation_fence(self, actor):
        return operation_fence(self, actor)

    def _identity(self, actor):
        if parse_agent_uri(actor) is None:
            return None, None
        entity = self._agents.read_agent(actor)
        if entity is None or entity.uri != actor:
            return None, None
        return entity, self._agents.read_binding(actor)

    def _select(self, entity, binding):
        if binding is None:
            return None, "none"
        if binding.runtime == "interactive":
            session = self._sessions.read_session(entity.uri)
            if (session is not None and session.session_ref is not None
                    and session.session_ref == binding.session_id):
                return (self._sessions.lifecycle_observations,
                        entity.uri, session.session_ref), "external"
            return None, "external"
        if binding.runtime == "headless" and self._harnesses is not None:
            key = f"{binding.harness}:{entity.actor}"
            source = self._harnesses.read_observation_source(
                key, entity.entity_token, binding.revision,
            )
            if source is not None:
                return (self._harnesses.lifecycle_observations, key, source), "managed"
        return None, "managed" if binding.runtime == "headless" else "none"

    def _refresh(self, actor):
        if self._closed:
            return None
        for _ in range(3):
            entity, binding = self._identity(actor)
            if entity is None:
                self._mounts.remove(actor)
                return None
            source, custody = self._select(entity, binding)
            # Independent cache reads cannot define a cross-owner transaction.
            # Revalidate the incarnation and binding before and after mounting;
            # a concurrent change retries instead of adopting stale source facts.
            current, current_binding = self._identity(actor)
            if (current is None or current.entity_token != entity.entity_token
                    or _binding_key(current_binding) != _binding_key(binding)):
                continue
            mount = self._mounts.get(actor, entity.entity_token)
            revision = None if binding is None else binding.revision
            if source is None:
                key = mount.target.key
                epoch = f"{mount.diagnostic_incarnation}:{revision}:unbound"
                self._unknown.publish(key, epoch, state="unknown", cause="source-unavailable",
                                      available=False)
                source = self._unknown, key, epoch
            mount.attach(*source, binding_revision=revision)
            lifecycle = mount.owner.reader.snapshot(mount.target)
            current, current_binding = self._identity(actor)
            if (current is not None and current.entity_token == entity.entity_token
                    and _binding_key(current_binding) == _binding_key(binding)):
                return CellSnapshot(actor, mount.diagnostic_incarnation, revision,
                                    mount.target, custody, lifecycle)
            mount.detach()
        # No stable identity/binding cut was available. Never retain an old mount.
        self._mounts.remove(actor)
        return None

    def run_states(self, interactive, blocks, *, now_ms, monotonic_ms):
        with self._lock:
            if self._closed:
                return {}
            return run_states(self, interactive, blocks, now_ms=now_ms, monotonic_ms=monotonic_ms)

    def snapshot(self, actor: str) -> CellSnapshot | None:
        with self._lock:
            return self._refresh(actor)

    def observe(self, actor: str, *, after_revision=None, epoch=None,
                limit=128, wait_ms=0, observer_id=None):
        with self._lock:
            cell = self._refresh(actor)
            if cell is None:
                return None
            mount = self._mounts.rows[actor]
            return mount.owner.reader.observe(cell.target, after_revision=after_revision,
                                               epoch=epoch, limit=limit, wait_ms=wait_ms,
                                               observer_id=observer_id)

    def open(self, actor: str):
        with self._lock:
            cell = self._refresh(actor)
            if cell is None:
                return None
            return self._mounts.rows[actor].owner.reader.open(cell.target)

    def cancel(self, observer_id: str) -> None:
        with self._lock:
            for mount in self._mounts.rows.values():
                if mount.owner is not None:
                    mount.owner.reader.cancel(observer_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._mounts.close()
            self._unknown.close()
