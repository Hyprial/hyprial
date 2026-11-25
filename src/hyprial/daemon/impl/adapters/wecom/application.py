from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from hyprial.daemon.impl.adapters.contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    AuthorizedChatTarget,
    CustodyReceipt,
    IngressObservation,
    IngressUnit,
    HydratedEvent,
    IngressSink,
    NativeCheckpoint,
    NativeSubmissionOutcome,
    NativeTargetResolution,
    OperationNode,
    PartSubmission,
    PlatformEventEnvelope,
    RuntimeSession,
    VerifiedCallbackRequest,
    canonical_json_v1,
    freeze_json_v1,
)

from .codec import decode_wecom_application_plaintext
from .crypto import WeComCallbackCrypto
from .errors import WeComProtocolError


@dataclass(frozen=True, slots=True)
class WeComApplicationCredentials:
    instance_id: str
    organization_id: str
    generation: int
    corp_id: str
    agent_id: str
    corp_secret: str
    callback_token: str
    encoding_aes_key: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.instance_id,
                self.organization_id,
                self.corp_id,
                self.agent_id,
                self.corp_secret,
                self.callback_token,
                self.encoding_aes_key,
            )
        ):
            raise WeComProtocolError("missing-application-credentials")
        if self.generation < 1:
            raise WeComProtocolError("invalid-generation")
        try:
            key = base64.b64decode(self.encoding_aes_key + "=", validate=True)
        except ValueError as exc:
            raise WeComProtocolError("invalid-encoding-aes-key") from exc
        if len(key) != 32:
            raise WeComProtocolError("invalid-encoding-aes-key")

    def __repr__(self) -> str:
        return (
            f"WeComApplicationCredentials(instance_id={self.instance_id!r}, "
            f"organization_id={self.organization_id!r}, generation={self.generation!r}, "
            f"corp_id={self.corp_id!r}, agent_id={self.agent_id!r}, secrets=<redacted>)"
        )


class WeComApplicationTransport(Protocol):
    async def get_access_token(self, corp_id: str, corp_secret: str) -> Mapping[str, object]: ...

    async def send(
        self, access_token: str, payload: dict[str, object]
    ) -> Mapping[str, object]: ...


def classify_wecom_application_send(
    result: object, *, operation_started: bool, part_index: int = 0
) -> PartSubmission:
    if isinstance(result, BaseException):
        return PartSubmission(
            part_index,
            "unknown" if operation_started else "failed-retryable",
            None,
            not operation_started,
            "wecom-application-submit-unknown"
            if operation_started
            else "wecom-application-submit-not-started",
        )
    if not isinstance(result, Mapping):
        return PartSubmission(
            part_index, "unknown", None, False, "wecom-application-invalid-response"
        )
    code = result.get("errcode", 0)
    if code not in (0, "0", None):
        retryable = str(code) in {"-1", "45009"}
        return PartSubmission(
            part_index,
            "failed-retryable" if retryable else "failed-final",
            None,
            not operation_started,
            f"wecom-application-provider-{str(code)[:160]}",
        )
    message_id = result.get("msgid") or result.get("msg_id")
    if not isinstance(message_id, str) or not message_id:
        return PartSubmission(
            part_index, "unknown", None, False, "wecom-application-missing-message-id"
        )
    return PartSubmission(part_index, "accepted", message_id, False, None)


class WeComApplicationRuntime:
    def __init__(
        self,
        credentials: WeComApplicationCredentials,
        transport: WeComApplicationTransport,
        *,
        now_ms: object | None = None,
        trusted_source_ips: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(credentials, WeComApplicationCredentials):
            raise TypeError("WeCom application credentials are required for the application variant")
        self._credentials = credentials
        self._transport = transport
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._token: str | None = None
        self._token_expires_at_ms = 0
        self._instance: AdapterInstanceRef | None = None
        self._sink: object | None = None
        self._state = "configured"
        try:
            self._trusted_source_ips = frozenset(
                str(ipaddress.ip_address(value)) for value in trusted_source_ips
            )
        except ValueError as exc:
            raise WeComProtocolError("trusted-source-ip-invalid") from exc
        self._callback_units: dict[str, IngressUnit] = {}
        self._callback_digests: dict[str, str] = {}
        self._callback_committed: set[str] = set()
        self._history: list[IngressUnit] = []
        self.crypto = WeComCallbackCrypto(
            credentials.callback_token,
            credentials.encoding_aes_key,
            credentials.corp_id,
        )

    def _fence(self, instance: AdapterInstanceRef) -> None:
        if instance.kind != "wecom" or instance.variant != "application":
            raise WeComProtocolError("variant-mismatch")
        if instance.instance_id != self._credentials.instance_id:
            raise WeComProtocolError("instance-mismatch")
        if instance.organization_id != self._credentials.organization_id:
            raise WeComProtocolError("organization-mismatch")
        if instance.generation != self._credentials.generation:
            raise WeComProtocolError("generation-mismatch")

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        self._fence(instance)
        if not self._trusted_source_ips:
            raise WeComProtocolError("trusted-ip-prerequisite-required")
        self._instance = instance
        self._sink = sink
        self._state = "online"
        return RuntimeSession(
            f"wecom-application:{instance.instance_id}:{instance.generation}",
            instance.instance_id,
            instance.generation,
            "callback",
        )

    def accept_encrypted_callback(
        self,
        *,
        timestamp: str,
        nonce: str,
        encrypted: str,
        signature: str,
        source_ip: str | None,
    ) -> IngressUnit:
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        if self._trusted_source_ips and source_ip not in self._trusted_source_ips:
            raise WeComProtocolError("callback-source-ip-untrusted")
        replay_key = hashlib.sha256(
            b"wecom-app-envelope\0"
            + timestamp.encode()
            + b"\0"
            + nonce.encode()
            + b"\0"
            + encrypted.encode()
        ).hexdigest()
        prior = self._callback_units.get(replay_key)
        if prior is not None:
            return prior
        plaintext = self.crypto.verify_and_decrypt(
            timestamp=timestamp,
            nonce=nonce,
            encrypted=encrypted,
            signature=signature,
        )
        proof = "wecom-callback-proof:" + hashlib.sha256(
            signature.encode() + b"\0" + replay_key.encode()
        ).hexdigest()
        decoded = decode_wecom_application_plaintext(
            self._instance,
            plaintext,
            corp_id=self._credentials.corp_id,
            agent_id=self._credentials.agent_id,
            callback_proof_ref=proof,
            raw_source_ref=f"sealed-wecom-app:{replay_key}",
        )
        digest = hashlib.sha256(plaintext).hexdigest()
        token = f"wecom-app-checkpoint:{uuid.uuid4().hex}"
        unit = IngressUnit(
            PlatformEventEnvelope(
                IngressObservation(
                    f"wecom-app-observation:{uuid.uuid4().hex}",
                    decoded.aliases,
                    "callback",
                    decoded.provenance.unknown.raw_source_ref,
                    digest,
                    self._now_ms(),
                ),
                freeze_json_v1(
                    {
                        "msgId": decoded.provenance.msg_id,
                        "msgType": decoded.provenance.msg_type,
                        "fromUser": decoded.sender_id,
                        "text": decoded.text,
                        "mediaRefs": list(decoded.media_refs),
                        "callbackProofRef": proof,
                    }
                ),
            ),
            NativeCheckpoint("callback-ack", token, self._now_ms() + 4_000),
            False,
        )
        self._callback_units[replay_key] = unit
        self._callback_digests[token] = digest
        self._history.append(unit)
        return unit

    async def next_ingress(self) -> IngressUnit:
        raise WeComProtocolError("pull-unavailable-for-callback-runtime")

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        if not isinstance(request, VerifiedCallbackRequest):
            raise WeComProtocolError("verified-callback-request-required")
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        if request.instance_id != self._instance.instance_id:
            raise WeComProtocolError("instance-mismatch")
        if request.generation != self._instance.generation:
            raise WeComProtocolError("generation-mismatch")
        headers = json.loads(canonical_json_v1(request.headers))
        body = json.loads(canonical_json_v1(request.body))
        if not isinstance(headers, dict) or not isinstance(body, dict):
            raise WeComProtocolError("callback-envelope-invalid")
        values = {
            "timestamp": headers.get("timestamp"),
            "nonce": headers.get("nonce"),
            "signature": headers.get("signature"),
            "source_ip": headers.get("sourceIp"),
            "encrypted": body.get("encrypt"),
        }
        if not all(isinstance(value, str) and value for value in values.values()):
            raise WeComProtocolError("callback-envelope-invalid")
        unit = self.accept_encrypted_callback(**values)  # type: ignore[arg-type]
        if self.callback_committed(unit.checkpoint):
            return unit
        stage = getattr(self._sink, "stage", None)
        if not callable(stage):
            raise WeComProtocolError("ingress-sink-unavailable")
        custody = await stage(unit)
        if not isinstance(custody, CustodyReceipt):
            raise WeComProtocolError("ingress-custody-receipt-invalid")
        await self.commit_checkpoint(unit.checkpoint, custody)
        return unit

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        if not isinstance(envelope, PlatformEventEnvelope):
            raise WeComProtocolError("event-envelope-required")
        return HydratedEvent(envelope, envelope.payload)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise WeComProtocolError("invalid-history-limit")
        start = 0
        if after is not None:
            for index, unit in enumerate(self._history):
                if unit.checkpoint == after:
                    start = index + 1
                    break
            else:
                raise WeComProtocolError("history-checkpoint-unknown")
        return tuple(self._history[start : start + limit])

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution:
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        if target.chat_ref.instance != self._instance:
            return NativeTargetResolution(
                target, "unauthorized", None, None, "wecom-application-target-instance-mismatch"
            )
        if target.chat_ref.kind != "dm":
            return NativeTargetResolution(
                target, "unsupported", None, None, "wecom-application-dm-only"
            )
        return NativeTargetResolution(
            target, "resolved", target.chat_ref.native_chat_id, None, None
        )

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        token = checkpoint.token or ""
        digest = self._callback_digests.get(token)
        if digest is None:
            raise WeComProtocolError("unknown-checkpoint")
        if custody.envelope_digest != digest:
            raise WeComProtocolError("custody-digest-mismatch")
        self._callback_committed.add(token)

    def callback_committed(self, checkpoint: NativeCheckpoint) -> bool:
        return (checkpoint.token or "") in self._callback_committed

    async def _access_token(self, *, force: bool = False) -> str:
        if not force and self._token and self._now_ms() < self._token_expires_at_ms:
            return self._token
        result = await self._transport.get_access_token(
            self._credentials.corp_id, self._credentials.corp_secret
        )
        if result.get("errcode", 0) not in (0, "0", None):
            raise WeComProtocolError(f"token-rejected:{result.get('errcode')}")
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise WeComProtocolError("token-missing")
        expires = result.get("expires_in", 7200)
        if isinstance(expires, bool) or not isinstance(expires, int) or expires < 60:
            raise WeComProtocolError("token-expiry-invalid")
        self._token = token
        self._token_expires_at_ms = self._now_ms() + (expires - 60) * 1000
        return token

    async def send_text(
        self, user_id: str, text: str, *, idempotency_key: str
    ) -> PartSubmission:
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        if not user_id or not text or not idempotency_key:
            raise WeComProtocolError("invalid-send-request")
        if len(text.encode("utf-8")) > 2048:
            return PartSubmission(0, "unsupported", None, True, "wecom-text-too-large")
        payload: dict[str, object] = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": int(self._credentials.agent_id),
            "text": {"content": text},
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        token = await self._access_token()
        try:
            result = dict(await self._transport.send(token, payload))
        except Exception as exc:
            return classify_wecom_application_send(exc, operation_started=True)
        if str(result.get("errcode", 0)) in {"40014", "42001"}:
            self._token = None
            token = await self._access_token(force=True)
            try:
                result = dict(await self._transport.send(token, payload))
            except Exception as exc:
                return classify_wecom_application_send(exc, operation_started=True)
        return classify_wecom_application_send(result, operation_started=True)

    async def submit(self, operation: OperationNode) -> NativeSubmissionOutcome:
        import json

        if operation.sealed_request is None:
            raise WeComProtocolError("sealed-request-required")
        request = json.loads(canonical_json_v1(operation.sealed_request))
        if not isinstance(request, dict):
            raise WeComProtocolError("sealed-request-invalid")
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        for field, expected in (
            ("instanceId", self._instance.instance_id),
            ("generation", self._instance.generation),
            ("organizationId", self._instance.organization_id),
        ):
            if request.get(field) != expected:
                raise WeComProtocolError(f"{field}-mismatch")
        user_id = request.get("toUserId")
        text = request.get("text")
        if not isinstance(user_id, str) or not isinstance(text, str):
            return NativeSubmissionOutcome(
                (PartSubmission(0, "unsupported", None, True, "wecom-application-text-only"),),
                None,
            )
        submission = await self.send_text(
            user_id, text, idempotency_key=operation.idempotency_key
        )
        return NativeSubmissionOutcome((submission,), None)

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        # The application message/send API has no strong lookup by local
        # idempotency key. An unknown result remains held for operator evidence.
        return prior

    async def reconnect(self, reason: str) -> None:
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        if not isinstance(reason, str) or not reason:
            raise WeComProtocolError("reconnect-reason-required")
        self._state = "reconnecting"
        self._token = None
        self._token_expires_at_ms = 0
        self._state = "online"

    async def health(self) -> AdapterHealth:
        reasons = () if self._state == "online" else (f"wecom-application-{self._state}",)
        return AdapterHealth(
            self._state, "wecom-application-http/v1", 0, 0, reasons
        )

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        self._token = None
        self._state = "stopped"
        retained = len(set(self._callback_digests) - self._callback_committed)
        return AdapterCloseOutcome(
            "closed" if retained == 0 else "retained-pending",
            retained,
            0,
            None if retained == 0 else "wecom-application-callback-pending",
        )


__all__ = [
    "WeComApplicationCredentials",
    "WeComApplicationRuntime",
    "WeComApplicationTransport",
    "classify_wecom_application_send",
]
