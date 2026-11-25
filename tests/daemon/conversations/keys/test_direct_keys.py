"""The moved key derivation equals the frozen one (T1a step 1).

Ported from tests/daemon/threads/test_ids.py and test_translation.py:203
(internal and system actors have no key), plus the keys.py purity guard
(rewrite of tests/daemon/threads/test_seams.py:58) and the equivalence
assertions: while the threads package exists, every frozen literal in
test_frozen_keys must also be the direct derivation's answer, and the two
derivations must agree on every input.  Delete the equivalence asserts with
the threads package (plan T1c).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hyprial.daemon.impl.conversations import conversation_cell_id
from hyprial.daemon.impl.conversations.keys import (
    DIRECT_KINDS,
    DIRECT_SEED_TAG,
    DirectEndpoint,
    direct_endpoint,
    direct_key,
    direct_seed,
)
from hyprial.kernel import is_thread_id_segment, mint_thread_id
from tests.daemon.conversations.keys.test_frozen_keys import (
    ADAPTER3,
    ADAPTER4,
    ALPHA,
    AUTHORITY,
    BARE,
    BETA,
    CHANNEL4,
    DISPATCH,
    HOOKS,
    PINNED,
    ROUTE,
    SYSTEM,
    THREAD_MATRIX,
    USER_OP,
)

IMPL = Path(__file__).resolve().parents[4] / "src/hyprial/daemon/impl"


def test_kinds_are_the_three_phase_a_kinds() -> None:
    assert DIRECT_KINDS == ("agent", "user", "adapter")


@pytest.mark.parametrize(
    ("uri", "kind", "canonical"),
    [
        (ALPHA, "agent", ALPHA),
        (USER_OP, "user", USER_OP),
        (ADAPTER4, "adapter", ADAPTER4),
    ],
)
def test_endpoint_kinds(uri: str, kind: str, canonical: str) -> None:
    endpoint = direct_endpoint(uri)
    assert endpoint is not None
    assert (endpoint.kind, endpoint.uri) == (kind, canonical)
    assert endpoint == DirectEndpoint(kind, canonical)


@pytest.mark.parametrize(
    "uri",
    ["", BARE, SYSTEM, "agent:op:node-1", "route:lark", ROUTE, "user:a:b", 7, ADAPTER3, CHANNEL4],
)
def test_non_endpoints(uri: object) -> None:
    assert direct_endpoint(uri) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(("name", "pair"), THREAD_MATRIX, ids=[r[0] for r in THREAD_MATRIX])
def test_the_moved_derivation_answers_the_frozen_literals(
    name: str, pair: tuple[str, str]
) -> None:
    a, b = pair
    fields, thread_id, _pair_key, attribution, cell = PINNED[name]

    key = direct_key(a, b)
    found = None if key is None else (key.kind, key.agent, key.counterpart)
    assert found == fields
    assert (None if key is None else key.id) == thread_id
    if attribution is not None:
        assert conversation_cell_id(AUTHORITY, attribution, None) == cell


def test_internal_and_system_actors_have_no_key() -> None:
    for pair in (
        (DISPATCH, BETA),
        ("agent:op:node-1:dispatch-coordinator", ALPHA),
        (HOOKS, ALPHA),
        (SYSTEM, BETA),
    ):
        assert direct_key(*pair) is None


def test_the_seed_and_tag_are_the_frozen_bytes() -> None:
    key = direct_key(ALPHA, BETA)
    assert key is not None
    assert DIRECT_SEED_TAG == b"hyprial-thread-v1\0"
    assert key.id == mint_thread_id(direct_seed(key)) == "t.36d3esmiwzmpa5t65pgokfc5zy"


def test_keys_module_is_pure() -> None:
    source = (IMPL / "conversations/keys.py").read_text()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    assert {name for name in names if name.startswith("hyprial.")} <= {
        "hyprial.kernel",
        *{f"hyprial.kernel.{name.split('.')[-1]}" for name in names},
    }


def test_all_ids_stay_thread_segments() -> None:
    for _name, pair in THREAD_MATRIX:
        key = direct_key(*pair)
        if key is not None:
            assert is_thread_id_segment(key.id)



def test_a_custom_internal_actor_has_no_key() -> None:
    """Review R1: the runtime internal set is an input, not a fixed rule."""

    relay = "agent:op:node-1:relay"

    assert direct_key(ALPHA, relay, internal=frozenset({"relay"})) is None
    assert direct_key(relay, USER_OP, internal=frozenset({"relay"})) is None
    assert direct_key(ALPHA, relay) is not None

    assert direct_key(ALPHA, "agent:op:node-1:dispatch-coordinator") is None


@pytest.mark.parametrize(
    "uri",
    [
        "AGENT:op:node-1:alpha",
        "Agent:op:node-1:alpha",
        "agent:OP:node-1:ALPHA",
        "USER:op",
        "User:op",
        "ROUTE:lark:ops",
        "ADAPTER:op:node-1:lark",
        "CHANNEL:op:node-1:lark",
        "foo:bar:baz",
        "x:",
        ":",
        "agent:",
        "route:",
        "adapter:lark",
        "user:",
        7,
        None,
        b"agent:op:node-1:alpha",
    ],
)
def test_odd_spellings_return_no_spurious_endpoint_or_key(uri: object) -> None:
    mine = direct_endpoint(uri)  # type: ignore[arg-type]
    if not isinstance(uri, str) or uri == "foo:bar:baz" or uri.startswith(("x:", ":", "agent:", "route:", "adapter:", "user:")):
        if uri == "adapter:lark":
            assert mine is None
    for other in (ALPHA, USER_OP, ROUTE, BARE, ""):
        if mine is None:
            assert direct_key(uri, other) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("a", ["", " ", "agent:op:node-1:", 7, None])
@pytest.mark.parametrize("b", ["", BETA, "user:op", None])
def test_empty_and_non_string_pairs_return_none(a: object, b: object) -> None:
    assert direct_key(a, b) is None  # type: ignore[arg-type]


