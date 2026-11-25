from __future__ import annotations

import asyncio
import base64
from collections import deque

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    NativeTargetResolution,
    OnboardingAttempt,
    SealedProviderMediaRef,
)

from .support import FakeWorker, MemoryStream, instance, operation, target


def test_unavailable_candidate_is_typed_blocked_without_import_or_native_call() -> None:
    async def scenario() -> None:
        from hyprial.daemon.impl.adapters.whatsapp import ArtifactStatus, WhatsAppOnboarding

        worker = FakeWorker()
        onboarding = WhatsAppOnboarding(
            worker,
            instance=instance(),
            artifact=ArtifactStatus.unavailable(
                "WHATSAPP_TRYX_ARTIFACT_UNAVAILABLE",
                "WHATSAPP_RELEASE_ASSEMBLY_OPEN",
            ),
        )
        preflight = await onboarding.inspect({})
        assert preflight.state == "blocked"
        assert preflight.reason_codes == (
            "WHATSAPP_TRYX_ARTIFACT_UNAVAILABLE",
            "WHATSAPP_RELEASE_ASSEMBLY_OPEN",
        )
        with pytest.raises(Exception) as raised:
            await onboarding.authenticate(OnboardingAttempt("attempt-1", {}, "credential-ref:receiver"))
        assert getattr(raised.value, "code", None) == "WHATSAPP_TRYX_ARTIFACT_UNAVAILABLE"
        assert worker.calls == []

    asyncio.run(scenario())


def test_qr_pairing_lifecycle_identity_fingerprint_and_capabilities_are_exact() -> None:
    async def scenario() -> None:
        from hyprial.daemon.impl.adapters.whatsapp import ArtifactStatus, WhatsAppOnboarding

        worker = FakeWorker()
        worker.add("onboarding.inspect", {"state": "ready", "reasonCodes": []})
        worker.add(
            "onboarding.pairing-status",
            {"state": "qr-ready", "qrRef": "qr-ref:opaque-1", "expiresAtMs": 5000, "accountJid": None, "deviceId": "0", "attemptId": "attempt-1", "credentialRef": "credential-ref:receiver", "instanceId": "wa-main", "generation": 7},
            {"state": "paired", "qrRef": None, "expiresAtMs": None, "accountJid": "15550001@s.whatsapp.net", "deviceId": "0", "attemptId": "attempt-1", "credentialRef": "credential-ref:receiver", "instanceId": "wa-main", "generation": 7},
        )
        worker.add(
            "onboarding.resolve-qr",
            {"qrPayload": "tryx-native-qr-payload", "attemptId": "attempt-1", "credentialRef": "credential-ref:receiver", "instanceId": "wa-main", "generation": 7, "expiresAtMs": 5000},
        )
        worker.add(
            "onboarding.authenticate",
            {"state": "paired", "accountJid": "15550001@s.whatsapp.net", "deviceId": "0", "proofRef": "tryx-store:device:0", "attemptId": "attempt-1", "credentialRef": "credential-ref:receiver", "instanceId": "wa-main", "generation": 7, "expiresAtMs": None},
        )
        worker.add(
            "onboarding.capabilities",
            {
                "revision": "tryx:no-account-reviewed:v1",
                "observedAtMs": 1000,
                "expiresAtMs": 2000,
                "facts": [
                    {"name": "linked-device", "support": "degraded", "permission": "unknown", "limits": {}, "reasonCode": "NEEDS_ACCOUNT_VALIDATION"},
                    {"name": "text-send", "support": "unknown", "permission": "unknown", "limits": {}, "reasonCode": "NEEDS_ACCOUNT_VALIDATION"},
                ],
            },
        )
        onboarding = WhatsAppOnboarding(
            worker,
            instance=instance(),
            artifact=ArtifactStatus.available_candidate("tryx-test-build"),
            clock_ms=lambda: 1000,
        )
        assert (await onboarding.inspect({})).state == "ready"
        attempt = OnboardingAttempt("attempt-1", {}, "credential-ref:receiver")
        first = await onboarding.pairing_status(attempt)
        assert first.state == "qr-ready" and first.qr_ref.startswith("qr-ref:")
        assert await onboarding.resolve_pairing_qr(attempt, first.qr_ref) == "tryx-native-qr-payload"
        second = await onboarding.pairing_status(attempt)
        assert second.state == "paired" and second.account_jid == "15550001@s.whatsapp.net"
        identity = await onboarding.authenticate(attempt)
        fingerprint = await onboarding.fingerprint(identity)
        assert fingerprint.kind == "whatsapp" and fingerprint.variant == "linked-device"
        assert fingerprint.external_fingerprint != identity.stable_account_id
        capabilities = await onboarding.probe_capabilities(identity)
        assert [fact.name for fact in capabilities.facts] == ["linked-device", "text-send"]
        assert all(fact.support != "native" for fact in capabilities.facts)

        pairing_calls = [
            call for call in worker.calls if call["operation"].startswith("onboarding.")
        ]
        assert pairing_calls[1] == {
            "operation": "onboarding.pairing-status",
            "attemptId": "attempt-1",
            "credentialRef": "credential-ref:receiver",
            "instanceId": "wa-main",
            "generation": 7,
            "nowMs": 1000,
        }
        assert pairing_calls[2]["operation"] == "onboarding.resolve-qr"

    asyncio.run(scenario())


def test_qr_resolve_and_status_reject_wrong_binding_and_expiry() -> None:
    async def scenario() -> None:
        from hyprial.daemon.impl.adapters.whatsapp import ArtifactStatus, WhatsAppOnboarding

        worker = FakeWorker()
        worker.add(
            "onboarding.pairing-status",
            {"state": "qr-ready", "qrRef": "qr-ref:opaque", "expiresAtMs": 1500, "accountJid": None, "deviceId": "0", "attemptId": "other-attempt", "credentialRef": "credential-ref:receiver", "instanceId": "wa-main", "generation": 7},
            {"state": "qr-ready", "qrRef": "qr-ref:opaque", "expiresAtMs": 1500, "accountJid": None, "deviceId": "0", "attemptId": "attempt-1", "credentialRef": "credential-ref:receiver", "instanceId": "wa-main", "generation": 7},
        )
        ticks = iter((1000, 2000))
        onboarding = WhatsAppOnboarding(
            worker,
            instance=instance(),
            artifact=ArtifactStatus.available_candidate("tryx-test-build"),
            clock_ms=lambda: next(ticks),
        )
        attempt = OnboardingAttempt("attempt-1", {}, "credential-ref:receiver")
        with pytest.raises(Exception) as wrong:
            await onboarding.pairing_status(attempt)
        assert getattr(wrong.value, "code", None) == "WHATSAPP_PAIRING_BINDING_MISMATCH"
        with pytest.raises(Exception) as expired:
            await onboarding.pairing_status(attempt)
        assert getattr(expired.value, "code", None) == "WHATSAPP_PAIRING_EXPIRED"
        assert worker.operations() == [
            "onboarding.pairing-status",
            "onboarding.pairing-status",
        ]

    asyncio.run(scenario())


def test_media_uses_bounded_chunks_and_accepts_only_after_native_send_evidence() -> None:
    async def scenario() -> None:
        from hyprial.daemon.impl.adapters.whatsapp import ArtifactStatus, WhatsAppMedia

        worker = FakeWorker()
        worker.add(
            "media.open",
            {"streamId": "stream-1", "name": "photo.jpg", "mediaType": "image/jpeg", "size": 5, "sha256": "0" * 64},
        )
        worker.add(
            "media.read",
            {"dataBase64": base64.b64encode(b"image").decode(), "eof": False},
            {"dataBase64": "", "eof": True},
        )
        worker.add("media.close", {"closed": True})
        worker.add("media.upload-begin", {"uploadId": "upload-1"})
        worker.add("media.upload-chunk", {"acceptedBytes": 3}, {"acceptedBytes": 2})
        worker.add("media.upload-finish", {"staged": True, "uploadId": "upload-1"})
        worker.add(
            "media.upload-send",
            {"partIndex": 0, "state": "accepted", "providerMessageId": "wamid-native-1", "definitelyNotSent": False, "reasonCode": None, "evidence": "tryx-send-result"},
        )
        media = WhatsAppMedia(
            worker,
            instance=instance(),
            artifact=ArtifactStatus.available_candidate("tryx-test-build"),
        )
        source = SealedProviderMediaRef("wa-main", 7, "a" * 64, "native-media:1")
        downloaded = await media.fetch(source, 10)
        assert await downloaded.read(64 * 1024) == b"image"
        assert await downloaded.read(64 * 1024) == b""
        await downloaded.aclose()
        upload = MemoryStream(deque([b"abc", b"de"]))
        resolved = NativeTargetResolution(
            target(), "resolved", "120363000000@g.us", None, None
        )
        result = await media.upload(operation(kind="file-upload"), resolved, upload)
        assert result.state == "accepted"
        assert result.provider_message_id == "wamid-native-1"
        assert upload.closed is True
        chunks = [call for call in worker.calls if call["operation"] == "media.upload-chunk"]
        assert [len(base64.b64decode(call["dataBase64"])) for call in chunks] == [3, 2]
        assert all(len(base64.b64decode(call["dataBase64"])) <= 64 * 1024 for call in chunks)
        finish_index = worker.operations().index("media.upload-finish")
        send_index = worker.operations().index("media.upload-send")
        assert finish_index < send_index
        send_call = worker.calls[send_index]
        assert send_call["uploadId"] == "upload-1"
        assert send_call["nodeId"] == "node-1"
        assert send_call["chatJid"] == "120363000000@g.us"

    asyncio.run(scenario())


def test_runtime_crash_and_revoke_share_one_effect_fence_with_media() -> None:
    async def scenario() -> None:
        from hyprial.daemon.impl.adapters.whatsapp import (
            ArtifactStatus,
            WhatsAppEffectFence,
            WhatsAppMedia,
            WhatsAppRuntime,
        )

        worker = FakeWorker()
        fence = WhatsAppEffectFence(instance(), "15550001@s.whatsapp.net")
        runtime = WhatsAppRuntime(
            worker,
            instance=instance(),
            account_jid="15550001@s.whatsapp.net",
            artifact=ArtifactStatus.available_candidate("tryx-test-build"),
            effect_fence=fence,
        )
        media = WhatsAppMedia(
            worker,
            instance=instance(),
            artifact=ArtifactStatus.available_candidate("tryx-test-build"),
            account_jid="15550001@s.whatsapp.net",
            effect_fence=fence,
        )
        assert runtime.accept_native_callback(
            {"type": "crash", "reasonCode": "WHATSAPP_NATIVE_CRASH", "generation": 7}
        )
        upload = MemoryStream(deque([b"abc"]))
        resolved = NativeTargetResolution(
            target(), "resolved", "120363000000@g.us", None, None
        )
        with pytest.raises(Exception) as crashed:
            await media.upload(operation(kind="file-upload"), resolved, upload)
        assert getattr(crashed.value, "code", None) == "WHATSAPP_NATIVE_CRASH"
        assert worker.calls == []

        runtime.revoke("WHATSAPP_CLAIM_REVOKED")
        with pytest.raises(Exception) as revoked:
            await media.fetch(
                SealedProviderMediaRef("wa-main", 7, "a" * 64, "native-media:1"), 10
            )
        assert getattr(revoked.value, "code", None) == "WHATSAPP_CLAIM_REVOKED"

    asyncio.run(scenario())


def test_media_rejects_wrong_instance_generation_before_native_access() -> None:
    async def scenario() -> None:
        from hyprial.daemon.impl.adapters.whatsapp import ArtifactStatus, WhatsAppMedia

        worker = FakeWorker()
        media = WhatsAppMedia(
            worker,
            instance=instance(),
            artifact=ArtifactStatus.available_candidate("tryx-test-build"),
        )
        stale = SealedProviderMediaRef("wa-main", 6, "a" * 64, "native-media:1")
        with pytest.raises(Exception) as raised:
            await media.fetch(stale, 10)
        assert getattr(raised.value, "code", None) == "WHATSAPP_MEDIA_AUTHORITY_MISMATCH"
        assert worker.calls == []

    asyncio.run(scenario())
