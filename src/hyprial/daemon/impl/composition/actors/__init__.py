# Decision record (file header; no inline comments by repository rule):
# - P7 single-host mode: the daemon process owns exactly one actor host and
#   therefore one engine. Owners still ask for a spawner by name, but every
#   name resolves to the same host; the name is custody vocabulary, not a
#   construction key. This object remains the only daemon file allowed to
#   call create_actor_host (P4 ENGINE_COMPOSITION_ROOTS).
# - The host is created lazily so composing a registry that never spawns
#   constructs no engine. Shutdown closes the registry first, so a concurrent
#   host_for cannot create a host that the shutdown pass would miss, then
#   stops the one host. A failed shutdown retains the host and its owner
#   labels so a later shutdown retries instead of reporting a vacuous success;
#   a BaseException is recorded, the host retained, and the exception re-raised.
# - drain_all drains the single engine once and reports that one result under
#   every composed owner label, keeping the existing per-owner map shape
#   without pretending each owner owns separate hardware. The caller
#   (AgentSessionDomains.close) still propagates a false aggregate.
from __future__ import annotations

import threading

from hyprial.kernel import (
    ActorEngineConfig,
    ActorHost,
    ActorSpawner,
    create_actor_host,
    GenerationScheduler,
)


def create_generation_scheduler() -> GenerationScheduler:
    """The composition root's fallback for a caller that injects none."""

    return GenerationScheduler()


class ActorHostRegistry:
    """One process-wide actor host; owner names are custody labels (P7)."""

    __slots__ = ("_closed", "_config", "_host", "_lock", "_owners")

    def __init__(self, config: ActorEngineConfig | None = None) -> None:
        self._config = config
        self._host: ActorHost | None = None
        self._owners: list[str] = []
        self._lock = threading.Lock()
        self._closed = False

    def _host_locked(self) -> ActorHost:
        if self._host is None:
            self._host = create_actor_host(self._config)
        return self._host

    def host_for(self, owner: str) -> ActorHost:
        """The process-wide host, created on first use; stable for its life."""

        if not owner.strip():
            raise ValueError("owner must not be blank")
        with self._lock:
            if self._closed:
                raise RuntimeError("actor host registry is shut down")
            host = self._host_locked()
            if owner not in self._owners:
                self._owners.append(owner)
            return host

    def spawner_for(self, owner: str) -> ActorSpawner:
        """The owner-facing capability; an ActorHost satisfies ActorSpawner."""

        return self.host_for(owner)

    def owners(self) -> tuple[str, ...]:
        """Composed owner labels in first-use order; a stopped host retires them."""

        with self._lock:
            return tuple(self._owners)

    def drain_all(self, timeout: float = 5.0) -> dict[str, bool]:
        """Drain the one engine; every composed owner sees the same result."""

        with self._lock:
            if self._closed:
                raise RuntimeError("actor host registry is shut down")
            host = self._host
            owners = tuple(self._owners)
        if host is None:
            return {}
        try:
            complete = host.drain_all(timeout).complete
        except Exception:
            complete = False
        return {owner: complete for owner in owners}

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Stop the one host; a failed host stays for a later retry."""

        with self._lock:
            self._closed = True
            host = self._host
            if host is None:
                return True
            pending: BaseException | None = None
            try:
                result = host.shutdown(timeout)
            except Exception:
                result = False
            except BaseException as exc:
                result = False
                pending = exc
            if result:
                self._host = None
                self._owners.clear()
            if pending is not None:
                raise pending
            return result
