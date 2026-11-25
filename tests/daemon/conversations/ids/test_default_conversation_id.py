"""The pure default-conversation id and its endpoint keys (plan slice C1).

The literal ids below pin the formula: a change of tag, framing, hash,
encoding or a key rule moves every default conversation and must fail here
first.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.conversations.ids import (
    DEFAULT_CONVERSATION_PREFIX,
    default_conversation_id,
    default_conversation_seed,
    endpoint_key,
)
from hyprial.daemon.impl.conversations.keys import direct_endpoint
from hyprial.kernel import (
    DISPATCH_SERVICE_ACTOR_NAME,
    TURN_HOOKS_ACTOR_NAME,
    canonical_agent_uri,
)

ALPHA = canonical_agent_uri("op", "node-1", "alpha")
BETA = canonical_agent_uri("op", "node-1", "beta")
DISPATCH = canonical_agent_uri("op", "node-1", DISPATCH_SERVICE_ACTOR_NAME)
HOOKS = canonical_agent_uri("op", "node-1", TURN_HOOKS_ACTOR_NAME)

AGENT_ID = "a.b4kuo3kxusjtp6ug25qzqycshe"
PEER_ID = "a.xs3h24k2eippf7faqsx4qzxfdi"


def pair_id(a: str, b: str) -> str:
    return default_conversation_id(endpoint_key(a), endpoint_key(b))


PINNED = (
    (
        canonical_agent_uri("op", "node-1", AGENT_ID),
        canonical_agent_uri("op", "node-1", PEER_ID),
        "dc.n5fs7qnuxyt2bwupezrd3kuqqy",
    ),
    (canonical_agent_uri("op", "node-1", AGENT_ID), "user:op", "dc.yxfzzbasf44op7qnmcmo2if62m"),
    (ALPHA, BETA, "dc.quh7gwxudevchjablaf4yh6yxq"),
    (ALPHA, "user:op", "dc.xy6aor733a6q3lphrp6gaaxmay"),
    ("user:op", "user:ann", "dc.jtbjgasdysdos7leuf4geogie4"),
    (DISPATCH, ALPHA, "dc.iq2c6hoykp23dhyoykao6am4ti"),
    (HOOKS, ALPHA, "dc.jf536jauegqesml3oqt5ojmso4"),
    ("adapter:lark:golden-lark", ALPHA, "dc.sbr3c2bdfxfxkt5gc6utcd3co4"),
    (ALPHA, "route:golden-lark:group", "dc.ocxlv37lqyxcazemgcfayvnssi"),
    ("adapter:op:node-1:lark", ALPHA, "dc.zwie6wf5zg7f547n36q67uotoi"),
    (ALPHA, ALPHA, "dc.dhlrqwya3wyus6hlokhwoipula"),
    ("system:watchdog", "worker", "dc.nmt5srrjhosroocnq5xytiwo2m"),
    ("", "", "dc.r5dj33oxka7xjjpchbbbr2p7b4"),
)


@pytest.mark.parametrize(("a", "b", "expected"), PINNED)
def test_the_id_of_each_address_kind_is_pinned_and_symmetric(
    a: str, b: str, expected: str
) -> None:
    assert pair_id(a, b) == expected
    assert pair_id(b, a) == expected


def test_every_pinned_pair_has_its_own_id_of_one_shape() -> None:
    ids = [expected for _, _, expected in PINNED]
    assert len(set(ids)) == len(ids)
    for value in ids:
        assert value.startswith(DEFAULT_CONVERSATION_PREFIX)
        body = value[len(DEFAULT_CONVERSATION_PREFIX):]
        assert len(body) == 26
        assert set(body) <= set("abcdefghijklmnopqrstuvwxyz234567")


@pytest.mark.parametrize(
    ("legacy", "current"),
    (
        ("channel:op:node-1:lark", "adapter:op:node-1:lark"),
        ("channel:lark:golden-lark", "adapter:lark:golden-lark"),
    ),
)
def test_the_retired_channel_prefix_is_not_read_as_adapter(
    legacy: str, current: str
) -> None:
    assert endpoint_key(legacy) == legacy
    assert pair_id(legacy, ALPHA) != pair_id(current, ALPHA)


@pytest.mark.parametrize(
    "address",
    (
        ALPHA,
        DISPATCH,
        "user:op",
        "adapter:op:node-1:lark",
    ),
)
def test_a_key_that_is_an_address_agrees_with_thread_endpoints(address: str) -> None:
    endpoint = direct_endpoint(address)
    assert endpoint is not None
    assert endpoint_key(address) == endpoint.uri


@pytest.mark.parametrize(
    ("owner", "machine"),
    (("op", "node-2"), ("op", "laptop"), ("ann", "node-1"), ("ann", "elsewhere")),
)
def test_an_id_form_agent_keeps_its_conversations_under_another_address(
    owner: str, machine: str
) -> None:
    here = canonical_agent_uri("op", "node-1", AGENT_ID)
    moved = canonical_agent_uri(owner, machine, AGENT_ID)
    peer = canonical_agent_uri("op", "node-1", PEER_ID)

    assert endpoint_key(here) == endpoint_key(moved) == AGENT_ID
    for other in (peer, "user:op", "route:golden-lark:group", ALPHA):
        assert pair_id(here, other) == pair_id(moved, other) == pair_id(other, moved)
    moved_peer = canonical_agent_uri(owner, machine, PEER_ID)
    assert pair_id(here, peer) == pair_id(moved, moved_peer)


def test_a_name_form_agent_is_keyed_by_its_full_address() -> None:
    """A name cannot be mapped to its id without the hosting node's registry."""

    assert endpoint_key(ALPHA) == ALPHA
    elsewhere = canonical_agent_uri("op", "node-2", "alpha")
    assert pair_id(ALPHA, BETA) != pair_id(elsewhere, BETA)
    id_form = canonical_agent_uri("op", "node-1", AGENT_ID)
    assert pair_id(ALPHA, BETA) != pair_id(id_form, BETA)


@pytest.mark.parametrize(
    "address",
    ("agent:op:node-1:a.short", "agent:op:node-1:A.B4KUO3KXUSJTP6UG25QZQYCSHE", AGENT_ID),
)
def test_only_an_exact_id_segment_of_an_agent_address_is_an_id_key(
    address: str,
) -> None:
    assert endpoint_key(address) == address


@pytest.mark.parametrize(
    "address",
    ("alpha", "Agent:op:node-1:alpha", " user:op", "agent:op:node-1", "system:x", ""),
)
def test_an_address_without_a_canonical_form_is_used_exactly(address: str) -> None:
    assert endpoint_key(address) == address
    assert address.encode() in default_conversation_seed(endpoint_key(address), BETA)


def test_addresses_are_not_folded_into_each_other() -> None:
    assert pair_id("user:op", ALPHA) != pair_id("user:OP", ALPHA)
    assert pair_id(ALPHA, BETA) != pair_id(ALPHA, "user:op")


def test_the_seed_is_length_framed_so_a_boundary_shift_changes_the_id() -> None:
    assert default_conversation_seed("ab", "c") != default_conversation_seed("a", "bc")
    assert default_conversation_id("ab", "c") != default_conversation_id("a", "bc")


@pytest.mark.parametrize("value", (None, 7, b"agent", "\ud800", "x" * 70_000))
def test_the_function_is_total(value: object) -> None:
    derived = pair_id(value, ALPHA)  # type: ignore[arg-type]
    assert derived.startswith(DEFAULT_CONVERSATION_PREFIX)
    assert derived == pair_id(ALPHA, value)  # type: ignore[arg-type]
    assert default_conversation_id(value, ALPHA) == derived  # type: ignore[arg-type]
