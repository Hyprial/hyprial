# Decision record (contract freeze r3 §2, §4.1, §8.3, §8.7, §9.1; impl spec slice B1)
# - Handlers, launch and the legacy adapter resolve the service and agent snapshots
#   only through service_for and resolve_agent. The injected app attributes
#   (_provider_key_service, _provider_key_agent_resolver) stay supported and win.
# - service_for reads the gate (storage.gate.authority) on every call: anything but
#   "owner-keys" refuses with PROVIDER_STORE_UNAVAILABLE before any other read.
# - Owner identity is the Casdoor sub and device uid of the local U1 signer store,
#   which user_key.ipc.compose derives from settings.json userId and the device
#   record (the U1 rule); records, the anchor lookup and the store resolver all use
#   that sub. Missing identity is PROVIDER_STORE_UNAVAILABLE.
# - The owner store comes from the OwnerSpaceResolver port
#   (_provider_key_space_resolver, else LocalSpaceResolver under hyprial_home,
#   else UNAVAILABLE_RESOLVER when hyprial_home is absent).
# - The owner anchor adapts a U2 verifier (_provider_key_owner_key_port, absent in
#   production) to ProviderKeys.KeyLookup: verified only when the binding's sub is
#   the record owner; absent stays absent; any source failure is unavailable.
# - Signing goes through the U1 Signer.signing_key gates; the default signer is
#   disabled, so production refuses with USER_KEY_UNBOUND.
# - resolve_agent takes one registry snapshot: a local agent by name, agent URI or
#   a.<id> (a.<id> scans the local list). Foreign-owner agents (including any
#   legacy pre-2026-11-21 host-invited row still on disk) are not local and
#   resolve like a missing agent. With expectedAgent, a missing agent or a
#   different (uri, createdAtMs) is AGENT_INCARNATION_MISMATCH; without it a missing
#   agent is AGENT_NOT_FOUND.
# - is_local is the one locality predicate (owner equals app.owner)
#   shared with the launch and legacy entry points, which give a registry agent that
#   is not local the legacy semantics instead of letting this refusal fail it.
"""Owner context seam: the provider-key service and agent incarnation resolution."""
from __future__ import annotations

import base64
import binascii
import re
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any, Mapping

from hyprial.identity import EncryptionKey, ProviderKeys, UserKey, machine_fingerprint
from hyprial.kernel import DaemonRequestError, ipc_errors
from .api import AgentRef, ProviderKeyService
from .ops.records import HighWater, OwnerContext
from .service import OwnerKeyService
from .storage import UNAVAILABLE_RESOLVER, LocalSpaceResolver, authority

OWNER_KEYS = "owner-keys"
_AGENT_ID = re.compile(r"a\.[a-z2-7]{26}")
_HIGH_WATER_GUARD = threading.Lock()


def _unavailable() -> ProviderKeys.Error:
    return ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)


def service_for(app: Any) -> ProviderKeyService:
    """Return the owner-local service, or refuse with PROVIDER_STORE_UNAVAILABLE."""
    service = getattr(app, "_provider_key_service", None)
    if service is not None:
        return service
    if authority(getattr(app, "hyprial_home", None)) != OWNER_KEYS:
        raise _unavailable()
    return OwnerKeyService(owner_context(app))


def owner_context(app: Any) -> OwnerContext:
    """Compose the owner context of this device behind an open gate."""
    from hyprial.daemon.impl.user_key.ipc import signer_for

    signer = signer_for(app)
    sub, device_uid = signer.store.casdoor_sub, signer.store.device_uid
    if not _text(sub) or not _text(device_uid):
        raise _unavailable()
    resolver = getattr(app, "_provider_key_space_resolver", None)
    if resolver is None:
        home = getattr(app, "hyprial_home", None)
        resolver = LocalSpaceResolver(Path(home)) if home else UNAVAILABLE_RESOLVER
    store = resolver.store_for(sub)
    try:
        sealed = EncryptionKey.Store(state_dir=Path(app.state_dir), kind="user", casdoor_sub=sub,
                                     device_uid=device_uid, fingerprint=machine_fingerprint())
    except UserKey.Error:
        raise _unavailable() from None
    clock = getattr(app, "_provider_key_clock", None) or _now_ms
    return OwnerContext(
        owner=sub, device_uid=device_uid, store=store,
        anchor=OwnerAnchor(getattr(app, "_provider_key_owner_key_port", None), app.owner),
        signing_key=signer.signing_key, sealed=sealed, clock=clock, state_dir=Path(app.state_dir),
        high_water=_high_water(app, sub), running=partial(_running, app),
        local_agents=partial(_local_agents, app), current=partial(_current, app),
    )


def _text(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and "\0" not in value


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _high_water(app: Any, sub: str) -> HighWater:
    """The durable rollback marks of this owner, loaded once per app and owner."""
    state_dir = Path(app.state_dir)
    with _HIGH_WATER_GUARD:
        cache = getattr(app, "_provider_key_high_water", None)
        if not isinstance(cache, dict):
            cache = {}
            app._provider_key_high_water = cache
        marks = cache.get((str(state_dir), sub))
        if not isinstance(marks, HighWater):
            marks = HighWater(state_dir, sub)
            cache[(str(state_dir), sub)] = marks
        return marks


class OwnerAnchor:
    """ProviderKeys.OwnerKeyPort over a U2 verifier keyed by the hyprial owner name."""

    def __init__(self, port: Any, hyprial_owner: str) -> None:
        self._port = port
        self._hyprial_owner = hyprial_owner

    def __repr__(self) -> str:
        return f"OwnerAnchor(configured={self._port is not None})"

    def lookup(self, owner: str, key_id: str) -> ProviderKeys.KeyLookup:
        if self._port is None:
            return ProviderKeys.KeyLookup("unavailable")
        try:
            result = self._port.lookup(self._hyprial_owner, key_id)
        except Exception:
            return ProviderKeys.KeyLookup("unavailable")
        state = getattr(result, "state", None)
        if state == "absent":
            return ProviderKeys.KeyLookup("absent")
        if state != "verified":
            return ProviderKeys.KeyLookup("unavailable")
        if isinstance(result, ProviderKeys.KeyLookup):
            key = result.key
            if isinstance(key, ProviderKeys.OwnerKey) and key.owner == owner and key.key_id == key_id:
                return result
            return ProviderKeys.KeyLookup("absent")
        if getattr(result, "sub", None) != owner:
            return ProviderKeys.KeyLookup("absent")
        try:
            raw = base64.b64decode(result.public_key, validate=True)
        except (binascii.Error, ValueError, TypeError):
            return ProviderKeys.KeyLookup("unavailable")
        if len(raw) != 32:
            return ProviderKeys.KeyLookup("unavailable")
        return ProviderKeys.KeyLookup("verified", ProviderKeys.OwnerKey(owner=owner, key_id=key_id, public_key=raw))


def is_local(app: Any, agent: Any) -> bool:
    """True for a registry agent this owner's authority can hold slots for (§3.3)."""
    return (agent is not None
            and getattr(agent, "owner", None) == getattr(app, "owner", None))


def _lookup(app: Any, ref: str) -> Any:
    if _AGENT_ID.fullmatch(ref):
        return next((agent for agent in app.agents.list() if agent.agent_id == ref), None)
    try:
        return app.agents.get(ref)
    except Exception:
        return None


def non_local_agent(app: Any, ref: str) -> Any:
    """The registry agent ref names when it exists but is not owner-local, else None."""
    agent = _lookup(app, ref) if isinstance(ref, str) and ref else None
    return agent if agent is not None and not is_local(app, agent) else None


def resolve_agent(app: Any, ref: str, expected_agent: Mapping[str, Any] | None = None) -> AgentRef:
    """Resolve one local agent incarnation snapshot (contract §8.3)."""
    resolver = getattr(app, "_provider_key_agent_resolver", None)
    if resolver is not None:
        return resolver(ref, expected_agent)
    agent = _lookup(app, ref) if isinstance(ref, str) and ref else None
    if not is_local(app, agent):
        agent = None
    if expected_agent is not None and (
            agent is None or agent.uri != expected_agent.get("uri")
            or agent.created_at_ms != expected_agent.get("createdAtMs")):
        raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH,
                                 "target agent incarnation does not match expectedAgent")
    if agent is None:
        raise DaemonRequestError(ipc_errors.AGENT_NOT_FOUND, "no local agent matches the reference")
    return AgentRef(actor=agent.actor, uri=agent.uri, created_at_ms=agent.created_at_ms,
                    entity_token=agent.entity_token, agent=ProviderKeys.agent_uuid(agent.entity_token),
                    agent_id=agent.agent_id)


def _running(app: Any, ref: AgentRef) -> bool | None:
    """True/None unless liveness observed this agent not running."""
    liveness = getattr(app, "_agent_liveness", None)
    if liveness is None:
        return None
    try:
        verdict = liveness.verdict(ref.uri)
        if verdict is None and liveness.binding(ref.uri) is None:
            return False
        return verdict
    except Exception:
        return None


def _local_agents(app: Any) -> Mapping[str, str]:
    """Canonical agent UUID -> agentId for every local agent."""
    try:
        return {ProviderKeys.agent_uuid(agent.entity_token): agent.agent_id
                for agent in app.agents.list() if is_local(app, agent)}
    except Exception:
        return {}


def _current(app: Any, ref: AgentRef) -> bool:
    """The registry still holds the incarnation this request resolved."""
    try:
        agent = app.agents.get(ref.actor)
    except Exception:
        return False
    return agent is not None and agent.entity_token == ref.entity_token
