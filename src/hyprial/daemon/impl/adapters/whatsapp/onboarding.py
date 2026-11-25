"""Receiver-owned linked-device pairing and capability projection."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    CapabilityFactV1,
    CapabilitySnapshot,
    OnboardingAttempt,
    OnboardingPreflight,
    VerifiedPlatformIdentity,
    canonical_json_v1,
    freeze_json_v1,
)

from .boundary import (
    ArtifactStatus,
    NativeWorker,
    WhatsAppProviderError,
    invoke_native,
    require_artifact,
)


@dataclass(frozen=True, slots=True)
class WhatsAppPairingStatus:
    state: Literal["new", "qr-ready", "scanned", "paired", "expired", "failed"]
    qr_ref: str | None
    expires_at_ms: int | None
    account_jid: str | None
    device_id: str | None


class WhatsAppOnboarding:
    def __init__(
        self,
        worker: NativeWorker,
        *,
        instance,
        artifact: ArtifactStatus,
        clock_ms=None,
    ) -> None:
        self.worker = worker
        self.instance = instance
        self.artifact = artifact
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def _attempt_payload(self, attempt: OnboardingAttempt, now_ms: int) -> dict[str, object]:
        return {
            "attemptId": attempt.attempt_id,
            "credentialRef": attempt.credential_ref,
            "instanceId": self.instance.instance_id,
            "generation": self.instance.generation,
            "nowMs": now_ms,
        }

    def _validate_binding(
        self,
        response: Mapping[str, object],
        attempt: OnboardingAttempt,
        now_ms: int,
    ) -> None:
        expected = (
            attempt.attempt_id,
            attempt.credential_ref,
            self.instance.instance_id,
            self.instance.generation,
        )
        observed = (
            response.get("attemptId"),
            response.get("credentialRef"),
            response.get("instanceId"),
            response.get("generation"),
        )
        if observed != expected:
            raise WhatsAppProviderError(
                "WHATSAPP_PAIRING_BINDING_MISMATCH",
                "pairing result does not match attempt, credential, instance, and generation",
            )
        expires_at_ms = response.get("expiresAtMs")
        if response.get("state") in {"qr-ready", "scanned"} and (
            not isinstance(expires_at_ms, int) or expires_at_ms <= now_ms
        ):
            raise WhatsAppProviderError("WHATSAPP_PAIRING_EXPIRED", "pairing QR expired")

    async def inspect(self, config) -> OnboardingPreflight:
        if not self.artifact.available:
            return OnboardingPreflight("blocked", self.artifact.reason_codes)
        frozen = freeze_json_v1(config)
        response = await invoke_native(
            self.worker,
            "onboarding.inspect",
            {"config": __import__("json").loads(canonical_json_v1(frozen))},
        )
        return OnboardingPreflight(
            response.get("state"), tuple(response.get("reasonCodes", ()))
        )

    async def pairing_status(self, attempt: OnboardingAttempt) -> WhatsAppPairingStatus:
        require_artifact(self.artifact)
        now_ms = self._clock_ms()
        response = await invoke_native(
            self.worker,
            "onboarding.pairing-status",
            self._attempt_payload(attempt, now_ms),
        )
        self._validate_binding(response, attempt, now_ms)
        try:
            return WhatsAppPairingStatus(
                response["state"],
                response.get("qrRef"),
                response.get("expiresAtMs"),
                response.get("accountJid"),
                response.get("deviceId"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise WhatsAppProviderError("WHATSAPP_PAIRING_STATE_INVALID", str(error)) from error

    async def resolve_pairing_qr(self, attempt: OnboardingAttempt, qr_ref: str) -> str:
        require_artifact(self.artifact)
        if not qr_ref:
            raise ValueError("qr_ref must not be empty")
        now_ms = self._clock_ms()
        response = await invoke_native(
            self.worker,
            "onboarding.resolve-qr",
            {**self._attempt_payload(attempt, now_ms), "qrRef": qr_ref},
        )
        self._validate_binding(response, attempt, now_ms)
        payload = response.get("qrPayload")
        if not isinstance(payload, str) or not payload:
            raise WhatsAppProviderError(
                "WHATSAPP_PAIRING_STATE_INVALID", "resolved QR payload missing"
            )
        return payload

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity:
        require_artifact(self.artifact)
        now_ms = self._clock_ms()
        response = await invoke_native(
            self.worker,
            "onboarding.authenticate",
            self._attempt_payload(attempt, now_ms),
        )
        self._validate_binding(response, attempt, now_ms)
        if response.get("state") != "paired":
            raise WhatsAppProviderError(
                "WHATSAPP_PAIRING_PENDING", f"pairing state is {response.get('state')}"
            )
        account = response.get("accountJid")
        device = response.get("deviceId")
        proof = response.get("proofRef")
        if not all(isinstance(value, str) and value for value in (account, device, proof)):
            raise WhatsAppProviderError("WHATSAPP_PAIRING_STATE_INVALID", "paired identity incomplete")
        return VerifiedPlatformIdentity(account, f"{account}#{device}", proof)

    async def fingerprint(self, identity: VerifiedPlatformIdentity) -> AdapterInstanceRef:
        digest = hashlib.sha256(
            f"whatsapp-linked-device\0{identity.stable_account_id}".encode()
        ).hexdigest()
        return AdapterInstanceRef(
            instance_id=self.instance.instance_id,
            kind="whatsapp",
            variant="linked-device",
            local_name=self.instance.local_name,
            organization_id=self.instance.organization_id,
            external_fingerprint=digest,
            generation=self.instance.generation,
        )

    async def probe_capabilities(
        self, identity: VerifiedPlatformIdentity
    ) -> CapabilitySnapshot:
        require_artifact(self.artifact)
        response = await invoke_native(
            self.worker,
            "onboarding.capabilities",
            {"accountScope": identity.account_scope, "proofRef": identity.proof_ref},
        )
        raw = response.get("facts")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray, memoryview)):
            raise WhatsAppProviderError("WHATSAPP_CAPABILITIES_INVALID", "facts missing")
        facts: list[CapabilityFactV1] = []
        try:
            for item in raw:
                if not isinstance(item, Mapping):
                    raise TypeError("capability fact must be an object")
                facts.append(
                    CapabilityFactV1(
                        item["name"],
                        item["support"],
                        item["permission"],
                        item["limits"],
                        item.get("reasonCode"),
                    )
                )
            facts.sort(key=lambda fact: fact.name.encode())
            return CapabilitySnapshot(
                self.instance.instance_id,
                self.instance.generation,
                response["revision"],
                "tryx-worker-no-account-evidence",
                response["observedAtMs"],
                response["expiresAtMs"],
                tuple(facts),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise WhatsAppProviderError("WHATSAPP_CAPABILITIES_INVALID", str(error)) from error


__all__ = ["WhatsAppOnboarding", "WhatsAppPairingStatus"]
