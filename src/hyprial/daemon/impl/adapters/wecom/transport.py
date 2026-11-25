from __future__ import annotations

import importlib
import importlib.util
import hashlib
import time
from collections.abc import Mapping
from typing import Protocol

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AuthorizedByteStream,
    CapabilityFactV1,
    CapabilitySnapshot,
    FrozenJsonMap,
    FrozenJsonV1,
    NativeTargetResolution,
    OnboardingAttempt,
    OnboardingPreflight,
    OperationNode,
    PartSubmission,
    SealedProviderMediaRef,
    VerifiedPlatformIdentity,
)

from .application import WeComApplicationCredentials
from .bot import WeComBotCredentials
from .errors import WeComProtocolError


class HttpxWeComApplicationTransport:
    def __init__(self, client: object | None = None) -> None:
        if client is None:
            try:
                import httpx
            except ImportError as exc:
                raise WeComProtocolError("httpx-dependency-missing") from exc
            client = httpx.AsyncClient(timeout=15.0, follow_redirects=False)
        self._client = client

    async def get_access_token(self, corp_id: str, corp_secret: str) -> Mapping[str, object]:
        response = await self._client.get(  # type: ignore[attr-defined]
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": corp_id, "corpsecret": corp_secret},
        )
        if response.status_code != 200:
            raise WeComProtocolError(f"token-http-{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise WeComProtocolError("token-invalid-response")
        return value

    async def send(self, access_token: str, payload: dict[str, object]) -> Mapping[str, object]:
        response = await self._client.post(  # type: ignore[attr-defined]
            "https://qyapi.weixin.qq.com/cgi-bin/message/send",
            params={"access_token": access_token},
            json=payload,
        )
        if response.status_code != 200:
            raise WeComProtocolError(f"send-http-{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise WeComProtocolError("send-invalid-response")
        return value


class WeComSdkBotTransport:
    """Official-team AI-bot SDK bridge loaded in the adapter worker."""

    def __init__(self) -> None:
        self._client: object | None = None
        self._frames: dict[str, Mapping[str, object]] = {}

    async def connect(self, credentials: WeComBotCredentials, receiver: object) -> None:
        try:
            sdk = importlib.import_module("aibot")
        except ImportError as exc:
            raise WeComProtocolError("wecom-aibot-dependency-missing") from exc
        client = sdk.WSClient(
            sdk.WSClientOptions(bot_id=credentials.bot_id, secret=credentials.bot_secret)
        )

        async def on_message(frame: object) -> None:
            if not isinstance(frame, Mapping):
                raise WeComProtocolError("bot-frame-invalid")
            headers = frame.get("headers")
            req_id = headers.get("req_id") if isinstance(headers, Mapping) else None
            if not req_id:
                raise WeComProtocolError("bot-frame-req-id-missing")
            copied = dict(frame)
            self._frames[str(req_id)] = copied
            await receiver.ingest_frame(copied)

        client.on("message", on_message)
        await client.connect()
        self._client = client

    async def disconnect(self) -> None:
        if self._client is not None:
            result = self._client.disconnect()  # type: ignore[attr-defined]
            if hasattr(result, "__await__"):
                await result
        self._client = None

    async def send(self, request: Mapping[str, object]) -> Mapping[str, object]:
        if self._client is None:
            raise WeComProtocolError("bot-not-connected")
        body = request.get("body")
        if not isinstance(body, dict):
            raise WeComProtocolError("bot-send-body-invalid")
        reply_req_id = request.get("replyReqId")
        chat_id = request.get("chatId")
        if isinstance(reply_req_id, str) and reply_req_id:
            frame = self._frames.get(reply_req_id)
            if frame is None:
                raise WeComProtocolError("bot-reply-frame-expired")
            result = await self._client.reply(frame, body)  # type: ignore[attr-defined]
        elif isinstance(chat_id, str) and chat_id:
            result = await self._client.send_message(chat_id, body)  # type: ignore[attr-defined]
        else:
            raise WeComProtocolError("bot-send-target-invalid")
        if isinstance(result, Mapping):
            return result
        if hasattr(result, "to_dict"):
            value = result.to_dict()
            if isinstance(value, Mapping):
                return value
        return {}


class WeComCredentialAuthority(Protocol):
    async def resolve(
        self, credential_ref: str
    ) -> WeComBotCredentials | WeComApplicationCredentials: ...


class WeComOnboarding:
    def __init__(
        self,
        authority: WeComCredentialAuthority,
        *,
        bot_probe: object | None = None,
        application_probe: object | None = None,
        dependency_available: bool | None = None,
        now_ms: object | None = None,
    ) -> None:
        self._authority = authority
        self._bot_probe = bot_probe
        self._application_probe = application_probe
        self._dependency_available = dependency_available
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._authenticated: dict[
            str, WeComBotCredentials | WeComApplicationCredentials
        ] = {}

    async def inspect(self, config: FrozenJsonV1) -> OnboardingPreflight:
        values = (
            dict(config)
            if isinstance(config, FrozenJsonMap)
            else config
            if isinstance(config, Mapping)
            else None
        )
        if values is None:
            return OnboardingPreflight("blocked", ("wecom-config-required",))
        variant = values.get("variant")
        if variant not in {"bot", "application"}:
            return OnboardingPreflight("unsupported", ("wecom-variant-unsupported",))
        if not isinstance(values.get("credentialRef"), str):
            return OnboardingPreflight("blocked", ("wecom-credential-ref-required",))
        available = self._dependency_available
        if available is None:
            available = importlib.util.find_spec("aibot") is not None
        if variant == "bot" and not available:
            return OnboardingPreflight("blocked", ("wecom-aibot-dependency-missing",))
        required = {"corpId", "agentId", "callbackBase", "trustedIpConfigured"}
        if variant == "application" and not required <= set(values):
            return OnboardingPreflight(
                "blocked", ("wecom-application-prerequisites-incomplete",)
            )
        if variant == "application" and values.get("trustedIpConfigured") is not True:
            return OnboardingPreflight(
                "blocked", ("wecom-trusted-ip-prerequisite-required",)
            )
        return OnboardingPreflight("ready", ())

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity:
        if not isinstance(attempt, OnboardingAttempt):
            raise WeComProtocolError("onboarding-attempt-required")
        credentials = await self._authority.resolve(attempt.credential_ref)
        if not isinstance(credentials, (WeComBotCredentials, WeComApplicationCredentials)):
            raise WeComProtocolError("credential-authority-type-mismatch")
        if isinstance(credentials, WeComBotCredentials):
            variant = "bot"
            account_id = credentials.bot_id
        else:
            variant = "application"
            account_id = f"{credentials.corp_id}:{credentials.agent_id}"
        proof = hashlib.sha256(
            f"{credentials.instance_id}\0{credentials.generation}\0{variant}\0{account_id}".encode()
        ).hexdigest()
        identity = VerifiedPlatformIdentity(
            f"wecom:{variant}:{account_id}", account_id, f"credential-proof:{proof}"
        )
        self._authenticated[identity.proof_ref] = credentials
        return identity

    def _credentials_for(
        self, identity: VerifiedPlatformIdentity
    ) -> WeComBotCredentials | WeComApplicationCredentials:
        if not isinstance(identity, VerifiedPlatformIdentity):
            raise WeComProtocolError("verified-identity-required")
        credentials = self._authenticated.get(identity.proof_ref)
        if credentials is None:
            raise WeComProtocolError("identity-proof-unrecognized")
        return credentials

    async def fingerprint(self, identity: VerifiedPlatformIdentity) -> AdapterInstanceRef:
        credentials = self._credentials_for(identity)
        if isinstance(credentials, WeComBotCredentials):
            variant = "bot"
            fingerprint = credentials.bot_id
        else:
            variant = "application"
            fingerprint = f"{credentials.corp_id}:{credentials.agent_id}"
        return AdapterInstanceRef(
            credentials.instance_id,
            "wecom",
            variant,
            f"wecom-{variant}",
            credentials.organization_id,
            fingerprint,
            credentials.generation,
        )

    async def revoke(self, identity: VerifiedPlatformIdentity) -> None:
        if not isinstance(identity, VerifiedPlatformIdentity):
            raise WeComProtocolError("verified-identity-required")
        self._authenticated.pop(identity.proof_ref, None)

    async def probe_capabilities(
        self, identity: VerifiedPlatformIdentity
    ) -> CapabilitySnapshot:
        credentials = self._credentials_for(identity)
        probe_owner = (
            self._bot_probe
            if isinstance(credentials, WeComBotCredentials)
            else self._application_probe
        )
        probe = getattr(probe_owner, "probe_capabilities", None)
        if not callable(probe):
            raise WeComProtocolError("capability-probe-unavailable")
        result = await probe(credentials)
        if not isinstance(result, dict) or result.get("authenticated") is not True:
            raise WeComProtocolError("capability-probe-unauthenticated")
        raw_facts = result.get("facts")
        if not isinstance(raw_facts, dict) or not raw_facts:
            raise WeComProtocolError("capability-probe-invalid")
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
            raise WeComProtocolError("capability-probe-invalid")
        now = self._now_ms()
        variant = "bot" if isinstance(credentials, WeComBotCredentials) else "application"
        return CapabilitySnapshot(
            credentials.instance_id,
            credentials.generation,
            f"wecom-{variant}/{hashlib.sha256(repr(facts).encode()).hexdigest()[:16]}",
            f"wecom-{variant}-authenticated-probe",
            now,
            now + 300_000,
            facts,
        )


class WeComMedia:
    def __init__(self, variant: str, authority: object | None = None) -> None:
        if variant not in {"bot", "application"}:
            raise WeComProtocolError("variant-mismatch")
        self._variant = variant
        self._authority = authority

    async def fetch(
        self, source: SealedProviderMediaRef, max_bytes: int
    ) -> AuthorizedByteStream:
        fetch = getattr(self._authority, "fetch", None)
        if not callable(fetch):
            raise WeComProtocolError("media-fetch-unavailable")
        if max_bytes < 1:
            raise WeComProtocolError("invalid-media-limit")
        return await fetch(source, max_bytes)

    async def upload(
        self,
        operation: OperationNode,
        target: NativeTargetResolution,
        stream: AuthorizedByteStream,
    ) -> PartSubmission:
        upload = getattr(self._authority, "upload", None)
        if not callable(upload):
            raise WeComProtocolError("media-upload-unavailable")
        return await upload(operation, target, stream)


__all__ = [
    "HttpxWeComApplicationTransport",
    "WeComCredentialAuthority",
    "WeComMedia",
    "WeComOnboarding",
    "WeComSdkBotTransport",
]
