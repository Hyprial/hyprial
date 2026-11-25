# Decision record (contract freeze r3 §4, §5; impl spec slice B1)
# - OwnerKeyService is the concrete ProviderKeyService over one OwnerContext. It is
#   rebuilt by context.service_for on every request (the gate is read every time);
#   cross-request state (locks, high-water marks) lives outside the instance.
# - The service only shapes results and routes to ops.keys and ops.grants; it holds
#   no secret material. revoke also reports restart (§5.2) for the IPC result.
# - slot_names is not part of the frozen Protocol: it lists every grant file of an
#   incarnation, verified or not, for callers that must fail closed.
"""Concrete owner-device provider-key service."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .api import AgentRef, JsonObject, LaunchSecret
from .ops import grants, keys
from .ops.records import OwnerContext


class OwnerKeyService:
    """ProviderKeyService over one owner context (contract §4.3, §5.2)."""

    def __init__(self, ctx: OwnerContext) -> None:
        self._ctx = ctx

    def __repr__(self) -> str:
        return "OwnerKeyService()"

    def login(self, *, provider: str, kind: str, secret: str, label: str | None = None,
              environment_names: Sequence[str] | None = None,
              legacy_ref: Mapping[str, str] | None = None) -> JsonObject:
        meta = keys.login(self._ctx, provider=provider, kind=kind, secret=secret, label=label,
                          environment_names=environment_names, legacy_ref=legacy_ref)
        return {"key": keys.view(self._ctx, meta)}

    def list_keys(self, *, provider: str | None = None, include_deleted: bool = False) -> JsonObject:
        return keys.list_keys(self._ctx, provider=provider, include_deleted=include_deleted)

    def logout(self, *, key_id: str) -> JsonObject:
        revoked, already = keys.logout(self._ctx, key_id=key_id)
        return {"keyId": key_id, "state": "deleted", "revokedGrants": revoked, "alreadyDeleted": already}

    def rotate(self, *, key_id: str, replace_secret: str | None = None) -> JsonObject:
        meta, reissued = keys.rotate(self._ctx, key_id=key_id, replace_secret=replace_secret)
        return {"key": keys.view(self._ctx, meta), "reissuedGrants": reissued}

    def allow(self, *, agent: AgentRef, provider: str, key_id: str | None = None,
              environment_names: Sequence[str] | None = None, replace: bool = False,
              grant_id: str | None = None, legacy_ref: Mapping[str, Any] | None = None) -> JsonObject:
        grant, changed = grants.allow(self._ctx, agent, provider=provider, key_id=key_id,
                                      environment_names=environment_names, replace=replace,
                                      grant_id=grant_id, legacy_ref=legacy_ref)
        return {"grant": grant, "changed": changed}

    def put_awaiting(self, *, agent: AgentRef, grant_id: str, provider: str,
                     environment_names: Sequence[str], legacy_ref: Mapping[str, Any]) -> JsonObject:
        grant, changed = grants.put_awaiting(self._ctx, agent, grant_id=grant_id, provider=provider,
                                             environment_names=environment_names, legacy_ref=legacy_ref)
        return {"grant": grant, "changed": changed}

    def revoke(self, *, agent: AgentRef, grant_id: str) -> JsonObject:
        grant, changed, restart = grants.revoke(self._ctx, agent, grant_id=grant_id)
        return {"grant": grant, "changed": changed, "restart": {"state": restart}}

    def status(self, *, agent: AgentRef, provider: str | None = None) -> JsonObject:
        return {"agent": agent.agent, "agentId": agent.agent_id, "actor": agent.actor,
                "grants": grants.status(self._ctx, agent, provider=provider)}

    def grants_for(self, agent: AgentRef) -> list[JsonObject]:
        return grants.grants_for(self._ctx, agent)

    def slot_names(self, agent: AgentRef) -> frozenset[str]:
        return grants.slot_names(self._ctx, agent)

    def find_key_by_legacy_ref(self, *, entry_id: str, field_name: str) -> JsonObject | None:
        return keys.find_by_legacy_ref(self._ctx, entry_id=entry_id, field_name=field_name)

    def launch_secrets(self, agent: AgentRef, *, now_ms: int) -> tuple[frozenset[str], tuple[LaunchSecret, ...]]:
        return grants.launch_secrets(self._ctx, agent, now_ms=now_ms)
