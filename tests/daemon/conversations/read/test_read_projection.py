"""S6-read: the pure conversation read projection (heads, list cursor, thread walk)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hyprial.daemon.impl.conversations import (
    ConversationHead,
    conversation_heads,
    decode_list_cursor,
    encode_list_cursor,
    merge_heads,
    merge_queue_threads,
    page_heads,
    queue_threads,
    row_conversation,
    row_thread,
    thread_filtered,
)
from hyprial.daemon.impl.conversations.record.contracts import (
    conversation_cell_id,
    conversation_pair_key,
)
from hyprial.daemon.impl.conversations import direct_key

OWNER = "user:op"
A = "agent:op:n1:alpha"
B = "agent:op:n1:beta"
C = "agent:op:n1:gamma"


@dataclass(frozen=True)
class Group:
    conversation_id: str
    sender: str
    recipient: str
    last_created_at_ms: int
    last_message_id: str
    messages: int
    thread_id: str | None = None
    message_class: str | None = None


@dataclass(frozen=True)
class Row:
    created_at_ms: int
    message_id: str
    sender: str
    recipient: str
    thread_id: str | None = None


def _heads(groups: list[Group], participant: str = A) -> tuple[ConversationHead, ...]:
    return conversation_heads(
        groups, participant=participant, authority=OWNER, thread_key=direct_key
    )


def test_both_directions_of_one_pair_fold_into_one_conversation() -> None:
    heads = _heads(
        [
            Group("conv-1", A, B, 10, "m1", 2),
            Group("conv-1", B, A, 20, "m2", 3),
        ]
    )
    key = direct_key(A, B)
    assert key is not None
    assert heads == (
        ConversationHead(
            conversation_id=conversation_cell_id(OWNER, key.id, "conv-1"),
            thread_id=key.id,
            kind="agent",
            peer=B,
            legacy_conversation_id="conv-1",
            last_message_at_ms=20,
            last_message_id="m2",
            messages=5,
        ),
    )


def test_one_legacy_id_with_two_counterparts_is_two_conversations() -> None:
    heads = _heads([Group("shared", A, B, 10, "m1", 1), Group("shared", C, A, 30, "m3", 1)])
    assert [head.peer for head in heads] == [C, B]
    assert len({head.conversation_id for head in heads}) == 2
    assert {head.legacy_conversation_id for head in heads} == {"shared"}


def test_pairs_with_an_unaddressed_end_are_not_conversations() -> None:
    heads = _heads(
        [
            Group("notice", "system:hyprial:n1", A, 10, "s1", 1),
            Group("bare", A, "beta", 11, "b1", 1),
            Group("talk", A, B, 12, "t1", 1),
        ]
    )
    assert [head.legacy_conversation_id for head in heads] == ["talk"]


def test_user_and_adapter_threads_name_the_peer_from_either_side() -> None:
    user_heads = _heads([Group("dm", OWNER, A, 5, "u1", 1)])
    assert [(head.kind, head.peer) for head in user_heads] == [("user", OWNER)]
    operator_heads = _heads([Group("dm", OWNER, A, 5, "u1", 1)], participant=OWNER)
    assert [(head.kind, head.peer) for head in operator_heads] == [("user", A)]
    adapter_heads = _heads([Group("chat", "adapter:op:node-1:lark", A, 7, "r1", 1)])
    assert [(head.kind, head.peer) for head in adapter_heads] == [
        ("adapter", "adapter:op:node-1:lark")
    ]
    assert _heads([Group("chat", "route:lark:group", A, 7, "r2", 1)]) == ()


def test_a_stamped_route_peer_conversation_still_lists_with_no_kind() -> None:
    stamped = Group("chat", "route:lark:group", A, 7, "r3", 1, thread_id="t.stampedroute")
    heads = _heads([stamped])
    assert [(head.thread_id, head.kind) for head in heads] == [("t.stampedroute", None)]
    assert heads[0].legacy_conversation_id == "chat"


def test_ties_on_time_take_the_greater_message_id_and_order_by_cell_id() -> None:
    heads = _heads(
        [
            Group("x", A, B, 10, "m-b", 1),
            Group("x", B, A, 10, "m-a", 1),
            Group("y", A, C, 10, "n-1", 1),
        ]
    )
    assert heads[0].last_message_at_ms == heads[1].last_message_at_ms == 10
    assert heads[0].conversation_id < heads[1].conversation_id
    assert {head.legacy_conversation_id: head.last_message_id for head in heads} == {
        "x": "m-b",
        "y": "n-1",
    }


def test_pages_walk_every_head_once_in_order() -> None:
    groups = [Group(f"c{index}", A, B, 100 - index % 3, f"m{index}", 1) for index in range(7)]
    heads = _heads(groups)
    seen: list[str] = []
    after = None
    while True:
        page, more = page_heads(heads, after=after, limit=3)
        seen.extend(head.conversation_id for head in page)
        if not more:
            break
        token = encode_list_cursor(page[-1], participant=A)
        after = decode_list_cursor(token, participant=A)
    assert seen == [head.conversation_id for head in heads]
    assert len(seen) == 7


def test_list_cursor_is_bound_to_participant_and_kinds() -> None:
    import base64
    import json

    head = _heads([Group("c", A, B, 1, "m", 1)])[0]
    token = encode_list_cursor(head, participant=A)
    assert decode_list_cursor(token, participant=A) == head.position
    with pytest.raises(ValueError):
        decode_list_cursor(token, participant=B)
    for thread_filter in (None, head.thread_id):
        legacy_payload = {
            "v": 1,
            "participant": A,
            "threadId": thread_filter,
            "lastMessageAtMs": head.last_message_at_ms,
            "conversationId": head.conversation_id,
        }
        legacy_token = base64.urlsafe_b64encode(
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        with pytest.raises(ValueError):
            decode_list_cursor(legacy_token, participant=A)
    for bogus in ("", "!!", "e30", 5, None, "x" * 5000):
        with pytest.raises(ValueError):
            decode_list_cursor(bogus, participant=A)


class _Source:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = sorted(rows, key=lambda row: (row.created_at_ms, row.message_id))
        self.calls = 0

    def __call__(self, *, after=None, before=None, limit, newest_first=False):
        self.calls += 1
        rows = self.rows
        if after is not None:
            rows = [r for r in rows if (r.created_at_ms, r.message_id) > after]
        if before is not None:
            rows = [r for r in rows if (r.created_at_ms, r.message_id) < before]
        if newest_first:
            rows = list(reversed(rows))
        return tuple(rows[:limit])


def _mixed() -> list[Row]:
    rows = []
    for index in range(10):
        rows.append(Row(index, f"ab{index:02d}", A if index % 2 else B, B if index % 2 else A))
        rows.append(Row(index, f"ac{index:02d}", A, C))
    return rows


def test_thread_walk_skips_other_threads_across_batches() -> None:
    key = direct_key(A, B)
    assert key is not None
    source = _Source(_mixed())
    walk = thread_filtered(source, thread_id=key.id, thread_key=direct_key, batch=3)
    forward = walk(limit=4)
    assert [row.message_id for row in forward] == ["ab00", "ab01", "ab02", "ab03"]
    resumed = walk(after=(forward[-1].created_at_ms, forward[-1].message_id), limit=100)
    assert [row.message_id for row in resumed] == [f"ab{i:02d}" for i in range(4, 10)]
    newest = walk(limit=2, newest_first=True)
    assert [row.message_id for row in newest] == ["ab09", "ab08"]
    older = walk(before=(8, "ab08"), limit=3, newest_first=True)
    assert [row.message_id for row in older] == ["ab07", "ab06", "ab05"]
    assert source.calls > 4


def test_thread_walk_refuses_a_bound_against_its_direction() -> None:
    walk = thread_filtered(_Source([]), thread_id="t.x", thread_key=direct_key, batch=2)
    with pytest.raises(ValueError):
        walk(after=(1, "m"), limit=1, newest_first=True)
    with pytest.raises(ValueError):
        walk(before=(1, "m"), limit=1)
    with pytest.raises(ValueError):
        thread_filtered(_Source([]), thread_id="t.x", thread_key=direct_key, batch=0)


def test_head_json_names_every_field() -> None:
    head = _heads([Group("c", A, B, 1, "m", 2)])[0]
    assert head.to_json() == {
        "conversationId": head.conversation_id,
        "kind": "agent",
        "peer": B,
        "legacyConversationId": "c",
        "lastMessageAtMs": 1,
        "lastMessageId": "m",
        "messages": 2,
        "messageClass": None,
    }


STAMP = "t." + "z" * 26


def test_a_stored_thread_wins_over_the_inferred_one_like_the_recorder() -> None:
    inferred = direct_key(A, B)
    assert inferred is not None
    assert row_thread(A, B, None, direct_key) == (inferred.id, inferred)
    assert row_thread(A, B, inferred.id, direct_key) == (inferred.id, inferred)
    assert row_thread(A, B, STAMP, direct_key) == (STAMP, None)
    assert row_thread("system:hyprial:n1", A, STAMP, direct_key) == (STAMP, None)
    assert row_thread("system:hyprial:n1", A, None, direct_key) == (None, None)


def test_a_stamped_row_gets_its_own_cell_and_never_folds_into_the_inferred_one() -> None:
    inferred = direct_key(A, B)
    assert inferred is not None
    heads = _heads(
        [
            Group("conv", A, B, 10, "m1", 2),
            Group("conv", B, A, 20, "m2", 1, thread_id=STAMP),
        ]
    )
    by_thread = {head.thread_id: head for head in heads}
    assert set(by_thread) == {inferred.id, STAMP}
    stamped = by_thread[STAMP]
    assert stamped.conversation_id == conversation_cell_id(OWNER, STAMP, "conv")
    assert (stamped.kind, stamped.peer, stamped.messages) == (None, B, 1)
    plain = by_thread[inferred.id]
    assert (plain.kind, plain.peer, plain.messages) == ("agent", B, 2)


def test_a_stamped_id_another_row_infers_takes_that_rows_key() -> None:
    user = direct_key(OWNER, A)
    assert user is not None
    heads = _heads(
        [
            Group("dm", OWNER, A, 5, "u1", 1),
            Group("dm", A, "user:other", 9, "u2", 1, thread_id=user.id),
        ]
    )
    assert len(heads) == 1
    head = heads[0]
    assert (head.thread_id, head.kind, head.peer, head.messages) == (
        user.id, "user", OWNER, 2,
    )
    threads = queue_threads(
        [Group("dm", A, "user:other", 9, "u2", 1, thread_id=user.id)],
        participant=A,
        thread_key=direct_key,
    )
    assert threads[user.id].key is None
    assert threads[user.id].peer == "user:other"


def test_queue_threads_name_every_thread_once_with_the_newest_peer() -> None:
    ab = direct_key(A, B)
    assert ab is not None
    threads = queue_threads(
        [
            Group("x", A, B, 10, "m1", 1),
            Group("y", B, A, 30, "m3", 1),
            Group("z", A, C, 20, "m2", 1, thread_id=STAMP),
            Group("n", "system:hyprial:n1", A, 40, "s1", 1),
        ],
        participant=A,
        thread_key=direct_key,
    )
    assert list(threads) == sorted(threads)
    assert threads[ab.id].key == ab and threads[ab.id].peer == B
    assert threads[STAMP].key is None and threads[STAMP].peer == C
    assert len(threads) == 2


def test_thread_walk_follows_the_stored_thread() -> None:
    inferred = direct_key(A, B)
    assert inferred is not None
    rows = [
        Row(1, "m1", A, B),
        Row(2, "m2", B, A, thread_id=STAMP),
        Row(3, "m3", A, B, thread_id=inferred.id),
    ]
    plain = thread_filtered(_Source(rows), thread_id=inferred.id, thread_key=direct_key, batch=2)
    assert [row.message_id for row in plain(limit=10)] == ["m1", "m3"]
    stamped = thread_filtered(_Source(rows), thread_id=STAMP, thread_key=direct_key, batch=2)
    assert [row.message_id for row in stamped(limit=10)] == ["m2"]


def test_heads_of_several_read_keys_merge_into_one_newest_first_list() -> None:
    """Release D9: one reader's heads under its own key and a frozen name key."""

    frozen = "agent:op:n1:old-alpha"
    own = _heads([Group("conv-new", A, B, 30, "m3", 1)])
    aliased = _heads([Group("conv-old", B, frozen, 10, "m1", 2)], participant=frozen)
    shared = _heads([Group("conv-new", A, B, 20, "m2", 4)])
    merged = merge_heads([own, aliased, shared])
    assert [head.legacy_conversation_id for head in merged] == ["conv-new", "conv-old"]
    assert merged[0].messages == 5 and merged[0].last_message_id == "m3"
    assert merged[1].peer == B


def test_queue_threads_of_several_read_keys_keep_the_first_known_key() -> None:
    frozen = "agent:op:n1:old-alpha"
    own = queue_threads(
        [Group("z", A, C, 20, "m2", 1, thread_id=STAMP)],
        participant=A,
        thread_key=direct_key,
    )
    aliased = queue_threads(
        [Group("y", B, frozen, 10, "m1", 1)], participant=frozen, thread_key=direct_key
    )
    merged = merge_queue_threads([own, aliased])
    assert list(merged) == sorted(merged)
    assert merged[STAMP].peer == C
    [old] = [thread for thread in merged.values() if thread.thread_id != STAMP]
    assert old.peer == B and old.key is not None


COORDINATOR = "agent:op:n1:dispatch-coordinator"
HOOKS = "agent:op:n1:_turn-hooks"
LARK = "adapter:lark:golden-lark"


def test_ends_without_a_thread_are_one_conversation_under_their_pair_key() -> None:
    """Plan slice S1: reserved actors and the three-segment adapter are listed."""

    heads = _heads(
        [
            Group("workflow", COORDINATOR, A, 10, "d1", 1, message_class="system"),
            Group("workflow", A, COORDINATOR, 20, "r1", 1, message_class="system"),
            Group("turn-hook:1", HOOKS, A, 30, "h1", 1, message_class="interaction"),
            Group("lark-1", LARK, A, 40, "l1", 1, message_class="interaction"),
            Group("lark-1", A, LARK, 50, "l2", 1),
        ]
    )
    by_legacy = {head.legacy_conversation_id: head for head in heads}
    assert set(by_legacy) == {"workflow", "turn-hook:1", "lark-1"}
    for legacy, peer, newest, messages, message_class in (
        ("workflow", COORDINATOR, "r1", 2, "system"),
        ("turn-hook:1", HOOKS, "h1", 1, "interaction"),
        ("lark-1", LARK, "l2", 2, None),
    ):
        head = by_legacy[legacy]
        pair = conversation_pair_key(A, peer)
        assert pair is not None and pair.startswith("p.")
        assert head.thread_id == pair
        assert head.conversation_id == conversation_cell_id(OWNER, pair, legacy)
        assert (head.kind, head.last_message_id, head.messages) == (None, newest, messages)
        assert head.message_class == message_class
        assert "threadId" not in head.to_json()
        assert head.to_json()["messageClass"] == message_class
    assert by_legacy["workflow"].peer == COORDINATOR
    assert by_legacy["lark-1"].peer == LARK


def test_a_threaded_pair_keeps_its_thread_and_never_takes_a_pair_key() -> None:
    inferred = direct_key(A, B)
    assert inferred is not None
    assert row_conversation(A, B, None, direct_key) == (inferred.id, inferred)
    assert row_conversation(A, B, STAMP, direct_key) == (STAMP, None)
    assert row_conversation(COORDINATOR, A, None, direct_key) == (
        conversation_pair_key(COORDINATOR, A), None,
    )
    assert row_conversation("system:hyprial:n1", A, None, direct_key) == (None, None)
    assert row_conversation(A, "beta", None, direct_key) == (None, None)


def test_queue_threads_stay_thread_only() -> None:
    """``threads.*`` read the queue threads; a pair key is not a send-link thread."""

    threads = queue_threads(
        [
            Group("workflow", COORDINATOR, A, 10, "d1", 1),
            Group("lark-1", LARK, A, 40, "l1", 1),
            Group("talk", A, B, 12, "t1", 1),
        ],
        participant=A,
        thread_key=direct_key,
    )
    ab = direct_key(A, B)
    assert ab is not None
    assert list(threads) == [ab.id]


def test_the_walk_follows_a_pair_key() -> None:
    rows = [
        Row(1, "d1", COORDINATOR, A),
        Row(2, "t1", A, B),
        Row(3, "r1", A, COORDINATOR),
        Row(4, "h1", HOOKS, A),
    ]
    pair = conversation_pair_key(A, COORDINATOR)
    assert pair is not None
    walk = thread_filtered(_Source(rows), thread_id=pair, thread_key=direct_key, batch=2)
    assert [row.message_id for row in walk(limit=10)] == ["d1", "r1"]
