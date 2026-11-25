"""Matrix credential authentication and capability projection."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping

from ..contracts import (
    AdapterInstanceRef,
    CapabilityFactV1,
    CapabilitySnapshot,
    FrozenJsonV1,
    OnboardingAttempt,
    OnboardingPreflight,
    VerifiedPlatformIdentity,
)
from .models import (
    MatrixCredentialResolver,
    MatrixCryptoBackend,
    MatrixTransport,
    crypto_descriptor_is_reviewed,
)


def _map(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Matrix {name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Matrix {name} is required")
    return value


class MatrixOnboarding:
    def __init__(
        self,
        transport: MatrixTransport,
        credentials: MatrixCredentialResolver,
        *,
        organization_id: str,
        instance_id: str,
        local_name: str,
        credential_ref: str,
        crypto: MatrixCryptoBackend | None,
        now_ms: object | None = None,
    ) -> None:
        for name, value in (
            ("organization_id", organization_id),
            ("instance_id", instance_id),
            ("local_name", local_name),
            ("credential_ref", credential_ref),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        self._transport = transport
        self._credentials = credentials
        self._organization_id = organization_id
        self._instance_id = instance_id
        self._local_name = local_name
        self._credential_ref = credential_ref
        self._crypto = crypto
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._identity_credentials: dict[str, object] = {}

    async def inspect(self, config: FrozenJsonV1) -> OnboardingPreflight:
        del config
        try:
            credentials = await self._credentials.resolve(self._credential_ref)
        except Exception:
            return OnboardingPreflight("blocked", ("matrix-credentials-unavailable",))
        if (
            credentials.instance_id != self._instance_id
            or credentials.organization_id != self._organization_id
        ):
            return OnboardingPreflight("blocked", ("matrix-credential-scope-mismatch",))
        return OnboardingPreflight("ready", ())

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity:
        if attempt.credential_ref != self._credential_ref:
            raise ValueError("Matrix onboarding credential ref mismatch")
        credentials = await self._credentials.resolve(attempt.credential_ref)
        if (
            credentials.instance_id != self._instance_id
            or credentials.organization_id != self._organization_id
        ):
            raise ValueError("Matrix onboarding credential scope mismatch")
        login = _map(await self._transport.login(credentials), "login response")
        whoami = _map(await self._transport.whoami(credentials), "whoami response")
        login_user = _text(login.get("user_id"), "login user id")
        current_user = _text(whoami.get("user_id"), "whoami user id")
        if login_user != credentials.user_id or current_user != credentials.user_id:
            raise ValueError("Matrix authenticated user mismatch")
        login_device = login.get("device_id")
        current_device = whoami.get("device_id")
        if credentials.device_id is not None and (
            login_device != credentials.device_id or current_device != credentials.device_id
        ):
            raise ValueError("Matrix authenticated device mismatch")
        account_scope = f"{credentials.homeserver}|{credentials.user_id}"
        proof_ref = "matrix-login:" + hashlib.sha256(
            (
                credentials.homeserver
                + "\0"
                + credentials.user_id
                + "\0"
                + (credentials.device_id or "")
            ).encode("utf-8")
        ).hexdigest()
        self._identity_credentials[account_scope] = credentials
        return VerifiedPlatformIdentity(account_scope, credentials.user_id, proof_ref)

    async def fingerprint(
        self, identity: VerifiedPlatformIdentity
    ) -> AdapterInstanceRef:
        credentials = self._identity_credentials.get(identity.account_scope)
        if credentials is None:
            raise ValueError("Matrix identity was not authenticated by this onboarding session")
        fingerprint = hashlib.sha256(
            (
                "matrix\0"
                + credentials.homeserver
                + "\0"
                + credentials.user_id
            ).encode("utf-8")
        ).hexdigest()
        return AdapterInstanceRef(
            self._instance_id,
            "matrix",
            "client",
            self._local_name,
            self._organization_id,
            fingerprint,
            1,
        )

    async def probe_capabilities(
        self, identity: VerifiedPlatformIdentity
    ) -> CapabilitySnapshot:
        if identity.account_scope not in self._identity_credentials:
            raise ValueError("Matrix capability probe requires authenticated identity")
        now = self._now_ms()
        if self._crypto is None:
            encrypted = CapabilityFactV1(
                "encrypted-messages",
                "unsupported",
                "unknown",
                {},
                "matrix-e2e-dependency-unavailable",
            )
            crypto_revision = "missing"
        elif not crypto_descriptor_is_reviewed(self._crypto):
            encrypted = CapabilityFactV1(
                "encrypted-messages",
                "unsupported",
                "unknown",
                {},
                "matrix-e2e-dependency-unreviewed",
            )
            crypto_revision = "unreviewed"
        elif await self._crypto.prepare(self._instance_id, 1):
            encrypted = CapabilityFactV1(
                "encrypted-messages",
                "degraded",
                "granted",
                {"backend": self._crypto.dependency},
                "experimental-upstream-parity",
            )
            crypto_revision = "experimental"
        else:
            encrypted = CapabilityFactV1(
                "encrypted-messages",
                "unsupported",
                "missing",
                {"backend": self._crypto.dependency},
                "matrix-e2e-keys-unavailable",
            )
            crypto_revision = "keys-unavailable"
        facts = (
            CapabilityFactV1("direct-messages", "native", "granted", {}, None),
            CapabilityFactV1(
                "encrypted-attachments",
                "unsupported",
                "not-applicable",
                {},
                "matrix-encrypted-attachments-unsupported",
            ),
            encrypted,
            CapabilityFactV1(
                "media",
                "degraded",
                "granted",
                {"inboundScheme": "mxc", "outboundUpload": False},
                "matrix-media-upload-port-required",
            ),
            CapabilityFactV1("messages", "native", "granted", {}, None),
            CapabilityFactV1(
                "reactions",
                "unsupported",
                "not-applicable",
                {},
                "matrix-reaction-renderer-unavailable",
            ),
            CapabilityFactV1(
                "receipts",
                "unsupported",
                "not-applicable",
                {},
                "matrix-receipt-effect-port-unavailable",
            ),
            CapabilityFactV1(
                "redaction",
                "unsupported",
                "not-applicable",
                {},
                "matrix-redaction-effect-port-unavailable",
            ),
            CapabilityFactV1("replies", "native", "granted", {}, None),
            CapabilityFactV1("threads", "native", "granted", {}, None),
            CapabilityFactV1(
                "typing",
                "unsupported",
                "not-applicable",
                {},
                "matrix-typing-effect-port-unavailable",
            ),
        )
        return CapabilitySnapshot(
            self._instance_id,
            1,
            f"matrix-client-v1:{crypto_revision}",
            "matrix-client-server-api+audited-e2e-backend",
            now,
            now + 300_000,
            facts,
        )


__all__ = ["MatrixOnboarding"]
