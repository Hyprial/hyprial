"""Child-only Tryx binding hosted by the generic supervised worker.

This module is never imported by the daemon. The generic worker entry imports it
inside the spawned process, after token receipt and the generation session lock.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import time
from collections.abc import Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeout
from uuid import uuid4

from hyprial.daemon.impl.adapters.worker.entry import WorkerContext, main

from .store import NativeStore


def _jid_text(value: object) -> str:
    user = getattr(value, "user", "")
    server = getattr(value, "server", "")
    if not user or not server:
        raise ValueError("Tryx returned an invalid JID")
    return f"{user}@{server}"


def _split_jid(value: str) -> tuple[str, str]:
    user, separator, server = value.partition("@")
    if not separator or not user or server not in {
        "s.whatsapp.net",
        "g.us",
        "lid",
        "newsletter",
        "broadcast",
    }:
        raise ValueError("invalid or unsupported WhatsApp JID")
    return user, server


def _durable_directory(session_dir) -> object:
    return NativeStore.durable_directory(session_dir)


def _has_field(value: object, name: str) -> bool:
    method = getattr(value, "HasField", None)
    if not callable(method):
        return False
    try:
        return bool(method(name))
    except ValueError:
        return False


def _field(value: object, *names: str) -> object | None:
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _event_kind(edit: object, *, has_reaction: bool) -> str:
    if has_reaction:
        return "reaction"
    return "message" if edit in (None, "", "None", "Empty") else "edit"


def _quoted_message_id(message: object) -> str | None:
    candidates = [message]
    for name in (
        "extendedTextMessage",
        "extended_text_message",
        "imageMessage",
        "image_message",
        "videoMessage",
        "video_message",
        "documentMessage",
        "document_message",
    ):
        if _has_field(message, name):
            nested = getattr(message, name)
            candidates.append(nested)
    for candidate in candidates:
        context = _field(candidate, "contextInfo", "context_info")
        stanza = _field(context, "stanzaId", "stanza_id") if context is not None else None
        if stanza:
            return str(stanza)
    return None


def _native_message_id(result: object) -> str:
    message_id = getattr(result, "message_id", None)
    if not isinstance(message_id, str) or not message_id or message_id in {"None", "Empty"}:
        raise RuntimeError("Tryx send returned no native message evidence")
    return message_id


def _semantic_request_digest(payload: Mapping[str, object]) -> str:
    """Bind provider semantics without W1 or transient transport scope."""
    sealed_request = payload.get("sealedRequest")
    if not isinstance(sealed_request, Mapping):
        raise ValueError("sealed provider request is missing")
    encoded = json.dumps(
        {
            "nodeId": payload.get("nodeId"),
            "kind": payload.get("kind"),
            "idempotencyKey": payload.get("idempotencyKey"),
            "sealedRequest": dict(sealed_request),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class TryxNativeDriver:
    """Exact accepted-pin API adapter; all native objects stay in this child."""

    def __init__(self, context: WorkerContext) -> None:
        # Import only after generic worker token/lock setup has entered factory.
        from tryx.backend import SqliteStore
        from tryx.client import Tryx
        from tryx.events import (
            EvConnected,
            EvDisconnected,
            EvHistorySync,
            EvLoggedOut,
            EvMessage,
            EvPairError,
            EvPairSuccess,
            EvPairingQrCode,
            EvReceipt,
            EvStreamReplaced,
        )

        self.context = context
        self.store = NativeStore.for_worker_session(context.session_dir)
        tryx_store = NativeStore.migrate_named_database(
            context.session_dir, "tryx-session.sqlite3"
        )
        self.backend = SqliteStore(str(tryx_store), 0)
        self.app = Tryx(self.backend)
        self._events = {
            "connected": EvConnected,
            "disconnected": EvDisconnected,
            "history": EvHistorySync,
            "logged-out": EvLoggedOut,
            "message": EvMessage,
            "pair-error": EvPairError,
            "pair-success": EvPairSuccess,
            "qr": EvPairingQrCode,
            "receipt": EvReceipt,
            "stream-replaced": EvStreamReplaced,
        }
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="tryx-native-loop", daemon=True)
        self._run_future: Future[object] | None = None
        self._closed = False
        self._account_jid = self.store.metadata("account_jid")
        self._device_id = self.store.metadata("device_id") or "0"
        self._pairing: dict[str, object] = {
            "state": "paired" if self._account_jid else "new",
            "deviceId": self._device_id,
            "accountJid": self._account_jid,
            "attemptId": self.store.metadata("pairing_attempt_id"),
            "credentialRef": self.store.metadata("pairing_credential_ref"),
            "instanceId": context.identity.instance_id,
            "generation": context.identity.generation,
            "qrRef": None,
            "expiresAtMs": None,
        }
        self._qr_payloads: dict[str, str] = {}
        self._native_messages: dict[str, object] = {}
        self._native_media: dict[str, object] = {}
        self._streams: dict[str, tuple[bytes, int, str | None, str | None]] = {}
        self._uploads: dict[str, dict[str, object]] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.app.on(self._events["qr"])
        async def pairing_qr(_client, event) -> None:
            qr_ref = f"qr-ref:{uuid4().hex}"
            self._qr_payloads[qr_ref] = str(event.code)
            self._pairing = {
                "state": "qr-ready",
                "qrRef": qr_ref,
                "expiresAtMs": int(time.time() * 1000) + int(event.timeout) * 1000,
                "accountJid": None,
                "deviceId": self._device_id,
                "attemptId": None,
                "credentialRef": None,
                "instanceId": self.context.identity.instance_id,
                "generation": self.context.identity.generation,
            }
            self._emit({"type": "pairing", **self._pairing})

        @self.app.on(self._events["pair-success"])
        async def pair_success(_client, event) -> None:
            account = _jid_text(event.data.id)
            self._account_jid = account
            self.store.set_metadata("account_jid", account)
            self.store.set_metadata("device_id", self._device_id)
            self._pairing = {
                "state": "paired",
                "qrRef": None,
                "expiresAtMs": None,
                "accountJid": account,
                "deviceId": self._device_id,
                "attemptId": self._pairing.get("attemptId"),
                "credentialRef": self._pairing.get("credentialRef"),
                "instanceId": self.context.identity.instance_id,
                "generation": self.context.identity.generation,
            }
            self._qr_payloads.clear()
            self._emit({"type": "pairing", **self._pairing})

        @self.app.on(self._events["pair-error"])
        async def pair_error(_client, event) -> None:
            self._pairing = {
                "state": "failed",
                "qrRef": None,
                "expiresAtMs": None,
                "accountJid": None,
                "deviceId": self._device_id,
                "reasonCode": "WHATSAPP_PAIRING_FAILED",
                "attemptId": self._pairing.get("attemptId"),
                "credentialRef": self._pairing.get("credentialRef"),
                "instanceId": self.context.identity.instance_id,
                "generation": self.context.identity.generation,
            }
            self._emit({"type": "pairing", **self._pairing})

        @self.app.on(self._events["message"])
        async def message(_client, event) -> None:
            payload = self._message_payload(event)
            message_id = str(payload["messageId"])
            if payload["isFromMe"]:
                self.store.settle_native_evidence(message_id, "echo")
            self._native_messages[message_id] = event
            cursor = self.store.append_event(payload)
            self._emit({"type": "event", "event": payload, "cursor": cursor})

        @self.app.on(self._events["receipt"])
        async def receipt(_client, event) -> None:
            source = event.source
            chat_jid = _jid_text(source.chat) if source is not None else self._account_jid
            sender_jid = _jid_text(event.message_sender)
            for message_id in event.message_ids:
                self.store.settle_native_evidence(str(message_id), "receipt")
                payload = self._base_event(
                    message_id=str(message_id),
                    chat_jid=chat_jid or sender_jid,
                    sender_jid=sender_jid,
                    participant_jid=None,
                    is_from_me=False,
                    is_group=bool(source and source.is_group),
                    event_kind="receipt",
                    text="",
                    timestamp_ms=int(event.timestamp.timestamp() * 1000),
                    unknown={"receiptType": str(event.receipt_type)},
                )
                cursor = self.store.append_event(payload)
                self._emit({"type": "event", "event": payload, "cursor": cursor})

        async def lifecycle(_client, _event) -> None:
            self._emit({"type": "health", "state": "online"})

        async def disconnected(_client, _event) -> None:
            self._emit({"type": "health", "state": "reconnecting"})

        async def blocked(_client, event) -> None:
            reason_code = (
                "WHATSAPP_SESSION_REVOKED"
                if event.__class__ is self._events["logged-out"]
                else "WHATSAPP_STREAM_REPLACED"
            )
            if self._account_jid:
                self.store.fence_effects(
                    instance_id=self.context.identity.instance_id,
                    generation=self.context.identity.generation,
                    account_jid=self._account_jid,
                    reason_code=reason_code,
                )
            self._emit(
                {
                    "type": "crash",
                    "reasonCode": reason_code,
                }
            )

        self.app.on(self._events["connected"])(lifecycle)
        self.app.on(self._events["disconnected"])(disconnected)
        self.app.on(self._events["logged-out"])(blocked)
        self.app.on(self._events["stream-replaced"])(blocked)

    def start(self) -> None:
        self._thread.start()

        async def run_app() -> None:
            await self.app.run()

        self._run_future = asyncio.run_coroutine_threadsafe(run_app(), self._loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, awaitable, timeout: float = 30.0):
        if self._closed:
            raise RuntimeError("Tryx worker is closed")
        return asyncio.run_coroutine_threadsafe(awaitable, self._loop).result(timeout)

    def _run_submission(self, node_id: str, awaitable, timeout: float = 30.0) -> str:
        if self._closed:
            raise RuntimeError("Tryx worker is closed")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            def preserve_candidate(completed: Future[object]) -> None:
                try:
                    provider_id = completed.result(timeout=0)
                    if isinstance(provider_id, str) and provider_id:
                        self.store.note_submission_candidate(node_id, provider_id)
                except Exception:
                    return

            future.add_done_callback(preserve_candidate)
            raise

    def _emit(self, payload: Mapping[str, object]) -> None:
        self.context.emit_callback(
            {**payload, "generation": self.context.identity.generation}
        )

    def handle_control(self, _control_type: str, payload: object) -> object:
        if not isinstance(payload, Mapping):
            raise TypeError("WhatsApp control payload must be an object")
        operation = payload.get("operation")
        dispatch = {
            "runtime.start": self._runtime_start,
            "runtime.commit-checkpoint": self._commit_checkpoint,
            "runtime.hydrate": self._hydrate,
            "runtime.history": self._history,
            "runtime.resolve": self._resolve,
            "runtime.submit": self._submit,
            "runtime.reconcile": self._reconcile,
            "runtime.reconnect": self._reconnect,
            "runtime.revoke": self._revoke,
            "runtime.close": self._runtime_close,
            "onboarding.inspect": self._inspect,
            "onboarding.pairing-status": self._pairing_status,
            "onboarding.resolve-qr": self._resolve_qr,
            "onboarding.authenticate": self._authenticate,
            "onboarding.capabilities": self._capabilities,
            "media.open": self._media_open,
            "media.read": self._media_read,
            "media.close": self._media_close,
            "media.upload-begin": self._upload_begin,
            "media.upload-chunk": self._upload_chunk,
            "media.upload-finish": self._upload_finish,
            "media.upload-send": self._upload_send,
        }
        if operation not in dispatch:
            raise ValueError(f"unsupported WhatsApp worker operation: {operation}")
        return dispatch[operation](dict(payload))

    def _runtime_start(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("generation") != self.context.identity.generation:
            raise ValueError("stale runtime generation")
        account_jid = str(payload.get("accountJid") or "")
        if not account_jid or (self._account_jid and account_jid != self._account_jid):
            raise ValueError("runtime account scope mismatch")
        reason = self.store.activate_effects(
            instance_id=self.context.identity.instance_id,
            generation=self.context.identity.generation,
            account_jid=account_jid,
        )
        if reason is not None:
            raise RuntimeError(reason)
        return {
            "sessionId": f"whatsapp:{self.context.identity.instance_id}:{self.context.identity.generation}",
            "ingressMode": "pull",
        }

    def _assert_effect_scope(self, payload: dict[str, object]) -> str:
        instance_id = payload.get("instanceId")
        generation = payload.get("generation")
        account_jid = payload.get("accountJid")
        if (
            instance_id != self.context.identity.instance_id
            or generation != self.context.identity.generation
            or not isinstance(account_jid, str)
            or not account_jid
        ):
            raise RuntimeError("WHATSAPP_EFFECT_SCOPE_MISMATCH")
        reason = self.store.effect_fence(
            instance_id=instance_id,
            generation=generation,
            account_jid=account_jid,
        )
        if reason is not None:
            raise RuntimeError(reason)
        return account_jid

    def _commit_checkpoint(self, payload: dict[str, object]) -> dict[str, object]:
        return {
            "committed": self.store.commit_checkpoint(
                str(payload["cursor"]), str(payload["journalId"]), str(payload["envelopeDigest"])
            )
        }

    def _hydrate(self, payload: dict[str, object]) -> dict[str, object]:
        event = payload.get("event")
        if not isinstance(event, Mapping):
            raise TypeError("hydrate event missing")
        canonical = json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        digest = hashlib.sha256(b"hyprial-json-v1\0" + canonical).hexdigest()
        if payload.get("digest") != digest:
            raise ValueError("hydrate digest changed")
        return {"event": dict(event)}

    def _history(self, payload: dict[str, object]) -> dict[str, object]:
        return {"units": self.store.history(payload.get("after"), int(payload["limit"]))}

    def _resolve(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            _split_jid(str(payload["chatJid"]))
        except ValueError:
            return {
                "status": "unresolved",
                "chatJid": None,
                "reasonCode": "WHATSAPP_JID_INVALID",
            }
        return {"status": "resolved", "chatJid": payload["chatJid"], "reasonCode": None}

    def _submit(self, payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(payload)
        node_id = str(payload["nodeId"])
        request = payload.get("sealedRequest")
        if not isinstance(request, Mapping):
            return {"parts": [self._final(node_id, "failed-final", None, True, "WHATSAPP_REQUEST_INVALID")]}
        request_digest = _semantic_request_digest(payload)
        existing = self.store.begin_submission(node_id, request_digest)
        if existing is not None:
            return {"parts": [existing]}
        try:
            provider_id = self._run_submission(
                node_id, self._send(str(payload["kind"]), dict(request))
            )
        except (KeyError, TypeError, ValueError) as error:
            return {"parts": [self._final(node_id, "failed-final", None, True, f"WHATSAPP_REQUEST_INVALID:{error}")]}
        except Exception:
            return {"parts": [self.store.submission(node_id)]}
        self.store.note_submission_candidate(node_id, provider_id)
        if not self.store.settle_native_evidence(provider_id, "tryx-send-result"):
            return {"parts": [self.store.submission(node_id)]}
        return {"parts": [self.store.submission(node_id)], "evidence": "tryx-send-result"}

    async def _send(self, kind: str, request: dict[str, object]) -> str:
        from tryx.types import JID

        user, server = _split_jid(str(request["chatJid"]))
        jid = JID(user, server)
        client = self.app.get_client()
        quoted_id = request.get("quotedMessageId")
        quoted = self._native_messages.get(quoted_id) if isinstance(quoted_id, str) else None
        if quoted_id and quoted is None:
            raise ValueError("quoted message is not available in this generation")
        if kind == "text-send":
            result = await client.send_text(jid, str(request["text"]), quoted=quoted)
            return _native_message_id(result)
        if kind == "reaction":
            result = await client.chat_actions.react_message(
                jid,
                str(request["messageId"]),
                str(request["reaction"]),
                bool(request.get("fromMe", False)),
                None,
            )
            if not isinstance(result, str) or not result or result in {"None", "Empty"}:
                raise RuntimeError("Tryx reaction returned no native message evidence")
            return result
        if kind == "file-send":
            upload = self._uploads[str(request["uploadId"])]
            data = bytes(upload["data"])
            media_type = str(upload.get("mediaType") or "application/octet-stream")
            if media_type.startswith("image/"):
                result = await client.send_photo(jid, data, mimetype=media_type, quoted=quoted)
            elif media_type.startswith("audio/"):
                result = await client.send_audio(jid, data, mimetype=media_type, quoted=quoted)
            elif media_type.startswith("video/"):
                result = await client.send_video(jid, data, mimetype=media_type, quoted=quoted)
            else:
                result = await client.send_document(
                    jid,
                    data,
                    mimetype=media_type,
                    file_name=upload.get("name"),
                    quoted=quoted,
                )
            return _native_message_id(result)
        raise ValueError(f"unsupported native submit kind: {kind}")

    def _final(
        self,
        node_id: str,
        state: str,
        provider_message_id: str | None,
        definitely_not_sent: bool,
        reason_code: str | None,
    ) -> dict[str, object]:
        return self.store.settle_submission(
            node_id, state, provider_message_id, definitely_not_sent, reason_code
        )

    def _reconcile(self, payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(payload)
        row = self.store.submission(
            str(payload["nodeId"]), _semantic_request_digest(payload)
        )
        # Tryx has no accepted-pin lookup-by-id proof. Unknown therefore remains
        # reconcile-only until a correlated echo/receipt updates the ledger.
        return {"parts": [row]}

    def _reconnect(self, _payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(_payload)
        if self._run_future is None or self._run_future.done():
            raise RuntimeError("Tryx session requires supervised worker restart")
        return {"accepted": True, "mode": "native-runtime"}

    def _revoke(self, payload: dict[str, object]) -> dict[str, object]:
        account_jid = self._assert_effect_scope(payload)
        reason_code = str(payload.get("reasonCode") or "WHATSAPP_RUNTIME_REVOKED")
        self.store.fence_effects(
            instance_id=self.context.identity.instance_id,
            generation=self.context.identity.generation,
            account_jid=account_jid,
            reason_code=reason_code,
        )
        return {"fenced": True, "reasonCode": reason_code}

    def _runtime_close(self, _payload: dict[str, object]) -> dict[str, object]:
        return {
            "state": "retained-pending" if self._uploads else "closed",
            "retainedIngress": 0,
            "retainedDeliveries": len(self._uploads),
            "reasonCode": "WHATSAPP_UPLOADS_RETAINED" if self._uploads else None,
        }

    def _inspect(self, _payload: dict[str, object]) -> dict[str, object]:
        return {"state": "ready", "reasonCodes": []}

    def _pairing_request(self, payload: dict[str, object]) -> tuple[str, str, int]:
        attempt_id = payload.get("attemptId")
        credential_ref = payload.get("credentialRef")
        now_ms = payload.get("nowMs")
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(credential_ref, str)
            or not credential_ref
            or payload.get("instanceId") != self.context.identity.instance_id
            or payload.get("generation") != self.context.identity.generation
            or isinstance(now_ms, bool)
            or not isinstance(now_ms, int)
        ):
            raise ValueError("invalid pairing authorization scope")
        return attempt_id, credential_ref, now_ms

    def _pairing_status(self, payload: dict[str, object]) -> dict[str, object]:
        attempt_id, credential_ref, now_ms = self._pairing_request(payload)
        bound_attempt = self._pairing.get("attemptId")
        bound_credential = self._pairing.get("credentialRef")
        if bound_attempt is None and bound_credential is None:
            self._pairing["attemptId"] = attempt_id
            self._pairing["credentialRef"] = credential_ref
            self.store.set_metadata("pairing_attempt_id", attempt_id)
            self.store.set_metadata("pairing_credential_ref", credential_ref)
        elif (bound_attempt, bound_credential) != (attempt_id, credential_ref):
            raise ValueError("pairing authorization binding mismatch")
        expires = self._pairing.get("expiresAtMs")
        if (
            self._pairing.get("state") in {"qr-ready", "scanned"}
            and isinstance(expires, int)
            and expires <= now_ms
        ):
            qr_ref = self._pairing.get("qrRef")
            if isinstance(qr_ref, str):
                self._qr_payloads.pop(qr_ref, None)
            self._pairing.update(
                {"state": "expired", "qrRef": None, "expiresAtMs": expires}
            )
        self._pairing["generation"] = self.context.identity.generation
        return dict(self._pairing)

    def _resolve_qr(self, payload: dict[str, object]) -> dict[str, object]:
        status = self._pairing_status(payload)
        qr_ref = payload.get("qrRef")
        if status.get("state") != "qr-ready" or qr_ref != status.get("qrRef"):
            raise ValueError("QR reference is not current")
        qr_payload = self._qr_payloads.get(str(qr_ref))
        if not qr_payload:
            raise ValueError("QR payload is unavailable")
        return {**status, "qrPayload": qr_payload}

    def _authenticate(self, payload: dict[str, object]) -> dict[str, object]:
        status = self._pairing_status(payload)
        if status["state"] == "paired":
            status["proofRef"] = f"tryx-store:device:{self._device_id}"
        return status

    def _capabilities(self, _payload: dict[str, object]) -> dict[str, object]:
        now = int(time.time() * 1000)
        names = (
            "edit",
            "group",
            "history",
            "linked-device",
            "media",
            "reaction",
            "receipt",
            "reply",
            "text-send",
        )
        return {
            "revision": "tryx:no-account-reviewed:v1",
            "observedAtMs": now,
            "expiresAtMs": now + 300_000,
            "facts": [
                {
                    "name": name,
                    "support": "degraded" if name == "linked-device" else "unknown",
                    "permission": "unknown",
                    "limits": {"workerFrameBytes": 1_048_576},
                    "reasonCode": "NEEDS_ACCOUNT_VALIDATION",
                }
                for name in names
            ],
        }

    def _media_open(self, payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(payload)
        ref = str(payload["authorityHandleId"])
        native = self._native_media.get(ref)
        if native is None:
            raise ValueError("unknown media authority handle")
        data = bytes(self._run(self.app.get_client().download_media(native)))
        if len(data) > int(payload["maxBytes"]):
            raise ValueError("media exceeds authority")
        stream_id = f"media-stream:{uuid4().hex}"
        digest = hashlib.sha256(data).hexdigest()
        self._streams[stream_id] = (data, 0, None, None)
        return {
            "streamId": stream_id,
            "name": None,
            "mediaType": None,
            "size": len(data),
            "sha256": digest,
        }

    def _media_read(self, payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(payload)
        stream_id = str(payload["streamId"])
        data, offset, name, media_type = self._streams[stream_id]
        maximum = min(int(payload["maxBytes"]), 64 * 1024)
        chunk = data[offset : offset + maximum]
        offset += len(chunk)
        self._streams[stream_id] = (data, offset, name, media_type)
        return {"dataBase64": base64.b64encode(chunk).decode(), "eof": offset == len(data)}

    def _media_close(self, payload: dict[str, object]) -> dict[str, object]:
        self._streams.pop(str(payload["streamId"]), None)
        return {"closed": True}

    def _upload_begin(self, payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(payload)
        upload_id = f"whatsapp-upload:{uuid4().hex}"
        size = int(payload["size"])
        if not 0 <= size <= 512 * 1024 * 1024:
            raise ValueError("upload size outside worker bound")
        self._uploads[upload_id] = {
            "data": bytearray(),
            "size": size,
            "sha256": payload["sha256"],
            "name": payload.get("name"),
            "mediaType": payload.get("mediaType"),
        }
        return {"uploadId": upload_id}

    def _upload_chunk(self, payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(payload)
        upload = self._uploads[str(payload["uploadId"])]
        chunk = base64.b64decode(str(payload["dataBase64"]), validate=True)
        if len(chunk) > 64 * 1024 or len(upload["data"]) + len(chunk) > upload["size"]:
            raise ValueError("upload chunk exceeds authority")
        upload["data"].extend(chunk)
        return {"acceptedBytes": len(chunk)}

    def _upload_finish(self, payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(payload)
        upload_id = str(payload["uploadId"])
        upload = self._uploads[upload_id]
        data = bytes(upload["data"])
        if len(data) != upload["size"] or hashlib.sha256(data).hexdigest() != upload["sha256"]:
            self._uploads.pop(upload_id, None)
            return {
                "staged": False,
                "uploadId": upload_id,
                "reasonCode": "WHATSAPP_MEDIA_DIGEST_MISMATCH",
            }
        return {
            "staged": True,
            "uploadId": upload_id,
            "reasonCode": None,
        }

    def _upload_send(self, payload: dict[str, object]) -> dict[str, object]:
        self._assert_effect_scope(payload)
        node_id = str(payload["nodeId"])
        upload_id = str(payload["uploadId"])
        request = {
            "chatJid": payload["chatJid"],
            "uploadId": upload_id,
            "quotedMessageId": payload.get("quotedMessageId"),
        }
        request_digest = _semantic_request_digest(payload)
        try:
            existing = self.store.begin_submission(node_id, request_digest)
        except Exception:
            self._uploads.pop(upload_id, None)
            raise
        if existing is not None:
            self._uploads.pop(upload_id, None)
            return existing
        try:
            provider_id = self._run_submission(node_id, self._send("file-send", request))
        except (KeyError, TypeError, ValueError) as error:
            return self._final(
                node_id,
                "failed-final",
                None,
                True,
                f"WHATSAPP_REQUEST_INVALID:{error}",
            )
        except Exception:
            return self.store.submission(node_id)
        self.store.note_submission_candidate(node_id, provider_id)
        if not self.store.settle_native_evidence(provider_id, "tryx-send-result"):
            return self.store.submission(node_id)
        self._uploads.pop(upload_id, None)
        return {**self.store.submission(node_id), "evidence": "tryx-send-result"}

    def _message_payload(self, event: object) -> dict[str, object]:
        data = event.data
        info = data.message_info
        source = info.source
        raw = data.raw_proto
        wrappers: list[str] = []
        view_once = False
        message = raw
        for fields, label in (
            (("ephemeralMessage", "ephemeral_message"), "ephemeral"),
            (("viewOnceMessage", "view_once_message"), "view-once"),
            (("viewOnceMessageV2", "view_once_message_v2"), "view-once-v2"),
            (("deviceSentMessage", "device_sent_message"), "device-sent"),
        ):
            field = next((name for name in fields if _has_field(message, name)), None)
            if field is not None:
                wrappers.append(label)
                view_once = view_once or label.startswith("view-once")
                nested = getattr(message, field)
                message = getattr(nested, "message", message)
        media: list[dict[str, object]] = []
        for fields, kind in (
            (("imageMessage", "image_message"), "image"),
            (("videoMessage", "video_message"), "video"),
            (("audioMessage", "audio_message"), "audio"),
            (("documentMessage", "document_message"), "file"),
            (("stickerMessage", "sticker_message"), "image"),
        ):
            field = next((name for name in fields if _has_field(message, name)), None)
            if field is not None:
                native = getattr(message, field)
                ref = f"native-media:{self.context.identity.instance_id}:{info.id}:{len(media)}"
                self._native_media[ref] = native
                sha = bytes(_field(native, "fileSha256", "file_sha256") or b"")
                media.append(
                    {
                        "ref": ref,
                        "kind": kind,
                        "name": _field(native, "fileName", "file_name") or None,
                        "mediaType": getattr(native, "mimetype", None) or None,
                        "size": int(_field(native, "fileLength", "file_length") or 0)
                        or None,
                        "sha256": sha.hex() if len(sha) == 32 else None,
                    }
                )
        quoted = _quoted_message_id(message) or _quoted_message_id(raw)
        has_reaction = _has_field(message, "reactionMessage") or _has_field(
            message, "reaction_message"
        )
        event_kind = _event_kind(getattr(info, "edit", None), has_reaction=has_reaction)
        return self._base_event(
            message_id=str(info.id),
            chat_jid=_jid_text(source.chat),
            sender_jid=_jid_text(source.sender),
            participant_jid=_jid_text(source.sender) if source.is_group else None,
            is_from_me=bool(source.is_from_me),
            is_group=bool(source.is_group),
            event_kind=event_kind,
            text=data.get_text() or data.caption or "",
            timestamp_ms=int(info.timestamp.timestamp() * 1000),
            unknown={
                "contentType": str(
                    getattr(info.meta_info, "content_type", "unknown") or "unknown"
                )
            },
            quoted=quoted,
            wrappers=wrappers,
            view_once=view_once,
            media=media,
            push_name=str(info.push_name or ""),
        )

    def _base_event(
        self,
        *,
        message_id: str,
        chat_jid: str,
        sender_jid: str,
        participant_jid: str | None,
        is_from_me: bool,
        is_group: bool,
        event_kind: str,
        text: str,
        timestamp_ms: int,
        unknown: dict[str, object],
        quoted: str | None = None,
        wrappers: list[str] | None = None,
        view_once: bool = False,
        media: list[dict[str, object]] | None = None,
        push_name: str = "",
    ) -> dict[str, object]:
        return {
            "schema": "hyprial.whatsapp.native-event/v1",
            "accountJid": self._account_jid or "unpaired@s.whatsapp.net",
            "chatJid": chat_jid,
            "participantJid": participant_jid,
            "senderJid": sender_jid,
            "messageId": message_id,
            "deviceId": self._device_id,
            "quotedMessageId": quoted,
            "wrappers": wrappers or [],
            "viewOnce": view_once,
            "isFromMe": is_from_me,
            "isGroup": is_group,
            "eventKind": event_kind,
            "text": text,
            "pushName": push_name,
            "providerCreatedAtMs": timestamp_ms,
            "receivedAtMs": int(time.time() * 1000),
            "media": media or [],
            "unknown": unknown,
        }

    def close_admission(self) -> None:
        self._closed = True

    def drain(self, timeout: float) -> bool:
        del timeout
        return True

    def close(self) -> None:
        if self._run_future is not None:
            self._run_future.cancel()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)
        self.store.close()


def create_driver(context: WorkerContext) -> TryxNativeDriver:
    return TryxNativeDriver(context)


if __name__ == "__main__":
    raise SystemExit(main(factory=create_driver))
