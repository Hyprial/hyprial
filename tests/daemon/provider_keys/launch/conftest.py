"""Shared fixtures for owner-key launch injection (contract freeze r3 G2a).

Every home, registry and state directory lives under ``tmp_path``; values are
synthetic sentinels; the owner-key service is a recording fake injected through
``app._provider_key_service`` (the seam every slice honors).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.provider_keys.api import AgentRef, LaunchSecret
from hyprial.identity import AgentSecretStore, SecretResolver, SecretSource
from hyprial.identity.impl.agents import AgentRegistry

LEGACY_VALUE = "LEGACY-SENTINEL-0123456789"
OWNER_VALUE = "OWNER-SENTINEL-9876543210"
PRIVATE_VALUE = "PRIVATE-SENTINEL-5555"


class FakeService:
    """Records every call; returns the configured launch answer or raises it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []
        self.answer: object = (frozenset(), ())
        self.grant_states: list[dict] = []
        self.slot_files: set[str] | None = None

    def launch_secrets(self, agent: AgentRef, *, now_ms: int):
        self.calls.append(("launch_secrets", agent, now_ms))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def grants_for(self, agent: AgentRef):
        self.calls.append(("grants_for", agent, None))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return list(self.grant_states)

    def slot_names(self, agent: AgentRef):
        self.calls.append(("slot_names", agent, None))
        if isinstance(self.answer, BaseException):
            raise self.answer
        if self.slot_files is not None:
            return frozenset(self.slot_files)
        return frozenset(state["grantId"] for state in self.grant_states)


class Harness:
    """One isolated home with a real registry and an app-shaped seam holder."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir(mode=0o700)
        self.state = tmp_path / "state"
        self.state.mkdir(mode=0o700)
        self.registry = AgentRegistry(
            self.state / "agents.sqlite3",
            owner="owner-a",
            machine="pk-node",
            hyprial_home=self.home,
        )
        self.agent = self.registry.create("worker")
        self.service = FakeService()
        self.resolved: list[tuple[str, object]] = []
        self.app = SimpleNamespace(
            hyprial_home=self.home,
            state_dir=self.state,
            owner="owner-a",
            _provider_key_service=self.service,
            _provider_key_agent_resolver=self._resolve,
        )

    def _resolve(self, ref: str, expected: object) -> AgentRef:
        self.resolved.append((ref, expected))
        agent = self.registry.require(ref)
        return AgentRef(
            actor=agent.actor,
            uri=agent.uri,
            created_at_ms=0,
            entity_token=agent.entity_token,
            agent="00000000-0000-4000-8000-000000000000",
            agent_id=agent.agent_id,
        )

    def legacy_user_grant(self, grant_id: str, entry_id: str, env: str, *, write: bool = True) -> None:
        if write:
            SecretResolver(self.home, self.registry).write_user_provider(entry_id, {"key": LEGACY_VALUE})
        self.registry.grant_secret(
            "worker", grant_id=grant_id, source=SecretSource.USER_PROVIDER,
            entry_id=entry_id, field_name="key", environment_names=(env,), revision=1,
        )

    def legacy_private_grant(self, grant_id: str, env: str) -> None:
        AgentSecretStore(Path(self.registry.home_receipt("worker").path)).set(
            service=f"{grant_id}.json",
            field="KEY",
            value=PRIVATE_VALUE,
            actor="worker",
            via="daemon",
            at_ms=1,
        )
        self.registry.grant_secret(
            "worker", grant_id=grant_id, source=SecretSource.AGENT_PRIVATE,
            entry_id=f"{grant_id}.json", field_name="KEY", environment_names=(env,), revision=1,
        )

    def held(self, grant_id: str, env: str, *, seq: int = 4, value: str = OWNER_VALUE) -> LaunchSecret:
        return LaunchSecret(grant_id=grant_id, provider=grant_id, seq=seq,
                            environment_names=(env,), value=value)

    def grant_state(self, grant_id: str, env: str, *, state: str = "active") -> dict:
        return {"grantId": grant_id, "provider": grant_id, "state": state, "environmentNames": [env]}

    def foreign_hosted(self, name: str = "guest") -> object:
        return self.registry.create_transfer_hosted(name, pinned_owner="owner-b")

    def close(self) -> None:
        self.registry.close()


@pytest.fixture
def harness(tmp_path: Path):
    instance = Harness(tmp_path)
    yield instance
    instance.close()


@pytest.fixture
def gate(harness: Harness):
    """Write the home's real ``providerKeys.authority`` setting; an exception
    value writes an unknown authority, which the gate must refuse."""

    def pin(value: object) -> None:
        authority = "damaged" if isinstance(value, BaseException) else value
        path = harness.home / "settings.json"
        path.write_text(json.dumps({"providerKeys": {"authority": authority}}), encoding="utf-8")

    return pin
