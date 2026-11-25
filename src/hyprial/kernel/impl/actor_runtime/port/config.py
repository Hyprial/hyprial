# Decision record (file header; no inline comments by repository rule):
# - The port configuration is deliberately engine-neutral and small: policy
#   data, one engine-level event sink, the jitter source, and CPU accounting.
#   The pool retry cadence is per ActorPoolSpec. P6 extends create_engine(config)
#   for the native backend without changing this public shape.
# - A missing event_sink keeps the structural log sink the ABI adapter owns.
# - Backend selection is read from HYPRIAL_ACTOR_BACKEND at each engine
#   construction, never cached. An absent variable means the shipped default
#   (native); an empty or unknown value is a configuration error, not a reason
#   to fall back. The native loader reports a missing or foreign extension with
#   ActorBackendUnavailableError, which also must never fall back.
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from hyprial.kernel.impl.actor_runtime.contracts import ActorEvent
from hyprial.kernel.impl.actor_runtime.policies import SupervisionPolicy
from hyprial.kernel.impl.facts.cost_counters import (
    RuntimeCpuCounters,
    runtime_cpu_counters,
)

ACTOR_BACKEND_ENV = "HYPRIAL_ACTOR_BACKEND"
DEFAULT_ACTOR_BACKEND = "native"
ACTOR_BACKENDS = ("reference-abi", "native")


class UnknownActorBackendError(ValueError):
    """HYPRIAL_ACTOR_BACKEND names a backend that does not exist."""


class ActorBackendUnavailableError(RuntimeError):
    """The selected backend cannot be loaded; no other backend is substituted."""


def selected_actor_backend(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the configured backend name, defaulting only when unset."""

    source = os.environ if environ is None else environ
    raw = source.get(ACTOR_BACKEND_ENV)
    if raw is None:
        return DEFAULT_ACTOR_BACKEND
    if raw not in ACTOR_BACKENDS:
        choices = ", ".join(ACTOR_BACKENDS)
        raise UnknownActorBackendError(
            f"{ACTOR_BACKEND_ENV}={raw!r} is not one of {choices}"
        )
    return raw


@dataclass(frozen=True, slots=True)
class ActorEngineConfig:
    """Construction data for one actor engine port instance."""

    policies: Mapping[str, SupervisionPolicy] | None = None
    event_sink: Callable[[ActorEvent], None] | None = None
    random_source: Callable[[], float] | None = None
    cpu_accounting: RuntimeCpuCounters = runtime_cpu_counters
