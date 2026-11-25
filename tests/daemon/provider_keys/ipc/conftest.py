"""Isolated daemon app, fake provider-key service and fake agent resolver for IPC tests."""
from __future__ import annotations

import threading
from typing import Any

import pytest

from hyprial.daemon import DaemonApplication
from hyprial.daemon.impl.provider_keys.api import AgentRef
from hyprial.kernel import DaemonRequestError, ipc_errors

SECRET = "sk-synthetic-sentinel-0123456789"
KEY_ID = "pk." + "a" * 26
AGENT_UUID = "0123456789abcdef0123456789abcdef"
AGENT = "01234567-89ab-cdef-0123-456789abcdef"
AGENT_REF = AgentRef(
    actor="agent:alice:node:worker",
    uri="agent:alice:node:worker",
    created_at_ms=1700000000000,
    entity_token=AGENT_UUID,
    agent=AGENT,
    agent_id="a." + "b" * 26,
)

KEY_VIEW = {
    "keyId": KEY_ID, "provider": "anthropic", "kind": "api-key", "label": "work",
    "owner": "casdoor-sub", "state": "active", "createdAt": 1, "updatedAt": 2,
    "dekVersion": 1, "environmentNames": ["ANTHROPIC_API_KEY"],
    "recipients": [{"recipientKeyId": "x25519:" + "0" * 32, "role": "owner-device",
                    "deviceUid": "device-1"}],
    "lastUsedAt": None,
    "grants": [{"agent": AGENT, "agentId": AGENT_REF.agent_id, "grantId": "anthropic",
                "provider": "anthropic", "state": "active"}],
}

GRANT_VIEW = {
    "agent": AGENT, "agentId": AGENT_REF.agent_id, "actor": AGENT_REF.actor,
    "grantId": "anthropic", "provider": "anthropic", "owner": "casdoor-sub",
    "keyId": KEY_ID, "state": "active", "seq": 1, "dekVersion": 1,
    "issuedAt": 10, "expiresAt": 20, "environmentNames": ["ANTHROPIC_API_KEY"],
    "recipientKeyId": "x25519:" + "0" * 32, "legacyRef": None,
}

GRANT_STATUS = {
    "grantId": "anthropic", "provider": "anthropic", "state": "ok", "code": None,
    "owner": "casdoor-sub", "keyId": KEY_ID, "seq": 1, "dekVersion": 1,
    "expiresAt": 20, "unsealable": True,
}

RESULTS = {
    "login": {"key": KEY_VIEW},
    "list_keys": {"keys": [KEY_VIEW], "invalid": [{"keyId": "pk." + "c" * 26,
                                                   "code": ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID}]},
    "logout": {"keyId": KEY_ID, "state": "deleted", "alreadyDeleted": False,
               "revokedGrants": [{"agent": AGENT, "agentId": None, "grantId": "anthropic",
                                  "provider": "anthropic"}]},
    "rotate": {"key": KEY_VIEW, "reissuedGrants": 1},
    "allow": {"grant": GRANT_VIEW, "changed": True},
    "revoke": {"grant": {**GRANT_VIEW, "state": "revoked", "keyId": KEY_ID, "dekVersion": None,
                         "issuedAt": None, "expiresAt": None, "recipientKeyId": None},
               "changed": True, "restart": {"state": "not-running"}},
    "status": {"agent": AGENT, "agentId": AGENT_REF.agent_id, "actor": AGENT_REF.actor,
               "grants": [GRANT_STATUS]},
}


class FakeService:
    """Records every call; returns canned contract results or raises a configured error."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: dict[str, Any] = {name: value for name, value in RESULTS.items()}
        self.error: BaseException | None = None

    def _record(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return self.results[name]

    def login(self, **kwargs: Any) -> Any:
        return self._record("login", **kwargs)

    def list_keys(self, **kwargs: Any) -> Any:
        return self._record("list_keys", **kwargs)

    def logout(self, **kwargs: Any) -> Any:
        return self._record("logout", **kwargs)

    def rotate(self, **kwargs: Any) -> Any:
        return self._record("rotate", **kwargs)

    def allow(self, **kwargs: Any) -> Any:
        return self._record("allow", **kwargs)

    def revoke(self, **kwargs: Any) -> Any:
        return self._record("revoke", **kwargs)

    def status(self, **kwargs: Any) -> Any:
        return self._record("status", **kwargs)


class FakeResolver:
    """Resolves every reference to AGENT_REF unless expectedAgent disagrees."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, ref: str, expected_agent: Any) -> AgentRef:
        self.calls.append((ref, expected_agent))
        if ref == "missing":
            raise DaemonRequestError(ipc_errors.AGENT_NOT_FOUND, "agent not found")
        if expected_agent is not None and expected_agent != {
            "uri": AGENT_REF.uri, "createdAtMs": AGENT_REF.created_at_ms,
        }:
            raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH, "incarnation mismatch")
        return AGENT_REF


def make_app(tmp_path) -> Any:
    """A DaemonApplication shell with no daemon, sockets or real home."""
    app = object.__new__(DaemonApplication)
    app._restore_done = threading.Event()
    app._restore_done.set()
    app._inbox = app._harnesses = app._presence = object()
    app._ipc_peer = threading.local()
    app.owner = "alice"
    app.state_dir = tmp_path / "state"
    app.hyprial_home = tmp_path / "home"
    return app


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture
def app(tmp_path, service, resolver) -> Any:
    instance = make_app(tmp_path)
    instance._provider_key_service = service
    instance._provider_key_agent_resolver = resolver
    return instance
