from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AudienceSnapshot,
    ChatContext,
    ChatMessage,
    ChatRef,
    ChatReplyIntent,
    ChatSendIntent,
    FrozenSyncTargetSetV1,
    IntentCapability,
    IntentPayload,
    LarkProvenanceV1,
    MirrorLineage,
    NativeAlias,
    MailReplyIntent,
    MailSendIntent,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    PrincipalRef,
    SourceProvenance,
    SyncPolicySnapshotV1,
    TextBlock,
)


def _payload() -> IntentPayload:
    return IntentPayload("intent-1", (TextBlock("text", "hello"),), (), "idem-1")


def _policy() -> SyncPolicySnapshotV1:
    return SyncPolicySnapshotV1(
        "hyprial.chat.sync-policy/v1",
        "policy-1",
        "org-1",
        "channel-1",
        1,
        "corresponding",
        "corresponding",
        "corresponding",
        "exact",
        "corresponding",
        (("corr-1", 2),),
        (("instance-1", 3),),
        10,
        "digest-1",
    )


@pytest.mark.parametrize(
    "value",
    [
        ChatReplyIntent,
        MailReplyIntent,
    ],
)
def test_reply_intents_enforce_mode_sync_and_parent_matrix(value: type[object]) -> None:
    schema = (
        "hyprial.chat.intent.chat-reply/v1"
        if value is ChatReplyIntent
        else "hyprial.chat.intent.mail-reply/v1"
    )
    with pytest.raises(ValueError):
        value(schema, _payload(), "parent-1", "bogus", "inherit")
    with pytest.raises(ValueError):
        value(schema, _payload(), "parent-1", "reply-all", "bogus")


def test_send_intents_require_opaque_refs_and_forbid_native_or_email_values() -> None:
    ChatSendIntent(
        "hyprial.chat.intent.chat-send/v1",
        _payload(),
        None,
        "explicit-chats",
        ("chat-ref:v1:group-1",),
        "exact",
    )
    MailSendIntent(
        "hyprial.chat.intent.mail-send/v1",
        _payload(),
        None,
        "explicit-recipients",
        ("recipient-ref:v1:user-1",),
        "exact",
    )
    with pytest.raises(ValueError):
        ChatSendIntent(
            "hyprial.chat.intent.chat-send/v1",
            _payload(),
            None,
            "explicit-chats",
            ("oc_native",),
            "exact",
        )
    with pytest.raises(ValueError):
        MailSendIntent(
            "hyprial.chat.intent.mail-send/v1",
            _payload(),
            None,
            "explicit-recipients",
            ("user@example.invalid",),
            "exact",
        )


def test_nested_value_unions_validate_literals_and_unique_principals() -> None:
    instance = AdapterInstanceRef("instance-1", "lark", "bot", "main", "org-1", "fp", 1)
    with pytest.raises(ValueError):
        ChatRef("chat-ref:v1:one", instance, "acct", "native", cast(object, "broadcast"), None, None)
    principal = PrincipalRef("subject-1", None, None, None, None, "unresolved", None, None, None, None)
    with pytest.raises(ValueError):
        replace(principal, resolution=cast(object, "trusted"))
    with pytest.raises(ValueError):
        AudienceSnapshot("group", (principal, principal), "complete", "corr-1", 1)


def test_sync_policy_requires_closed_modes_unique_positive_generations() -> None:
    with pytest.raises(ValueError):
        replace(_policy(), human_chat_input=cast(object, "broadcast"))
    with pytest.raises(ValueError):
        replace(_policy(), correspondence_versions=(("corr-1", 1), ("corr-1", 2)))
    with pytest.raises(ValueError):
        replace(_policy(), binding_generations=(("instance-1", 0),))


def test_intent_capability_and_context_are_runtime_closed() -> None:
    with pytest.raises(TypeError):
        IntentCapability("chat.reply.source", ("inherit",), 1)  # type: ignore[arg-type]
    instance = AdapterInstanceRef("instance-1", "lark", "bot", "main", "org-1", "fp", 1)
    principal = PrincipalRef(
        "subject-1", None, None, None, None, "unresolved", None, None, None, None
    )
    audience = AudienceSnapshot("private", (principal,), "complete", "corr-1", 1)
    chat = ChatRef("chat-ref:v1:one", instance, "acct", "native", "dm", None, None)
    with pytest.raises(TypeError):
        ChatContext(
            "channel-1",
            chat,
            None,
            principal,
            audience,
            (),
            ("chat.reply.source",),  # type: ignore[arg-type]
            "cap-1",
        )


def test_chat_message_requires_typed_sync_policy_snapshot() -> None:
    instance = AdapterInstanceRef("instance-1", "lark", "bot", "main", "org-1", "fp", 1)
    principal = PrincipalRef(
        "subject-1", None, None, None, None, "unresolved", None, None, None, None
    )
    context = ChatContext(
        "channel-1",
        ChatRef("chat-ref:v1:one", instance, "acct", "native", "dm", None, None),
        None,
        principal,
        AudienceSnapshot("private", (principal,), "complete", "corr-1", 1),
        (),
        (IntentCapability("chat.reply.source", ("inherit",), True),),
        "cap-1",
    )
    platform = LarkProvenanceV1(
        "hyprial.chat.platform.lark/v1",
        "lark",
        PlatformScopeV1("org-1", "instance-1", 1, "acct", "test"),
        PlatformRefsV1(event_id="evt-1", message_id="om-1"),
        PlatformUnknownV1({}, "raw-source-ref:test"),
        "evt-1",
        "om-1",
        "oc-1",
        "dm",
        None,
        None,
        None,
        "ou-1",
        "user",
        None,
        None,
        principal,
        (),
        None,
        1,
        "text",
        "available",
    )
    message = ChatMessage(
        "event-1",
        1,
        context,
        SourceProvenance(
            "ingress-1",
            ("observation-1",),
            (NativeAlias("instance-1", 1, "acct", "message", "create", "event", "evt-1", "v1"),),
            1,
            None,
            "native-human",
            "ou-1",
        ),
        platform,
        MirrorLineage(None, None, None, 0),
        (),
        (),
        None,
        _policy(),
        "admission-1",
    )
    with pytest.raises(TypeError):
        replace(message, sync_policy_snapshot=object())


def test_frozen_sync_target_set_enforces_event_matrix_and_no_audience_expansion() -> None:
    with pytest.raises(ValueError):
        FrozenSyncTargetSetV1(
            _policy(),
            "inherit",
            "corresponding",
            "mail-send-explicit",
            ("recipient-ref:v1:a",),
            ("recipient-ref:v1:a",),
            (),
            (),
            "audience-1",
        )
    with pytest.raises(ValueError):
        FrozenSyncTargetSetV1(
            _policy(),
            "exact",
            "exact",
            "chat-send-explicit",
            ("chat-ref:v1:a",),
            ("chat-ref:v1:a", "chat-ref:v1:expanded"),
            (),
            (),
            "audience-1",
        )


@pytest.mark.parametrize(
    ("event_class", "effective"),
    [
        ("human-chat-input", "exact"),
        ("human-mail-input", "exact"),
        ("chat-reply-source", "exact"),
        ("chat-reply-private", "corresponding"),
        ("mail-reply", "exact"),
    ],
)
def test_inherit_effective_mode_must_come_from_frozen_event_policy(
    event_class: str, effective: str
) -> None:
    with pytest.raises(ValueError):
        FrozenSyncTargetSetV1(
            _policy(),
            "inherit",
            effective,  # type: ignore[arg-type]
            event_class,  # type: ignore[arg-type]
            ("chat-ref:v1:a",),
            ("chat-ref:v1:a",),
            (),
            (),
            "audience-1",
        )
