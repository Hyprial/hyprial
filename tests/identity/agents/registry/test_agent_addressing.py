"""UUID addressing P2: one shared resolver for every local-agent read face.

The registry, the Agent actor projection and the daemon directory facade
each answered ``local_actor``/``uri_for`` with their own copy of the URI
parser.  They now share :mod:`hyprial.identity.impl.agents.addressing`, so an
``a.`` id resolves identically everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from hyprial.identity.impl.agents.addressing import (
    official_agent_uri,
    resolve_local_actor,
)

OWNER = "op"
NODE = "node-1"
FOO_ID = "a." + "c" * 26
HOSTED_ID = "a." + "d" * 26


@dataclass(frozen=True)
class Row:
    actor: str
    uri: str
    owner: str
    machine: str
    agent_id: str
    hosted_by: str | None = None


ROWS = (
    Row("foo", f"agent:{OWNER}:{NODE}:foo", OWNER, NODE, FOO_ID),
    Row("guest", f"agent:alice:{NODE}:guest", "alice", NODE, HOSTED_ID, "transfer-receive"),
)


def _read(key: str) -> Row | None:
    return next(
        (row for row in ROWS if key in (row.actor, row.uri, row.agent_id)), None
    )


def _resolve(value: str) -> str | None:
    return resolve_local_actor(value, owner=OWNER, machine=NODE, read=_read)


def test_names_and_name_uris_resolve_as_before() -> None:
    assert _resolve("foo") == "foo"
    assert _resolve(f"agent:{OWNER}:{NODE}:foo") == "foo"
    assert _resolve(f"agent:{OWNER}:{NODE}:unknown") == "unknown"
    assert _resolve("user:op") is None
    assert _resolve(f"agent:bob:{NODE}:foo") is None


def test_bare_ids_and_id_uris_resolve_to_the_record() -> None:
    assert _resolve(FOO_ID) == "foo"
    assert _resolve(f"agent:{OWNER}:{NODE}:{FOO_ID}") == "foo"
    assert _resolve(f"agent:alice:{NODE}:{HOSTED_ID}") == "guest"
    assert _resolve("a." + "e" * 26) is None
    assert _resolve(f"agent:{OWNER}:{NODE}:{'a.' + 'e' * 26}") is None


def test_an_id_under_the_wrong_owner_or_node_is_not_ours() -> None:
    assert _resolve(f"agent:alice:{NODE}:{FOO_ID}") is None
    assert _resolve(f"agent:{OWNER}:node-2:{FOO_ID}") is None


def test_hosted_name_uri_still_requires_the_exact_stored_uri() -> None:
    assert _resolve(f"agent:alice:{NODE}:guest") == "guest"
    assert _resolve(f"agent:mallory:{NODE}:guest") is None


def test_official_uri_prefers_the_stored_record() -> None:
    assert official_agent_uri("foo", owner=OWNER, machine=NODE, read=_read) == ROWS[0].uri
    assert official_agent_uri("guest", owner=OWNER, machine=NODE, read=_read) == ROWS[1].uri
    assert (
        official_agent_uri("squire", owner=OWNER, machine=NODE, read=_read)
        == f"agent:{OWNER}:{NODE}:squire"
    )


def test_mint_follows_the_id_form_switch(monkeypatch) -> None:
    from hyprial.identity.impl.agents import addressing

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    assert addressing.mint_agent_uri(OWNER, NODE, actor="foo", agent_id=FOO_ID) == (
        f"agent:{OWNER}:{NODE}:foo"
    )
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    assert addressing.id_form_minting() is True
    assert addressing.mint_agent_uri(OWNER, NODE, actor="foo", agent_id=FOO_ID) == (
        f"agent:{OWNER}:{NODE}:{FOO_ID}"
    )
