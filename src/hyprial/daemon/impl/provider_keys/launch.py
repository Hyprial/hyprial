# Decision record (contract freeze r3 §3.3, §7.3, §8.2, §8.4, §8.5, §8.7, §9.5; impl spec slice C1)
# - The gate is read on every launch, never cached. "legacy" returns None before any
#   service, agent or store read, so the legacy launch stays byte-identical (§8.7).
# - The gate is read only through the single reader storage.gate.authority (no home,
#   absent file or section is legacy; any other value, a non-object, corrupt JSON or
#   the literal provider_keys spelling refuses with PROVIDER_STORE_UNAVAILABLE, which
#   this path reports as a launch refusal, never as legacy; no environment override).
# - An agent received by transfer for another owner is not
#   owner-local: §3.3 lets the owner grant only agents on the owner's own device, so
#   the owner-key authority can never hold a slot for it. Both entry points return
#   None for it (legacy semantics) before any service or agent-resolution read,
#   instead of letting the local-agent refusal fail every such launch and export.
#   Locality is context.is_local, the same predicate resolve_agent applies. Every
#   other failure stays fail closed.
# - Shadowing and withholding use slot_ids: every slot the authority has a file or a
#   mark for, verified or not (the concrete service's slot_names), so a slot whose
#   record fails verification still hides the legacy value of its id (§8.2, §8.4).
#   An injected service without slot_names contributes its verified slots only.
# - "owner-keys" asks the service for the slots it holds and their unsealed values;
#   the identity composition drops legacy grants of those slots (single authority).
# - Every refusal on this path is re-raised as the identity SecretResolutionError so
#   the legacy start surface only ever sees AGENT_SECRET_RESOLUTION_FAILED; the
#   original code is kept as the category and no value, message, cause or context
#   is carried: the refusal is raised after the except block ends.
# - Interactive acquire (agent.runtime-launch.acquire) is unchanged: the legacy path
#   never injected user-provider grants there either, so parity means no injection.
"""Launch-time owner-key injection for managed workers (G2a)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from hyprial.identity import SecretResolutionError
from hyprial.kernel import ipc_errors
from .api import AgentRef, LaunchSecret
from .storage import gate

PHASE = "owner-key"


@dataclass(frozen=True)
class HeldSecret:
    """The identity-neutral shape of one unsealed slot: revision is the grant-state seq."""

    grant_id: str
    entry_id: str
    revision: int
    environment_names: tuple[str, ...]
    entity_token: str
    value: str = field(default="", repr=False, compare=False)

    @classmethod
    def of(cls, secret: LaunchSecret, agent: AgentRef) -> "HeldSecret":
        return cls(grant_id=secret.grant_id, entry_id=secret.provider, revision=secret.seq,
                   environment_names=tuple(secret.environment_names),
                   entity_token=agent.entity_token, value=secret.value)


@dataclass(frozen=True)
class HeldSlot:
    """One verified grant-state record of the agent, any state; never carries a value."""

    grant_id: str
    entry_id: str
    state: str
    environment_names: tuple[str, ...]

    @classmethod
    def of(cls, payload: Any) -> "HeldSlot":
        grant_id, entry_id, state = (payload[key] for key in ("grantId", "provider", "state"))
        names = payload["environmentNames"]
        if not all(isinstance(value, str) for value in (grant_id, entry_id, state)):
            raise TypeError("grant state fields must be strings")
        if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
            raise TypeError("grant state environment names must be a list of strings")
        return cls(grant_id=grant_id, entry_id=entry_id, state=state, environment_names=tuple(names))


LaunchAnswer = tuple[frozenset[str], tuple[HeldSecret, ...]]


def _refusal(error: BaseException, agent_name: str) -> SecretResolutionError:
    code = getattr(error, "code", None)
    category = code if isinstance(code, str) and code else ipc_errors.DAEMON_ERROR
    return SecretResolutionError(category, agent_name, PHASE)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _owner_agent(app: Any, registry: Any, agent_name: str) -> Any:
    """The service and agent snapshot for a local agent under "owner-keys"; else None."""

    from .context import is_local, resolve_agent, service_for

    if gate.authority(app.hyprial_home) != gate.OWNER_KEYS:
        return None
    if not is_local(app, registry.require(agent_name)):
        return None
    return service_for(app), resolve_agent(app, agent_name, None)


def launch_resolver(app: Any, *, registry: Any,
                    clock: Callable[[], int] = _now_ms) -> Callable[[str], LaunchAnswer | None]:
    """Return the per-launch resolver the worker composition calls with an agent name."""

    def resolve(agent_name: str) -> LaunchAnswer | None:
        refusal = None
        try:
            owner = _owner_agent(app, registry, agent_name)
            if owner is None:
                return None
            service, agent = owner
            slots, secrets = service.launch_secrets(agent, now_ms=clock())
            return frozenset(slots), tuple(HeldSecret.of(secret, agent) for secret in secrets)
        except Exception as error:
            refusal = _refusal(error, agent_name)
        raise refusal from None

    return resolve


def slot_ids(service: Any, agent: AgentRef, states: Any = None) -> frozenset[str]:
    """Every slot id the authority holds a record for, verified or not."""
    ids = {state["grantId"] for state in (service.grants_for(agent) if states is None else states)}
    lister = getattr(service, "slot_names", None)
    if lister is not None:
        ids.update(lister(agent))
    return frozenset(ids)


def export_slots(app: Any, agent_name: str, *,
                 registry: Any) -> tuple[tuple[HeldSlot, ...], frozenset[str]] | None:
    """(verified slots, every held slot id) of this agent; None when the legacy rules apply."""

    refusal = None
    try:
        owner = _owner_agent(app, registry, agent_name)
        if owner is None:
            return None
        service, agent = owner
        states = service.grants_for(agent)
        return tuple(HeldSlot.of(payload) for payload in states), slot_ids(service, agent, states)
    except Exception as error:
        refusal = _refusal(error, agent_name)
    raise refusal from None


def held_slots(app: Any, agent_name: str, *, registry: Any) -> tuple[HeldSlot, ...] | None:
    """Verified grant-state records of this agent, any state; None when the legacy rules apply."""
    exported = export_slots(app, agent_name, registry=registry)
    return None if exported is None else exported[0]
