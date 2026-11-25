"""Characterization of the conversation-key derivation (T1a step 0, 2026-11-20).

Every literal in this file was produced by the pre-change derivation —
threads.ids, threads.aliases, threads.service.official_agent_address and
conversations.record.contracts — at commit 4f53afe9b049adf6c8b5f0a59376d37e9940e1f4,
via ``uv run python generate_frozen_keys.py`` (worktree root; generator and
output are reproduced in REPORT.md).  The matrix follows plan §3.5.  These
values are the c. ids of existing conversations: any change to a tag, the
framing, the endpoint grammar or the keyed-cell formula must fail here
first (S3a, 2026-11-24, removed the owner binding and its pinned rows on
purpose: a ``user:<owner>`` counterpart is always its own conversation; I4a,
2026-11-24, retired the ``route:`` endpoint, so a route peer has no key and
only a stored stamp still files its rows), and after the derivation moves to conversations.keys
this file must keep passing byte for byte, one line unchanged.
"""

from __future__ import annotations


import pytest

from hyprial.daemon.impl.conversations.keys import (
    direct_key,
    direct_seed,
)
from hyprial.daemon.impl.conversations.record.contracts import (
    conversation_attribution,
    conversation_cell_id,
    conversation_key,
    conversation_pair_key,
)
from hyprial.kernel import (
    is_group_id_segment,
    is_thread_id_segment,
    mint_group_id,
    mint_thread_id,
)

ALPHA = "agent:op:node-1:alpha"
BETA = "agent:op:node-2:beta"
BETA_GOLDEN = "agent:op:node-1:beta"
ALPHA_ID = "agent:op:node-1:a.b4kuo3kxusjtp6ug25qzqycshe"
BETA_ID = "agent:op:node-2:a.xs3h24k2eippf7faqsx4qzxfdi"
USER_OP = "user:op"
USER_ANN = "user:ann"
ROUTE = "route:lark:ops"
ADAPTER4 = "adapter:op:node-1:lark"
CHANNEL4 = "channel:op:node-1:lark"
ADAPTER3 = "adapter:lark:golden-lark"
CHANNEL3 = "channel:lark:golden-lark"
DISPATCH = "agent:op:node-1:dispatch-coordinator"
HOOKS = "agent:op:node-1:_turn-hooks"
SYSTEM = "system:hyprial:node-1"
BARE = "alpha"
AUTHORITY = "user:op"
BOUND_THREAD = "t.cr73ph3yqiav6fkikqpv7occla"

ALICE_CHANNEL = "channel:alice:node:alice-squire"
T_ALPHA_BETA = "t.36d3esmiwzmpa5t65pgokfc5zy"

THREAD_MATRIX = [
    ("agent_pair_name_ab", (ALPHA, BETA)),
    ("agent_pair_name_ba", (BETA, ALPHA)),
    ("agent_pair_id_form", (ALPHA_ID, BETA_ID)),
    ("agent_pair_id_form_reversed", (BETA_ID, ALPHA_ID)),
    ("agent_route", (ALPHA, ROUTE)),
    ("agent_route_reversed", (ROUTE, ALPHA)),
    ("agent_adapter4", (ALPHA, ADAPTER4)),
    ("agent_adapter4_legacy_spelling", (ALPHA, CHANNEL4)),
    ("agent_user_unbound", (ALPHA, USER_OP)),
    ("agent_user_unbound_reversed", (USER_OP, ALPHA)),
    ("reserved_dispatch_agent", (DISPATCH, BETA)),
    ("reserved_hooks_agent", (HOOKS, ALPHA)),
    ("adapter3_agent", (ADAPTER3, ALPHA)),
    ("adapter3_legacy_spelling", (CHANNEL3, ALPHA)),
    ("user_user", (USER_OP, USER_ANN)),
    ("user_route", (USER_OP, ROUTE)),
    ("system_agent", (SYSTEM, ALPHA)),
    ("bare_name_agent", (BARE, ALPHA)),
]

PINNED = {
    "agent_pair_name_ab": (
        ("agent", ALPHA, BETA),
        "t.36d3esmiwzmpa5t65pgokfc5zy",
        "p.ttws2vkhapiagac5p56thq3ohq",
        "t.36d3esmiwzmpa5t65pgokfc5zy",
        "c.zoawnxyj5onq2otkhasqqppa74",
    ),
    "agent_pair_name_ba": (
        ("agent", ALPHA, BETA),
        "t.36d3esmiwzmpa5t65pgokfc5zy",
        "p.ttws2vkhapiagac5p56thq3ohq",
        "t.36d3esmiwzmpa5t65pgokfc5zy",
        "c.zoawnxyj5onq2otkhasqqppa74",
    ),
    "agent_pair_id_form": (
        ("agent", ALPHA_ID, BETA_ID),
        "t.efo3fugzbhggf5ufhofabe2yly",
        "p.pygbh467a7h2c7uffc4thekab4",
        "t.efo3fugzbhggf5ufhofabe2yly",
        "c.kbhbkkfu4qrergskk2dsxe3zhe",
    ),
    "agent_pair_id_form_reversed": (
        ("agent", ALPHA_ID, BETA_ID),
        "t.efo3fugzbhggf5ufhofabe2yly",
        "p.pygbh467a7h2c7uffc4thekab4",
        "t.efo3fugzbhggf5ufhofabe2yly",
        "c.kbhbkkfu4qrergskk2dsxe3zhe",
    ),
    "agent_route": (None, None, None, None, None),
    "agent_route_reversed": (None, None, None, None, None),
    "agent_adapter4": (
        ("adapter", ALPHA, ADAPTER4),
        "t.domxikoti4obuse7pp5vt5bgva",
        "p.27qvvze5j7up2bthzwcer63ada",
        "t.domxikoti4obuse7pp5vt5bgva",
        "c.dppnjwxdm54tzgkcri4mtshlcu",
    ),
    "agent_adapter4_legacy_spelling": (None, None, None, None, None),
    "agent_user_unbound": (
        ("user", ALPHA, USER_OP),
        "t.473x3nmznel2jvkxyvny7426lm",
        "p.f44kijr6jxrjoxn6r5clghphc4",
        "t.473x3nmznel2jvkxyvny7426lm",
        "c.p2a4dlaczbk2hq2ckmup66zsxi",
    ),
    "agent_user_unbound_reversed": (
        ("user", ALPHA, USER_OP),
        "t.473x3nmznel2jvkxyvny7426lm",
        "p.f44kijr6jxrjoxn6r5clghphc4",
        "t.473x3nmznel2jvkxyvny7426lm",
        "c.p2a4dlaczbk2hq2ckmup66zsxi",
    ),
    "reserved_dispatch_agent": (
        None,
        None,
        "p.keux2xksaw5aem7riis4sqlkkm",
        "p.keux2xksaw5aem7riis4sqlkkm",
        "c.sgevd6yesejosaxpxozblxj4xu",
    ),
    "reserved_hooks_agent": (
        None,
        None,
        "p.lxjhkzcxgkauwpsb7u6owhhpv4",
        "p.lxjhkzcxgkauwpsb7u6owhhpv4",
        "c.hr5ue7hahjyl4sdk264imyvs44",
    ),
    "adapter3_agent": (
        None,
        None,
        "p.leef2clq7gbjyjr2w7xtjqfwna",
        "p.leef2clq7gbjyjr2w7xtjqfwna",
        "c.5yzt465klvfchqixuq7rc35dr4",
    ),
    "adapter3_legacy_spelling": (None, None, None, None, None),
    "user_user": (
        None,
        None,
        "p.vdihrxcwromczafe2h4qlzeyuy",
        "p.vdihrxcwromczafe2h4qlzeyuy",
        "c.e5xpr6uucm7q3qnop3yykj23my",
    ),
    "user_route": (None, None, None, None, None),
    "system_agent": (None, None, None, None, None),
    "bare_name_agent": (None, None, None, None, None),
}

CONVERSATION_ID_MATRIX = [
    (None, f"{T_ALPHA_BETA}\0", "c.zoawnxyj5onq2otkhasqqppa74"),
    ("", f"{T_ALPHA_BETA}\0", "c.zoawnxyj5onq2otkhasqqppa74"),
    (
        "dc.quh7gwxudevchjablaf4yh6yxq",
        f"{T_ALPHA_BETA}\0dc.quh7gwxudevchjablaf4yh6yxq",
        "c.as7zs45b5ccradorkczgzna6a4",
    ),
    (
        "conv-custom-1",
        f"{T_ALPHA_BETA}\0conv-custom-1",
        "c.h4k7kois3i2ya2my5haq2o35oy",
    ),
    (
        "g.abcdefghijklmnopqrstuvwxyz",
        f"{T_ALPHA_BETA}\0g.abcdefghijklmnopqrstuvwxyz",
        "c.hmcyxdwlnc4owuvpr7xrvcjcay",
    ),
]

STAMP_MATRIX = [
    (
        "t.oq575fyi6fgxr4affnvkq6yzea",
        b"cross-node-sender-stamp",
        ALPHA,
        BETA,
        "c.helwxdet2riu53ngzjhxc2j23m",
    ),
    (
        "t.trxfnefq3b7cx46msc6r5eno6u",
        b"inherited-thread",
        USER_OP,
        ROUTE,
        "c.j3ritrz35h2xxzwarepnpgt5xa",
    ),
]

def _thread_id(a: str, b: str) -> str | None:
    key = direct_key(a, b)
    return None if key is None else key.id


@pytest.mark.parametrize(
    ("name", "pair"), THREAD_MATRIX, ids=[row[0] for row in THREAD_MATRIX]
)
def test_every_pair_pins_its_thread_pair_key_and_cell(name: str, pair: tuple[str, str]) -> None:
    a, b = pair
    fields, thread_id, pair_key, attribution, cell = PINNED[name]

    key = direct_key(a, b)
    found = None if key is None else (key.kind, key.agent, key.counterpart)
    assert found == fields
    assert _thread_id(a, b) == thread_id
    assert conversation_pair_key(a, b) == pair_key
    assert conversation_attribution(_thread_id(a, b), a, b) == attribution
    if attribution is None:
        assert cell is None
        return
    assert is_thread_id_segment(attribution) or attribution.startswith("p.")
    assert conversation_key(attribution, None) == f"{attribution}\0"
    assert conversation_cell_id(AUTHORITY, attribution, None) == cell


def test_the_seed_framing_is_pinned_bytes() -> None:
    key = direct_key(ALPHA, BETA)
    assert key is not None
    assert direct_seed(key) == (
        b"hyprial-thread-v1\0"
        + b"\x00\x00\x00\x05agent"
        + len(ALPHA).to_bytes(4, "big")
        + ALPHA.encode()
        + len(BETA).to_bytes(4, "big")
        + BETA.encode()
    )
    assert _thread_id(ALPHA, BETA) == T_ALPHA_BETA


@pytest.mark.parametrize(
    ("conversation_id", "key_bytes", "cell"),
    CONVERSATION_ID_MATRIX,
    ids=["none", "empty", "dc-derived", "caller-custom", "group-spelling"],
)
def test_each_conversation_id_pins_its_keyed_cell(
    conversation_id: str | None, key_bytes: str, cell: str
) -> None:
    assert conversation_key(T_ALPHA_BETA, conversation_id) == key_bytes
    assert conversation_cell_id(AUTHORITY, T_ALPHA_BETA, conversation_id) == cell


def test_none_and_empty_conversation_ids_are_one_conversation() -> None:
    assert conversation_key(T_ALPHA_BETA, None) == conversation_key(T_ALPHA_BETA, "")
    assert conversation_cell_id(AUTHORITY, T_ALPHA_BETA, None) == conversation_cell_id(
        AUTHORITY, T_ALPHA_BETA, ""
    )


@pytest.mark.parametrize(
    ("stamp", "seed", "a", "b", "cell"),
    STAMP_MATRIX,
    ids=["cross-node-sender", "reply-inherited"],
)
def test_a_stored_stamp_wins_over_inference(
    stamp: str, seed: bytes, a: str, b: str, cell: str
) -> None:
    assert mint_thread_id(seed) == stamp
    assert conversation_attribution(stamp, a, b) == stamp
    assert conversation_cell_id(AUTHORITY, stamp, None) == cell


def test_group_ids_never_come_from_this_derivation() -> None:
    group = "g.abcdefghijklmnopqrstuvwxyz"
    assert is_group_id_segment(group)
    assert not is_thread_id_segment(group)
    assert mint_group_id(b"frozen-keys-sample") == "g.5tzh4ydovsqwg5l5wxysvohhu4"
    thread_ids = [
        pinned[1] for pinned in PINNED.values() if pinned[1] is not None
    ] + [BOUND_THREAD] + [row[0] for row in STAMP_MATRIX]
    for thread_id in thread_ids:
        assert is_thread_id_segment(thread_id)
        assert not is_group_id_segment(thread_id)


def test_literals_already_pinned_elsewhere_in_the_repo_reproduce() -> None:
    assert _thread_id(ALPHA, BETA) == "t.36d3esmiwzmpa5t65pgokfc5zy"
    adapter_thread = _thread_id("adapter:op:node-1:golden-lark", BETA_GOLDEN)
    assert adapter_thread == "t.jdknpmo63tkapzzqbsmeehd7xe"
    assert (
        conversation_cell_id(AUTHORITY, adapter_thread, "lark-conversation-1")
        == "c.2nwbnegg2lkzyxsnligwjjxvpi"
    )
    user_thread = _thread_id(USER_OP, BETA_GOLDEN)
    assert user_thread == "t.cja4j77timli7gjcf5ch3x72tm"
    assert (
        conversation_cell_id(AUTHORITY, user_thread, "conv-owner")
        == "c.ohxebo23rrgm2cvq5xnb34mdum"
    )


def test_the_frozen_cell_literals_pin_the_record_directory_paths() -> None:
    """Review R2: the frozen ``c.`` literals name the record directories."""

    from hyprial.daemon.impl.conversations.record.segments import SegmentStore

    store = SegmentStore(fs=lambda: None, space_for=lambda _a: None, writer="node-1")
    default_cell = "c.zoawnxyj5onq2otkhasqqppa74"
    binding_cell = "c.swodmerqzz63a6rqc6eelpmkia"
    assert default_cell == conversation_cell_id(AUTHORITY, T_ALPHA_BETA, None)
    assert binding_cell == conversation_cell_id(
        "user:alice", BOUND_THREAD, None
    )
    assert store.directory(default_cell) == (
        f"cells/conversation/{default_cell}/rec-node-1"
    )
    assert store.segment_path(default_cell, 0) == (
        f"cells/conversation/{default_cell}/rec-node-1/000000.jsonl"
    )
    assert store.segment_path(default_cell, 7) == (
        f"cells/conversation/{default_cell}/rec-node-1/000007.jsonl"
    )
    assert store.segment_path(binding_cell, 0) == (
        f"cells/conversation/{binding_cell}/rec-node-1/000000.jsonl"
    )
