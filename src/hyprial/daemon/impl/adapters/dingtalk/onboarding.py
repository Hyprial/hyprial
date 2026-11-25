from __future__ import annotations

import importlib.util
import hashlib
import time
from collections.abc import Mapping
from typing import Protocol

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    CapabilityFactV1,
    CapabilitySnapshot,
    FrozenJsonMap,
    FrozenJsonV1,
    OnboardingPreflight,
    OnboardingAttempt,
    VerifiedPlatformIdentity,
)

from .errors import DingTalkProtocolError
from .runtime import DingTalkCredentials


class DingTalkCredentialAuthority(Protocol):
    async def resolve(self, credential_ref: str) -> DingTalkCredentials: ...


class DingTalkOnboarding:
    def __init__(
        self,
        authority: DingTalkCredentialAuthority,
        *,
        probe_transport: object | None = None,
        dependency_available: bool | None = None,
        now_ms: object | None = None,
    ) -> None:
        self._authority = authority
        self._probe_transport = probe_transport
        self._dependency_available = dependency_available
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._authenticated: dict[str, DingTalkCredentials] = {}

    async def inspect(self, config: FrozenJsonV1) -> OnboardingPreflight:
        values = (
            dict(config)
            if isinstance(config, FrozenJsonMap)
            else config
            if isinstance(config, Mapping)
            else None
        )
        if values is None or values.get("variant") != "stream":
            return OnboardingPreflight("unsupported", ("dingtalk-variant-unsupported",))
        if not isinstance(values.get("credentialRef"), str):
            return OnboardingPreflight("blocked", ("dingtalk-credential-ref-required",))
        available = self._dependency_available
        if available is None:
            available = importlib.util.find_spec("dingtalk_stream") is not None
        if not available:
            return OnboardingPreflight("blocked", ("dingtalk-stream-dependency-missing",))
        return OnboardingPreflight("ready", ())

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity:
        if not isinstance(attempt, OnboardingAttempt):
            raise DingTalkProtocolError("onboarding-attempt-required")
        credentials = await self._authority.resolve(attempt.credential_ref)
        if not isinstance(credentials, DingTalkCredentials):
            raise DingTalkProtocolError("credential-authority-type-mismatch")
        proof = hashlib.sha256(
            f"{credentials.instance_id}\0{credentials.generation}\0{credentials.client_id}".encode()
        ).hexdigest()
        identity = VerifiedPlatformIdentity(
            f"dingtalk:{credentials.client_id}",
            credentials.client_id,
            f"credential-proof:{proof}",
        )
        self._authenticated[identity.proof_ref] = credentials
        return identity

    def _credentials_for(self, identity: VerifiedPlatformIdentity) -> DingTalkCredentials:
        if not isinstance(identity, VerifiedPlatformIdentity):
            raise DingTalkProtocolError("verified-identity-required")
        credentials = self._authenticated.get(identity.proof_ref)
        if credentials is None or identity.stable_account_id != credentials.client_id:
            raise DingTalkProtocolError("identity-proof-unrecognized")
        return credentials

    async def fingerprint(self, identity: VerifiedPlatformIdentity) -> AdapterInstanceRef:
        credentials = self._credentials_for(identity)
        return AdapterInstanceRef(
            credentials.instance_id,
            "dingtalk",
            "stream",
            "dingtalk",
            credentials.organization_id,
            credentials.client_id,
            credentials.generation,
        )

    async def revoke(self, identity: VerifiedPlatformIdentity) -> None:
        if not isinstance(identity, VerifiedPlatformIdentity):
            raise DingTalkProtocolError("verified-identity-required")
        self._authenticated.pop(identity.proof_ref, None)

    async def probe_capabilities(
        self, identity: VerifiedPlatformIdentity
    ) -> CapabilitySnapshot:
        credentials = self._credentials_for(identity)
        probe = getattr(self._probe_transport, "probe_capabilities", None)
        if not callable(probe):
            raise DingTalkProtocolError("capability-probe-unavailable")
        result = await probe(credentials)
        if not isinstance(result, dict) or result.get("authenticated") is not True:
            raise DingTalkProtocolError("capability-probe-unauthenticated")
        raw_facts = result.get("facts")
        if not isinstance(raw_facts, dict) or not raw_facts:
            raise DingTalkProtocolError("capability-probe-invalid")
        now = self._now_ms()
        facts = tuple(
            CapabilityFactV1(
                name,
                str(value.get("support", "unknown")),  # type: ignore[arg-type]
                str(value.get("permission", "unknown")),  # type: ignore[arg-type]
                value.get("limits", {}),
                value.get("reasonCode") if isinstance(value.get("reasonCode"), str) else None,
            )
            for name, value in sorted(raw_facts.items())
            if isinstance(name, str) and isinstance(value, dict)
        )
        if not facts:
            raise DingTalkProtocolError("capability-probe-invalid")
        return CapabilitySnapshot(
            credentials.instance_id,
            credentials.generation,
            f"dingtalk-stream/{hashlib.sha256(repr(facts).encode()).hexdigest()[:16]}",
            "dingtalk-stream-authenticated-probe",
            now,
            now + 300_000,
            tuple(sorted(facts, key=lambda fact: fact.name.encode())),
        )


__all__ = ["DingTalkCredentialAuthority", "DingTalkOnboarding"]
