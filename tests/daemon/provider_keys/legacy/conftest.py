"""Fakes for the legacy agent.secret.* adapter: an in-memory record service and agent registry."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.provider_keys.api import AgentRef
from hyprial.identity import ProviderKeys, SecretSource
from hyprial.kernel import DaemonRequestError, ipc_errors

OWNER = "casdoor-sub-owner"
SENTINEL = "sk-legacy-sentinel-value-000"


def agent_ref(actor: str, token: str | None = None, created_at_ms: int = 1_700_000_000_000) -> AgentRef:
    token = token or uuid.uuid4().hex
    return AgentRef(
        actor=actor,
        uri=f"hyprial://node/{actor}",
        created_at_ms=created_at_ms,
        entity_token=token,
        agent=str(uuid.UUID(hex=token)),
        agent_id=f"a.{token[:26]}",
    )


class FakeService:
    """Records-only service: every answer is derived from the stored signed-record payloads."""

    def __init__(self, records: dict[tuple[str, str], dict[str, Any]] | None = None,
                 keys: list[dict[str, Any]] | None = None) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = records if records is not None else {}
        self.keys: list[dict[str, Any]] = keys if keys is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, BaseException] = {}

    def _enter(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        if name in self.fail:
            raise self.fail[name]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _write(self, ref: AgentRef, *, grant_id: str, provider: str, state: str, key_id: str | None,
               environment_names: Any, legacy_ref: Any) -> dict[str, Any]:
        previous = self.records.get((ref.agent, grant_id))
        record = {
            "type": "provider-grant-state", "v": 1, "owner": OWNER, "agent": ref.agent,
            "grantId": grant_id, "provider": provider, "state": state,
            "seq": 1 if previous is None else previous["seq"] + 1,
            "keyId": key_id, "grant": {"payload": {}, "sealedBy": {}} if state == "active" else None,
            "environmentNames": list(environment_names or []),
            "legacyRef": None if legacy_ref is None else dict(legacy_ref),
            "updatedAt": 1,
        }
        self.records[(ref.agent, grant_id)] = record
        return {"grant": self.view(ref, record), "changed": True}

    def view(self, ref: AgentRef, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": record["agent"], "agentId": ref.agent_id, "actor": ref.actor,
            "grantId": record["grantId"], "provider": record["provider"], "owner": record["owner"],
            "keyId": record["keyId"], "state": record["state"], "seq": record["seq"],
            "dekVersion": None, "issuedAt": None, "expiresAt": None,
            "environmentNames": list(record["environmentNames"]), "recipientKeyId": None,
            "legacyRef": copy.deepcopy(record["legacyRef"]),
        }

    def login(self, *, provider: str, kind: str, secret: str, label: str | None = None,
              environment_names: Any = None, legacy_ref: Any = None) -> dict[str, Any]:
        self._enter("login", provider=provider, kind=kind, label=label,
                    environment_names=environment_names, legacy_ref=legacy_ref,
                    secret_length=len(secret))
        key = {"keyId": f"pk.{'a' * 25}{len(self.keys)}", "provider": provider, "state": "active",
               "legacyRef": None if legacy_ref is None else dict(legacy_ref)}
        self.keys.append(key)
        return {"key": dict(key)}

    def rotate(self, *, key_id: str, replace_secret: str | None = None) -> dict[str, Any]:
        self._enter("rotate", key_id=key_id, replaced=replace_secret is not None,
                    secret_length=len(replace_secret or ""))
        return {"key": next(dict(key) for key in self.keys if key["keyId"] == key_id), "reissuedGrants": 0}

    def find_key_by_legacy_ref(self, *, entry_id: str, field_name: str) -> dict[str, Any] | None:
        self._enter("find_key_by_legacy_ref", entry_id=entry_id, field_name=field_name)
        for key in self.keys:
            if key["state"] == "active" and key["legacyRef"] == {"entryId": entry_id, "fieldName": field_name}:
                return dict(key)
        return None

    def allow(self, *, agent: AgentRef, provider: str, key_id: str | None = None,
              environment_names: Any = None, replace: bool = False, grant_id: str | None = None,
              legacy_ref: Any = None) -> dict[str, Any]:
        self._enter("allow", agent=agent, provider=provider, key_id=key_id,
                    environment_names=environment_names, replace=replace, grant_id=grant_id,
                    legacy_ref=legacy_ref)
        return self._write(agent, grant_id=grant_id or provider, provider=provider, state="active",
                           key_id=key_id, environment_names=environment_names, legacy_ref=legacy_ref)

    def put_awaiting(self, *, agent: AgentRef, grant_id: str, provider: str, environment_names: Any,
                     legacy_ref: Any) -> dict[str, Any]:
        self._enter("put_awaiting", agent=agent, grant_id=grant_id, provider=provider,
                    environment_names=environment_names, legacy_ref=legacy_ref)
        return self._write(agent, grant_id=grant_id, provider=provider, state="awaiting-key",
                           key_id=None, environment_names=environment_names, legacy_ref=legacy_ref)

    def revoke(self, *, agent: AgentRef, grant_id: str) -> dict[str, Any]:
        self._enter("revoke", agent=agent, grant_id=grant_id)
        record = self.records.get((agent.agent, grant_id))
        if record is None:
            raise ProviderKeys.Error(ipc_errors.PROVIDER_GRANT_MISSING)
        if record["state"] == "revoked":
            return {"grant": self.view(agent, record), "changed": False}
        result = self._write(agent, grant_id=grant_id, provider=record["provider"], state="revoked",
                             key_id=record["keyId"], environment_names=record["environmentNames"],
                             legacy_ref=record["legacyRef"])
        return result

    def grants_for(self, agent: AgentRef) -> list[dict[str, Any]]:
        self._enter("grants_for", agent=agent)
        return [copy.deepcopy(record) for (owner_agent, _), record in sorted(self.records.items())
                if owner_agent == agent.agent]


@dataclass
class FakeRegistry:
    """Name -> incarnation snapshots; ``changes`` swaps an incarnation after N resolutions."""

    refs: dict[str, AgentRef] = field(default_factory=dict)
    resolutions: list[tuple[str, Any]] = field(default_factory=list)
    changes: dict[int, tuple[str, AgentRef]] = field(default_factory=dict)
    fail: BaseException | None = None

    def add(self, actor: str, token: str | None = None) -> AgentRef:
        ref = agent_ref(actor, token)
        self.refs[actor] = ref
        return ref

    def __call__(self, ref: str, expected_agent: Any) -> AgentRef:
        self.resolutions.append((ref, expected_agent))
        if len(self.resolutions) in self.changes:
            actor, replacement = self.changes[len(self.resolutions)]
            self.refs[actor] = replacement
        if self.fail is not None:
            raise self.fail
        target = self.refs.get(ref)
        if expected_agent is not None and (
            target is None or target.uri != expected_agent["uri"]
            or target.created_at_ms != expected_agent["createdAtMs"]
        ):
            raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH, "target agent incarnation changed")
        if target is None:
            raise DaemonRequestError(ipc_errors.AGENT_NOT_FOUND, f"no agent named {ref!r}")
        return target


class FakeInventory:
    """Legacy ``agent_secret_grants`` rows as the actor read port returns them."""

    def __init__(self, registry: FakeRegistry) -> None:
        self.registry = registry
        self.rows: list[SimpleNamespace] = []
        self.reads: list[Any] = []

    def add(self, actor: str, grant_id: str, source: SecretSource = SecretSource.AGENT_PRIVATE, *,
            entry_id: str = "entry", field_name: str | None = "field", names: tuple[str, ...] = ("NAME",),
            revision: int = 1, entity_token: str | None = None) -> None:
        self.rows.append(SimpleNamespace(
            actor=actor, entity_token=entity_token or self.registry.refs[actor].entity_token,
            grant_id=grant_id, source=source, entry_id=entry_id, field_name=field_name,
            environment_names=names, revision=revision, owner_actor=None, owner_entity_token=None,
        ))

    def read_secret_inventory(self, actor: str | None = None, *, expected_uri=None,
                              expected_created_at_ms=None) -> tuple[SimpleNamespace, ...]:
        self.reads.append(actor)
        if expected_uri is not None:
            target = self.registry.refs.get(actor)
            if target is None or (target.uri, target.created_at_ms) != (expected_uri, expected_created_at_ms):
                raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH, "target changed")
        if actor is not None and actor not in self.registry.refs:
            raise DaemonRequestError(ipc_errors.AGENT_NOT_FOUND, f"no agent named {actor!r}")
        rows = [row for row in self.rows if actor is None or row.actor == actor]
        return tuple(sorted(rows, key=lambda row: (row.actor, row.grant_id)))


class FakeAgents:
    def __init__(self, registry: FakeRegistry) -> None:
        self.registry = registry

    def list(self) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(actor=name) for name in sorted(self.registry.refs))


def fake_app(service: FakeService | None, registry: FakeRegistry, inventory: FakeInventory) -> SimpleNamespace:
    app = SimpleNamespace(
        agents=FakeAgents(registry),
        _agent_session_domains=SimpleNamespace(agent=inventory),
        _provider_key_agent_resolver=registry,
    )
    if service is not None:
        app._provider_key_service = service
    return app


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def inventory(registry: FakeRegistry) -> FakeInventory:
    return FakeInventory(registry)


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def app(service: FakeService, registry: FakeRegistry, inventory: FakeInventory) -> SimpleNamespace:
    return fake_app(service, registry, inventory)


@pytest.fixture
def make_app(registry: FakeRegistry, inventory: FakeInventory):
    """Build a fake app around the shared registry; ``None`` leaves the authority uninjected."""

    def build(service: FakeService | None) -> SimpleNamespace:
        return fake_app(service, registry, inventory)

    return build


@pytest.fixture
def new_service():
    return FakeService


@pytest.fixture
def new_ref():
    return agent_ref
