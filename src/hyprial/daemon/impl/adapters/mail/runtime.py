"""Read-only IMAP polling runtime with custody-gated UID checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import imaplib
import re
import ssl
import time
from collections.abc import Callable
from email import policy
from email.parser import BytesHeaderParser

from ..contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    CustodyReceipt,
    HydratedEvent,
    IngressObservation,
    IngressSink,
    IngressUnit,
    NativeAlias,
    NativeCheckpoint,
    PlatformEventEnvelope,
    RuntimeSession,
    frozen_json_digest_v1,
)
from .models import (
    MailCheckpoint,
    MailCheckpointLedger,
    MailConnectionConfig,
    MailCredentialResolver,
    MailCredentials,
    MailFetchEvidence,
)
from .state import MailStateStore


_UIDNEXT = re.compile(rb"\bUIDNEXT\s+(\d+)\b", re.IGNORECASE)


class MailRuntimeBlockedError(RuntimeError):
    pass


class MailImapRuntime:
    """A single read-only IMAP session; all socket calls run via ``to_thread``."""

    def __init__(
        self,
        config: MailConnectionConfig,
        credentials: MailCredentialResolver,
        ledger: MailCheckpointLedger,
        factory: Callable[..., object] | None = None,
        *,
        state_store: MailStateStore | None = None,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._ledger = ledger
        self._factory = factory
        self._state_store = state_store
        self._instance: AdapterInstanceRef | None = None
        self._client: object | None = None
        self._uidvalidity: int | None = None
        self._next_uid = 1
        self._pending: list[int] = []
        self._raw: dict[str, bytes] = {}
        self._fetch_evidence: dict[str, MailFetchEvidence] = {}
        self._pending_checkpoints: dict[str, tuple[str, str, int]] = {}
        self._checkpoint_order: list[str] = []
        self._rollover_epoch_id: str | None = None
        self._rollover_scan_end: int | None = None
        self._state = "configured"
        self._reason_codes: tuple[str, ...] = ()

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        del sink
        if instance.kind != "mail" or instance.variant != "imap-smtp":
            raise ValueError("IMAP runtime requires mail/imap-smtp instance")
        self._state = "starting"
        credentials = await self._credentials.resolve(self._config.credential_ref)
        client, uidvalidity = await asyncio.to_thread(self._open_sync, credentials)
        checkpoint = await self._ledger.load(instance.instance_id, instance.generation, self._config.folder)
        if checkpoint is not None and (
            checkpoint.mailbox_scope != self._config.mailbox_scope
            or checkpoint.folder != self._config.folder
        ):
            await asyncio.to_thread(client.logout)
            self._state = "blocked"
            self._reason_codes = ("MAIL_CHECKPOINT_SCOPE_MISMATCH",)
            raise MailRuntimeBlockedError(self._reason_codes[0])
        epoch = (
            self._state_store.load_epoch(self._config.mailbox_scope, self._config.folder)
            if self._state_store is not None
            else None
        )
        if epoch is not None and epoch.uidvalidity == uidvalidity:
            if epoch.state == "active":
                self._next_uid = epoch.scan_next_uid
            else:
                self._rollover_epoch_id = epoch.epoch_id
                self._rollover_scan_end = epoch.scan_end_uid
                self._next_uid = (
                    epoch.scan_next_uid
                    if epoch.scan_end_uid is not None
                    and epoch.scan_next_uid <= epoch.scan_end_uid
                    else epoch.tail_next_uid
                )
        elif (
            epoch is not None and epoch.uidvalidity != uidvalidity
        ) or (
            checkpoint is not None and checkpoint.uidvalidity != uidvalidity
        ):
            if self._state_store is None:
                await asyncio.to_thread(client.logout)
                self._state = "blocked"
                self._reason_codes = ("MAIL_UIDVALIDITY_ROLLOVER_REQUIRES_RECONCILIATION",)
                raise MailRuntimeBlockedError(self._reason_codes[0])
            boundary = await asyncio.to_thread(self._highest_uid_sync, client)
            epoch = self._state_store.observe_uidvalidity(
                mailbox_scope=self._config.mailbox_scope,
                folder=self._config.folder,
                uidvalidity=uidvalidity,
                snapshot_highest_uid=boundary,
                operation_id=(
                    f"mail-rollover:{instance.instance_id}:{instance.generation}:"
                    f"{self._config.folder}:{uidvalidity}"
                ),
                now_ms=int(time.time() * 1000),
            )
            if epoch.state == "held":
                await asyncio.to_thread(client.logout)
                self._state = "blocked"
                self._reason_codes = ("MAIL_UIDVALIDITY_BOUNDARY_UNAVAILABLE",)
                raise MailRuntimeBlockedError(self._reason_codes[0])
            self._rollover_epoch_id = epoch.epoch_id
            self._rollover_scan_end = epoch.scan_end_uid
            self._next_uid = epoch.scan_next_uid
        else:
            self._next_uid = (checkpoint.uid + 1) if checkpoint else 1
            if self._state_store is not None and epoch is None:
                self._state_store.initialize_epoch(
                    mailbox_scope=self._config.mailbox_scope,
                    folder=self._config.folder,
                    uidvalidity=uidvalidity,
                    checkpoint_uid=checkpoint.uid if checkpoint else 0,
                    operation_id=(
                        f"mail-epoch:{instance.instance_id}:{instance.generation}:"
                        f"{self._config.folder}:{uidvalidity}"
                    ),
                    now_ms=int(time.time() * 1000),
                )
        self._instance = instance
        self._client = client
        self._uidvalidity = uidvalidity
        self._state = "degraded" if self._rollover_epoch_id else "online"
        return RuntimeSession(
            f"mail:{instance.instance_id}:{instance.generation}",
            instance.instance_id,
            instance.generation,
            "pull",
        )

    def _open_sync(self, credentials: MailCredentials) -> tuple[object, int]:
        context = ssl.create_default_context()
        client = (
            self._factory(self._config.imap_host, self._config.imap_port, ssl_context=context)
            if self._factory
            else imaplib.IMAP4_SSL(
                self._config.imap_host,
                self._config.imap_port,
                ssl_context=context,
                timeout=30,
            )
        )
        status, _response = client.login(credentials.username, credentials.password)
        if str(status).upper() != "OK":
            raise MailRuntimeBlockedError("MAIL_IMAP_AUTH_FAILED")
        status, _response = client.select(self._config.folder, readonly=True)
        if str(status).upper() != "OK":
            raise MailRuntimeBlockedError("MAIL_IMAP_FOLDER_UNAVAILABLE")
        _name, values = client.response("UIDVALIDITY")
        try:
            uidvalidity = int(values[-1])
        except (IndexError, TypeError, ValueError) as exc:
            raise MailRuntimeBlockedError("MAIL_UIDVALIDITY_UNAVAILABLE") from exc
        return client, uidvalidity

    def _highest_uid_sync(self, client: object) -> int | None:
        try:
            status, values = client.status(self._config.folder, "(UIDNEXT)")
        except (AttributeError, OSError, imaplib.IMAP4.error):
            return None
        if str(status).upper() != "OK":
            return None
        for value in values or ():
            raw = bytes(value) if isinstance(value, (bytes, bytearray)) else str(value).encode()
            match = _UIDNEXT.search(raw)
            if match is not None:
                return max(0, int(match.group(1)) - 1)
        return None

    async def next_ingress(self) -> IngressUnit:
        instance, client, uidvalidity = self._require_online()
        if not self._pending:
            self._pending = await asyncio.to_thread(self._search_sync, client, self._next_uid)
        if not self._pending:
            raise StopAsyncIteration
        uid = self._pending.pop(0)
        raw = await asyncio.to_thread(self._fetch_sync, client, uid)
        token = MailCheckpoint(self._config.mailbox_scope, self._config.folder, uidvalidity, uid)
        raw_ref = f"sealed:mail:{instance.instance_id}:{uidvalidity}:{uid}"
        self._raw[raw_ref] = raw
        evidence = MailFetchEvidence(
            self._config.mailbox_scope,
            self._config.authenticated_mailbox_id,
            self._config.folder,
            uidvalidity,
            uid,
            hashlib.sha256(raw).hexdigest(),
        )
        self._fetch_evidence[raw_ref] = evidence
        rollover_quarantined = (
            self._rollover_epoch_id is not None
            and self._rollover_scan_end is not None
            and uid <= self._rollover_scan_end
        )
        payload = {
            "mailbox_scope": self._config.mailbox_scope,
            "authenticated_mailbox_id": self._config.authenticated_mailbox_id,
            "folder": self._config.folder,
            "uidvalidity": uidvalidity,
            "uid": uid,
            "raw_source_ref": raw_ref,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "rollover_quarantined": rollover_quarantined,
        }
        observation_digest = frozen_json_digest_v1(payload)
        observation = IngressObservation(
            f"mail:{instance.instance_id}:{uidvalidity}:{uid}",
            (
                NativeAlias(
                    instance.instance_id,
                    instance.generation,
                    self._config.mailbox_scope,
                    self._config.folder,
                    "create",
                    "cursor-item",
                    f"{uidvalidity}:{uid}",
                    "mail-imap-v1",
                ),
            ),
            "poll",
            raw_ref,
            observation_digest,
            int(time.time() * 1000),
        )
        envelope = PlatformEventEnvelope(observation, payload)
        checkpoint = NativeCheckpoint("imap-cursor", token.token, None)
        if checkpoint.token is None:  # pragma: no cover - NativeCheckpoint invariant
            raise RuntimeError("mail checkpoint token unexpectedly absent")
        self._pending_checkpoints[checkpoint.token] = (
            observation_digest,
            instance.instance_id,
            instance.generation,
        )
        self._checkpoint_order.append(checkpoint.token)
        return IngressUnit(envelope, checkpoint, True)

    def _search_sync(self, client: object, next_uid: int) -> list[int]:
        status, values = client.uid("search", None, f"UID {next_uid}:*")
        if str(status).upper() != "OK":
            raise MailRuntimeBlockedError("MAIL_IMAP_SEARCH_FAILED")
        raw = values[0] if values else b""
        tokens = bytes(raw).split() if isinstance(raw, (bytes, bytearray)) else ()
        return sorted(
            int(token) for token in tokens if int(token) >= next_uid
        )[: self._config.page_size]

    @staticmethod
    def _fetch_sync(client: object, uid: int) -> bytes:
        status, values = client.uid("fetch", str(uid), "(BODY.PEEK[])")
        if str(status).upper() != "OK":
            raise MailRuntimeBlockedError("MAIL_IMAP_FETCH_FAILED")
        for item in values:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                return item[1]
        raise MailRuntimeBlockedError("MAIL_IMAP_BODY_UNAVAILABLE")

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        instance, _client, uidvalidity = self._require_online()
        if checkpoint.kind != "imap-cursor" or checkpoint.token is None:
            raise ValueError("mail requires an IMAP cursor checkpoint")
        parsed = MailCheckpoint.parse(checkpoint.token)
        pending = self._pending_checkpoints.get(checkpoint.token)
        if (
            pending is None
            or pending != (custody.envelope_digest, instance.instance_id, instance.generation)
            or not self._checkpoint_order
            or self._checkpoint_order[0] != checkpoint.token
        ):
            raise ValueError("mail checkpoint custody does not match the pending observation")
        if (
            parsed.mailbox_scope != self._config.mailbox_scope
            or parsed.folder != self._config.folder
            or parsed.uidvalidity != uidvalidity
            or parsed.uid < self._next_uid
        ):
            raise ValueError("mail checkpoint does not match the active namespace")
        if self._rollover_epoch_id is not None and self._rollover_scan_end is not None:
            if parsed.uid <= self._rollover_scan_end:
                raw_ref = f"sealed:mail:{instance.instance_id}:{uidvalidity}:{parsed.uid}"
                raw = self._raw.get(raw_ref)
                if raw is None:
                    raise ValueError("mail checkpoint custody raw observation is unavailable")
                message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
                message_id = str(message.get("Message-ID")) if message.get("Message-ID") else None
                if self._state_store is None:  # pragma: no cover - set with rollover id
                    raise RuntimeError("mail rollover state store is unavailable")
                self._state_store.advance_rollover(
                    epoch_id=self._rollover_epoch_id,
                    uid=parsed.uid,
                    message_id=message_id,
                    raw_source_ref=raw_ref,
                )
            else:
                if self._state_store is None:  # pragma: no cover - set with rollover id
                    raise RuntimeError("mail rollover state store is unavailable")
                self._state_store.advance_rollover_tail(
                    epoch_id=self._rollover_epoch_id, uid=parsed.uid
                )
        elif self._state_store is not None:
            self._state_store.advance_active_checkpoint(
                mailbox_scope=parsed.mailbox_scope,
                folder=parsed.folder,
                uidvalidity=parsed.uidvalidity,
                uid=parsed.uid,
            )
        await self._ledger.commit(
            instance.instance_id, instance.generation, self._config.folder, parsed
        )
        self._pending_checkpoints.pop(checkpoint.token, None)
        self._checkpoint_order.pop(0)
        self._next_uid = parsed.uid + 1

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        if envelope.observation.raw_safe_ref not in self._raw:
            raise MailRuntimeBlockedError("MAIL_RAW_SOURCE_UNAVAILABLE")
        return HydratedEvent(envelope, envelope.payload)

    def raw_message(self, raw_source_ref: str) -> bytes:
        try:
            return self._raw[raw_source_ref]
        except KeyError:
            raise MailRuntimeBlockedError("MAIL_RAW_SOURCE_UNAVAILABLE") from None

    def fetch_evidence(self, raw_source_ref: str) -> MailFetchEvidence:
        try:
            return self._fetch_evidence[raw_source_ref]
        except KeyError:
            raise MailRuntimeBlockedError("MAIL_FETCH_EVIDENCE_UNAVAILABLE") from None

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            self._state,
            "mail-imap-smtp-v1",
            len(self._pending),
            0,
            self._reason_codes,
        )

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        del deadline_ms
        retained = len(self._pending)
        if self._client is not None:
            await asyncio.to_thread(self._client.logout)
        self._client = None
        self._state = "stopped"
        return AdapterCloseOutcome(
            "closed" if retained == 0 else "retained-pending", retained, 0,
            None if retained == 0 else "MAIL_PENDING_UIDS_RETAINED",
        )

    def _require_online(self) -> tuple[AdapterInstanceRef, object, int]:
        if self._instance is None or self._client is None or self._uidvalidity is None:
            raise MailRuntimeBlockedError("MAIL_RUNTIME_NOT_ONLINE")
        return self._instance, self._client, self._uidvalidity


__all__ = ["MailImapRuntime", "MailRuntimeBlockedError"]
