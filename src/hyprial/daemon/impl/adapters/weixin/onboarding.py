from __future__ import annotations

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
    OnboardingAttempt,
    OnboardingPreflight,
    VerifiedPlatformIdentity,
)

from .errors import WeixinProtocolError
from .runtime import WeixinCredentials


class WeixinCredentialAuthority(Protocol):
    async def resolve(self, credential_ref: str) -> WeixinCredentials: ...


class WeixinOnboarding:
    def __init__(
        self,
        authority: WeixinCredentialAuthority,
        *,
        login_transport: object | None = None,
        now_ms: object | None = None,
    ) -> None:
        self._authority = authority
        self._login_transport = login_transport
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._authenticated: dict[str, WeixinCredentials] = {}

    async def inspect(self, config: FrozenJsonV1) -> OnboardingPreflight:
        values = (
            dict(config)
            if isinstance(config, FrozenJsonMap)
            else config
            if isinstance(config, Mapping)
            else None
        )
        if values is None or values.get("variant") != "personal":
            return OnboardingPreflight("unsupported", ("weixin-variant-unsupported",))
        if not isinstance(values.get("credentialRef"), str):
            return OnboardingPreflight("blocked", ("weixin-credential-ref-required",))
        return OnboardingPreflight("ready", ())

    async def begin_qr_login(
        self, *, local_tokens: tuple[str, ...] = ()
    ) -> dict[str, object]:
        begin = getattr(self._login_transport, "begin_login", None)
        if not callable(begin):
            raise WeixinProtocolError("weixin-login-transport-unavailable")
        return dict(await begin(local_tokens=local_tokens, bot_type="3"))

    async def poll_qr_login(
        self, qrcode: str, *, verify_code: str | None = None
    ) -> dict[str, object]:
        poll = getattr(self._login_transport, "poll_login", None)
        if not callable(poll):
            raise WeixinProtocolError("weixin-login-transport-unavailable")
        return dict(await poll(qrcode, verify_code=verify_code))

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity:
        if not isinstance(attempt, OnboardingAttempt):
            raise WeixinProtocolError("onboarding-attempt-required")
        credentials = await self._authority.resolve(attempt.credential_ref)
        if not isinstance(credentials, WeixinCredentials):
            raise WeixinProtocolError("credential-authority-type-mismatch")
        proof = hashlib.sha256(
            f"{credentials.instance_id}\0{credentials.generation}\0{credentials.account_id}".encode()
        ).hexdigest()
        identity = VerifiedPlatformIdentity(
            f"weixin:{credentials.account_id}", credentials.account_id, f"credential-proof:{proof}"
        )
        self._authenticated[identity.proof_ref] = credentials
        return identity

    def _credentials_for(self, identity: VerifiedPlatformIdentity) -> WeixinCredentials:
        if not isinstance(identity, VerifiedPlatformIdentity):
            raise WeixinProtocolError("verified-identity-required")
        credentials = self._authenticated.get(identity.proof_ref)
        if credentials is None or identity.stable_account_id != credentials.account_id:
            raise WeixinProtocolError("identity-proof-unrecognized")
        return credentials

    async def fingerprint(self, identity: VerifiedPlatformIdentity) -> AdapterInstanceRef:
        credentials = self._credentials_for(identity)
        return AdapterInstanceRef(
            credentials.instance_id,
            "weixin",
            "personal",
            "weixin",
            credentials.organization_id,
            credentials.account_id,
            credentials.generation,
        )

    async def revoke(self, identity: VerifiedPlatformIdentity) -> None:
        if not isinstance(identity, VerifiedPlatformIdentity):
            raise WeixinProtocolError("verified-identity-required")
        self._authenticated.pop(identity.proof_ref, None)

    async def probe_capabilities(
        self, identity: VerifiedPlatformIdentity
    ) -> CapabilitySnapshot:
        credentials = self._credentials_for(identity)
        probe = getattr(self._login_transport, "probe_capabilities", None)
        if not callable(probe):
            raise WeixinProtocolError("capability-probe-unavailable")
        result = await probe(credentials)
        if not isinstance(result, dict) or result.get("authenticated") is not True:
            raise WeixinProtocolError("capability-probe-unauthenticated")
        raw_facts = result.get("facts")
        if not isinstance(raw_facts, dict) or not raw_facts:
            raise WeixinProtocolError("capability-probe-invalid")
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
            raise WeixinProtocolError("capability-probe-invalid")
        return CapabilitySnapshot(
            credentials.instance_id,
            credentials.generation,
            f"weixin-ilink/{hashlib.sha256(repr(facts).encode()).hexdigest()[:16]}",
            "weixin-ilink-authenticated-probe",
            now,
            now + 300_000,
            tuple(sorted(facts, key=lambda fact: fact.name.encode())),
        )


__all__ = ["WeixinCredentialAuthority", "WeixinOnboarding"]
