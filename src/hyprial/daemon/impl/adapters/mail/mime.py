"""MIME normalization with envelope-first recipient privacy."""

from __future__ import annotations

import hashlib
import html
import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from ..contracts import (
    AdapterInstanceRef,
    AttachmentHandle,
    AudienceSnapshot,
    ChatContext,
    ChatRef,
    IntentCapability,
    MailAddressV1,
    MailEnvelopeV1,
    MailProvenanceV1,
    MailRecipientV1,
    NativeAlias,
    NormalizedIngress,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    PrincipalRef,
    ReplyReference,
    RichTextBlock,
    SourceProvenance,
    TextBlock,
)
from .models import MailConnectionConfig, MailFetchEvidence, canonical_address_key


_ANGLE = re.compile(r"^<(.+)>$")
_CAPABILITY_REVISION = "mail-imap-smtp-v1"


def _message_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _ANGLE.match(value.strip())
    return (match.group(1) if match else value.strip()) or None


def _message_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    found = re.findall(r"<([^<>]+)>", value)
    return tuple(found) if found else tuple(filter(None, value.split()))


def address_key(address: str) -> str:
    return canonical_address_key(address)


def _address(display_name: str, address: str) -> MailAddressV1:
    key = address_key(address)
    return MailAddressV1(key, display_name or None, None)


def _sender(address: MailAddressV1) -> PrincipalRef:
    return PrincipalRef(
        address.address,
        None,
        None,
        address.display_name,
        None,
        "unresolved",
        None,
        None,
        None,
        "mail-header",
    )


def _body(message: Message) -> tuple[str, str | None]:
    plain: str | None = None
    rich: str | None = None
    for part in message.walk():
        disposition = part.get_content_disposition()
        if disposition == "attachment":
            continue
        media_type = part.get_content_type()
        if media_type not in ("text/plain", "text/html"):
            continue
        value = part.get_content()
        if not isinstance(value, str):
            continue
        if media_type == "text/plain" and plain is None:
            plain = value.strip()
        elif media_type == "text/html" and rich is None:
            rich = value.strip()
    if not message.is_multipart():
        value = message.get_content()
        if isinstance(value, str):
            if message.get_content_type() == "text/html":
                rich = value.strip()
            else:
                plain = value.strip()
    fallback = plain or re.sub(r"<[^>]+>", " ", html.unescape(rich or ""))
    return re.sub(r"\s+", " ", fallback).strip(), rich


class MailMessageCodec:
    def __init__(
        self,
        instance: AdapterInstanceRef,
        config: MailConnectionConfig,
        *,
        self_addresses: tuple[str, ...],
    ) -> None:
        if instance.kind != "mail" or instance.variant != "imap-smtp":
            raise ValueError("mail codec requires mail/imap-smtp instance")
        self._instance = instance
        self._mailbox_scope = config.mailbox_scope
        self._folder = config.folder
        self._authenticated_mailbox_id = address_key(config.authenticated_mailbox_id)
        self._self_keys = frozenset(address_key(item) for item in self_addresses)
        if self._authenticated_mailbox_id not in self._self_keys:
            raise ValueError("authenticated mailbox must be one of the verified self addresses")

    def decode_message(
        self,
        raw: bytes,
        *,
        fetch_evidence: MailFetchEvidence,
        raw_source_ref: str,
        received_at_ms: int,
    ) -> tuple[NormalizedIngress, MailProvenanceV1]:
        if (
            fetch_evidence.authenticated_mailbox_id != self._authenticated_mailbox_id
            or fetch_evidence.mailbox_scope != self._mailbox_scope
            or fetch_evidence.folder != self._folder
        ):
            raise ValueError("mail fetch evidence authenticated mailbox does not match the codec")
        if not fetch_evidence.matches(raw):
            raise ValueError("mail fetch evidence does not match the raw message")
        mailbox_scope = fetch_evidence.mailbox_scope
        folder = fetch_evidence.folder
        uidvalidity = fetch_evidence.uidvalidity
        uid = fetch_evidence.uid
        message = BytesParser(policy=policy.default).parsebytes(raw)
        from_rows = getaddresses(message.get_all("From", []))
        if len(from_rows) != 1 or not from_rows[0][1]:
            raise ValueError("mail requires exactly one header From address")
        from_address = _address(*from_rows[0])
        reply_rows = getaddresses(message.get_all("Reply-To", []))
        reply_to = _address(*reply_rows[0]) if len(reply_rows) == 1 and reply_rows[0][1] else None
        recipients: list[MailRecipientV1] = []
        seen: set[str] = set()
        for role, header in (("to", "To"), ("cc", "Cc")):
            for display, address in getaddresses(message.get_all(header, [])):
                if not address:
                    continue
                mail_address = _address(display, address)
                key = address_key(mail_address.address)
                if key in seen:
                    continue
                seen.add(key)
                recipients.append(
                    MailRecipientV1(
                        f"mail-recipient:{hashlib.sha256(key.encode()).hexdigest()}",
                        mail_address,
                        role,
                        key,
                        True,
                        None,
                    )
                )
        message_id = _message_id(message.get("Message-ID"))
        in_reply_to = _message_ids(message.get("In-Reply-To"))
        references = _message_ids(message.get("References"))
        mailbox_visible = not self._self_keys.isdisjoint(seen)
        envelope = MailEnvelopeV1(
            "hyprial.chat.platform.mail/v1",
            mailbox_scope,
            self._authenticated_mailbox_id,
            folder,
            uidvalidity,
            uid,
            message_id,
            in_reply_to,
            references,
            from_address,
            None,
            reply_to,
            tuple(recipients),
            "none" if mailbox_visible else "self-only",
        )
        sender = _sender(from_address)
        native_id = message_id or f"{uidvalidity}:{uid}"
        chat_ref = ChatRef(
            f"chat-ref:v1:mail:{self._instance.instance_id}:{uidvalidity}:{uid}",
            self._instance,
            mailbox_scope,
            native_id,
            "mail",
            in_reply_to[-1] if in_reply_to else None,
            references[0] if references else None,
        )
        context = ChatContext(
            f"adapter:mail:{self._instance.instance_id}:{native_id}",
            chat_ref,
            str(message.get("Subject")) if message.get("Subject") else None,
            sender,
            AudienceSnapshot("mail-envelope", (sender,), "partial", None, 1),
            (),
            (
                IntentCapability("mail.reply.sender", ("inherit", "corresponding", "exact"), True),
                IntentCapability("mail.reply.reply-to", ("inherit", "corresponding", "exact"), True),
                IntentCapability("mail.reply.all", ("inherit", "corresponding", "exact"), True),
                IntentCapability("mail.send.explicit", ("exact",), False),
            ),
            _CAPABILITY_REVISION,
        )
        alias = NativeAlias(
            self._instance.instance_id,
            self._instance.generation,
            mailbox_scope,
            folder,
            "create",
            "cursor-item",
            f"{uidvalidity}:{uid}",
            "mail-imap-v1",
        )
        observation_id = f"mail:{self._instance.instance_id}:{uidvalidity}:{uid}"
        created_at = None
        try:
            date = parsedate_to_datetime(str(message.get("Date"))) if message.get("Date") else None
            created_at = int(date.timestamp() * 1000) if date else None
        except (TypeError, ValueError, OverflowError):
            created_at = None
        provenance = SourceProvenance(
            f"ingress:{observation_id}",
            (observation_id,),
            (alias,),
            received_at_ms,
            created_at,
            "native-human",
            from_address.address,
        )
        attachments: list[AttachmentHandle] = []
        for index, part in enumerate(message.iter_attachments()):
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                AttachmentHandle(
                    f"mail:{self._instance.instance_id}:{uidvalidity}:{uid}:{index}",
                    "file",
                    part.get_filename(),
                    part.get_content_type(),
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                    native_id,
                    None,
                )
            )
        plain, rich = _body(message)
        content = []
        if plain:
            content.append(TextBlock("text", plain))
        if rich:
            content.append(
                RichTextBlock(
                    "rich-text", ({"type": "html", "value": rich},), plain or "(HTML message)"
                )
            )
        reply = None
        if in_reply_to:
            reply = ReplyReference(None, in_reply_to[-1], None, (), (), None)
        platform = MailProvenanceV1(
            "hyprial.chat.platform.mail/v1",
            PlatformScopeV1(
                self._instance.organization_id,
                self._instance.instance_id,
                self._instance.generation,
                mailbox_scope,
                "imap-smtp",
            ),
            PlatformRefsV1(
                event_id=f"{uidvalidity}:{uid}",
                message_id=message_id,
                reply_id=in_reply_to[-1] if in_reply_to else None,
                thread_id=references[0] if references else None,
                attachment_refs=tuple(handle.handle_id for handle in attachments),
            ),
            PlatformUnknownV1({}, raw_source_ref),
            envelope,
        )
        return NormalizedIngress(context, provenance, tuple(content), tuple(attachments), reply), platform


__all__ = ["MailMessageCodec", "address_key"]
