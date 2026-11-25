"""Conversation record contracts: cell ids, entry keys, refs and the line format."""

from __future__ import annotations

import hashlib
import json

import pytest

from hyprial.daemon.impl.conversations import (
    RecordDraft,
    RecordEntry,
    RecordRef,
    SegmentStore,
    conversation_attribution,
    conversation_cell_id,
    conversation_key,
    conversation_pair_key,
    entry_key,
    no_redaction,
)
from hyprial.daemon.impl.conversations.record.contracts import pair_endpoint
from hyprial.daemon.impl.delivery.ports import BODY_REF_MAX_CHARS, BodyGrant
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.conversations.keys import direct_key
from hyprial.kernel import canonical_agent_uri, is_thread_id_segment, keyed_cell_id

ALPHA = canonical_agent_uri("op", "node-1", "alpha")
BETA = canonical_agent_uri("op", "node-1", "beta")
THREAD = direct_key(ALPHA, BETA).id
SHA = hashlib.sha256(b"body").hexdigest()


def _entry(body: bytes = b'{"message":"hi"}', **changes: object) -> RecordEntry:
    fields: dict[str, object] = {
        "key": entry_key("inbox", 7, hashlib.sha256(body).hexdigest()),
        "source": "inbox",
        "position": 7,
        "message_id": "m-1",
        "conversation_id": "conv-1",
        "thread_id": THREAD,
        "cell_id": conversation_cell_id("user:op", THREAD, "conv-1"),
        "sender": ALPHA,
        "recipient": BETA,
        "intent": "request",
        "created_at_ms": 1_000,
        "recorded_at_ms": 2_000,
        "payload_sha256": hashlib.sha256(body).hexdigest(),
        "body": body,
        "redacted": False,
    }
    fields.update(changes)
    return RecordEntry(**fields)  # type: ignore[arg-type]


def test_the_conversation_key_joins_thread_and_legacy_conversation_id() -> None:
    assert conversation_key(THREAD, "conv-1") == f"{THREAD}\0conv-1"
    assert conversation_key(THREAD, None) == f"{THREAD}\0"
    assert conversation_key(THREAD, "") == f"{THREAD}\0"


def test_the_conversation_cell_id_is_keyed_under_the_local_owner() -> None:
    expected = keyed_cell_id("conversation", "user:op", f"{THREAD}\0conv-1")
    assert conversation_cell_id("user:op", THREAD, "conv-1") == expected
    assert conversation_cell_id("user:op", THREAD, None) != expected
    assert conversation_cell_id("user:other", THREAD, "conv-1") != expected


def test_the_id_path_and_line_are_pinned_to_the_values_before_the_storage_split() -> None:
    """Literal values computed on dev while ``conversation`` was still a kind.

    The demotion of ``conversation`` to a storage category must not move one
    byte of the derived id, the segment path or the entry line.
    """

    authority = "user:op"
    thread = "t.pcemxskk5bbr2ehimzv5ibfgyi"
    cell = conversation_cell_id(authority, thread, "conversation-1")
    assert cell == "c.oaaogrvkivlie6ggdg34u6gbsu"
    store = SegmentStore(fs=lambda: None, space_for=lambda _a: None, writer="node-1")
    assert store.directory(cell) == f"cells/conversation/{cell}/rec-node-1"
    assert store.segment_path(cell, 0) == f"cells/conversation/{cell}/rec-node-1/000000.jsonl"
    sha = "a" * 64
    entry = RecordEntry(
        key=entry_key("inbox", 3, sha),
        source="inbox",
        position=3,
        message_id="m-1",
        conversation_id="conversation-1",
        thread_id=thread,
        cell_id=cell,
        sender="agent:op:node-1:driver",
        recipient="agent:op:node-1:reader",
        intent="request",
        created_at_ms=1_000,
        recorded_at_ms=1_700_000_000_000,
        payload_sha256=sha,
        body=b'{"message":"m-1"}',
    )
    assert entry.to_line() == (
        '{"body":"eyJtZXNzYWdlIjoibS0xIn0="'
        f',"cellId":"{cell}","conversationId":"conversation-1","createdAtMs":1000'
        ',"format":"hyprial-conversation-record/v1","intent":"request"'
        f',"key":"inbox:3:{sha}","messageClass":"interaction","messageId":"m-1"'
        f',"payloadSha256":"{sha}","position":3,"recipient":"agent:op:node-1:reader"'
        ',"recordedAtMs":1700000000000,"redacted":false'
        ',"sender":"agent:op:node-1:driver","source":"inbox"'
        f',"threadId":"{thread}"}}\n'
    )


def test_the_entry_key_names_source_position_and_payload_digest() -> None:
    assert entry_key("inbox", 3, SHA) == f"inbox:3:{SHA}"
    assert entry_key("sender_copies", 3, SHA) != entry_key("inbox", 3, SHA)


def test_a_ref_round_trips_and_fits_a_body_grant() -> None:
    ref = RecordRef(
        space_id="de1ede5a-50c2-46a7-b4a6-67111921f1a3",
        path="cells/conversation/c.x/rec-node-1/000000.jsonl",
        key=entry_key("inbox", 3, SHA),
    )
    text = ref.encode()
    assert RecordRef.parse(text) == ref
    assert len(text) <= BODY_REF_MAX_CHARS
    BodyGrant("inbox", 3, SHA, text)
    for garbage in ("", "rec1", "other a b c", "rec1 a b", "rec1 a b c d", None, 3):
        assert RecordRef.parse(garbage) is None  # type: ignore[arg-type]


def test_an_entry_line_is_one_json_line_and_keeps_bytes_exactly() -> None:
    for body in (b'{"message":"hi"}', b"\xff\x00\n raw", "日本 ".encode()):
        entry = _entry(body)
        line = entry.to_line()
        assert line.endswith("\n") and line.count("\n") == 1
        assert RecordEntry.from_line(line) == entry
        assert RecordEntry.from_line(line).body == body


def test_a_malformed_line_decodes_to_none() -> None:
    for line in ("", "{", "[]", '{"format":"other"}', '{"format":"hyprial-conversation-record/v1"}'):
        assert RecordEntry.from_line(line) is None


def test_the_default_redaction_keeps_every_draft_unchanged() -> None:
    message = InboxMessage(
        message_id="m-1",
        conversation_id="conv-1",
        sender=ALPHA,
        recipient=BETA,
        payload=b'{"message":"hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    draft = RecordDraft(
        source="inbox",
        position=1,
        payload_sha256=hashlib.sha256(message.payload).hexdigest(),
        message=message,
        thread_id=THREAD,
        conversation_id="conv-1",
        body=message.payload,
    )
    assert no_redaction(draft) is draft


COORDINATOR = canonical_agent_uri("op", "node-1", "dispatch-coordinator")


def test_the_pair_key_is_unordered_canonical_and_never_a_thread_id() -> None:
    """Plan slice S1: the attribution of ends that have no send-link thread."""

    key = conversation_pair_key(COORDINATOR, BETA)
    assert key is not None and key.startswith("p.") and len(key) == 28
    assert not is_thread_id_segment(key)
    assert conversation_pair_key(BETA, COORDINATOR) == key
    assert conversation_pair_key(ALPHA, BETA) not in (None, key)
    lark = conversation_pair_key("adapter:lark:golden", BETA)
    assert lark is not None
    assert conversation_pair_key("channel:lark:golden", BETA) is None
    assert conversation_pair_key("channel:op:node-1:golden", BETA) is None
    assert conversation_pair_key("user:op", "user:other") is not None
    for unaddressed in ("system:hyprial:node-1", "beta", "", "adapter:lark", "route:x"):
        assert conversation_pair_key(unaddressed, BETA) is None


_OU = "u.ou_" + "b" * 32
_DEV = "d." + "c" * 32
_AID = "a." + "a" * 26

# Outputs pinned from the hand-split implementation before pair_endpoint moved
# onto the kernel's address readers; every pair key and cell id derives from them.
_PAIR_ENDPOINTS: tuple[tuple[object, str | None], ...] = (
    ("agent:alice:mac:bot", "agent:alice:mac:bot"),
    (f"agent:op:node-1:{_AID}", f"agent:op:node-1:{_AID}"),
    (f"agent:{_OU}:{_DEV}:worker", f"agent:{_OU}:{_DEV}:worker"),
    (f"agent:{_OU}:{_DEV}:{_AID}", f"agent:{_OU}:{_DEV}:{_AID}"),
    ("user:alice", "user:alice"),
    (f"user:{_OU}", f"user:{_OU}"),
    ("route:lark:room-1", None),
    ("route:lark", None),
    ("adapter:alice:mac:lark", "adapter:alice:mac:lark"),
    ("channel:alice:mac:lark", None),
    ("adapter:lark:main", "adapter:lark:main"),
    ("channel:lark:main", None),
    ("adapter:lark:", None),
    ("adapter::main", None),
    ("adapter:lark", None),
    ("adapter:lark:a:b", "adapter:lark:a:b"),
    ("adapter:lark:a:b:c", None),
    ("channel:lark:main:", None),
    ("system:hyprial:node-1", None),
    ("system:daemon", None),
    ("bare-name", None),
    ("", None),
    ("adapter:", None),
    ("agent:", None),
    ("agent:a:b", None),
    ("user:", None),
    ("route:", None),
    ("::", None),
    ("x:y:z", None),
    (None, None),
    (5, None),
)


@pytest.mark.parametrize(("address", "expected"), _PAIR_ENDPOINTS)
def test_pair_endpoint_spells_each_address_form_as_it_always_did(
    address: object, expected: str | None
) -> None:
    assert pair_endpoint(address) == expected


def test_attribution_prefers_the_thread_then_the_pair() -> None:
    assert conversation_attribution(THREAD, ALPHA, BETA) == THREAD
    assert conversation_attribution(None, COORDINATOR, BETA) == (
        conversation_pair_key(COORDINATOR, BETA)
    )
    assert conversation_attribution(None, "system:hyprial:node-1", BETA) is None


def test_an_entry_carries_its_class_and_a_v1_line_without_one_reads_as_interaction() -> None:
    system = _entry(message_class="system")
    assert system.to_json()["messageClass"] == "system"
    assert system.to_json()["format"] == "hyprial-conversation-record/v1"
    assert RecordEntry.from_line(system.to_line()) == system

    document = _entry().to_json()
    del document["messageClass"]
    older = json.dumps(document, separators=(",", ":"), sort_keys=True)
    decoded = RecordEntry.from_line(older)
    assert decoded is not None and decoded.message_class == "interaction"
    assert decoded == _entry()
    document["messageClass"] = 7
    assert RecordEntry.from_line(json.dumps(document)) is None
