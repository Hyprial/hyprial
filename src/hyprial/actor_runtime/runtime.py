from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping

from .contracts import (
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorSnapshot,
    ActorSpec,
    AdmissionResult,
    DrainReport,
    EventSink,
)
from .guardian import ActorGuardian
from .policies import SupervisionPolicy, freeze_policy_catalog
from .pykka_backend import create_backend
from .scheduler import GenerationScheduler


_DEFAULT_EVENT_LOGGER = logging.getLogger("hyprial.actor_runtime")


def _log_event(event: ActorEvent) -> None:
    level = (
        logging.ERROR
        if event.kind in {ActorEventKind.CHILD_FAILED, ActorEventKind.CHILD_QUARANTINED}
        else logging.INFO
    )
    _DEFAULT_EVENT_LOGGER.log(
        level,
        "actor runtime event",
        extra={
            "actor_event": {
                "kind": event.kind.value,
                "actorId": event.handle.actor_id,
                "actorName": event.handle.name,
                "generation": event.generation,
                "commandType": event.command_type,
                "code": event.code,
                "restartDelay": event.restart_delay,
            }
        },
    )


class ActorRuntime:
    """Pykka-free command surface used by HYPRIAL business modules."""

    def __init__(
        self,
        *,
        event_sink: EventSink = _log_event,
        policies: Mapping[str, SupervisionPolicy] | None = None,
        random_source: Callable[[], float] | None = None,
        scheduler: GenerationScheduler | None = None,
    ) -> None:
        self._guardian = ActorGuardian(
            policies=freeze_policy_catalog(policies),
            event_sink=event_sink,
            scheduler=scheduler or GenerationScheduler(),
            random_source=random_source or random.random,
            backend=create_backend(),
        )

    def start(self, spec: ActorSpec) -> ActorHandle:
        return self._guardian.start(spec)

    def tell(self, handle: ActorHandle, command: object) -> AdmissionResult:
        return self._guardian.tell(handle, command)

    def snapshot(self, handle: ActorHandle) -> ActorSnapshot:
        return self._guardian.snapshot(handle)

    def stop(self, handle: ActorHandle, timeout: float = 1.0) -> bool:
        return self._guardian.stop(handle, timeout)

    def reset(self, handle: ActorHandle) -> bool:
        return self._guardian.reset(handle)

    def drain(self, timeout: float = 5.0) -> DrainReport:
        return self._guardian.drain(timeout)
