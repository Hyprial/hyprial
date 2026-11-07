"""The principal grammar: one validation/classification point for owners (D1).

Shape and character hygiene only — existence and reachability are never
legality inputs (design §1.3).  The same grammar powers the write boundary
(graph tests), the resolver, and the projections; this module pins its unit
behavior directly, including the guards the agent parser alone does NOT give.
"""

from __future__ import annotations

import pytest

from hyprial.identity import PAC_PRINCIPAL_SHAPE_INVALID, PacError
from hyprial.identity import parse_principal, principal_kind, principal_matches_local_actor
from hyprial.identity.impl.principal import Principal
from hyprial.kernel import canonical_user_uri, parse_user_uri


@pytest.mark.parametrize(
    "value,kind,owner,machine,actor",
    [
        ("user:allen", "user", "allen", None, None),
        ("agent:allen:hq.example-tailnet.ts.net:kanban", "agent", "allen", "hq.example-tailnet.ts.net", "kanban"),
    ],
)
def test_canonical_principals_parse(value, kind, owner, machine, actor):
    assert parse_principal(value) == Principal(
        uri=value, kind=kind, owner=owner, machine=machine, actor=actor
    )


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "allen",  # bare short name
        "agent:squire",  # two segments
        "agent:allen:test-node",  # missing actor
        "agent::test-node:x",  # empty owner segment
        "user:",  # empty user owner
        "user:allen:extra",  # colon inside user owner
        "host:machine",  # a node is not a responsible party
        "route:lark:chat",  # a route is a delivery exit
        "adapter:allen:machine:lark",  # an adapter is a delivery exit
        "channel:lark:chat",  # a channel is a delivery exit
        "user: allen",  # leading whitespace inside the URI
        "user:allen ",  # trailing whitespace
        "agent:allen:test node:x",  # inner whitespace
        "agent:allen:test-node:x\n",  # control character
        "agent:allen:test-node:x\x00",  # NUL
    ],
)
def test_non_principals_are_rejected_naming_the_reason(value):
    with pytest.raises(PacError) as raised:
        parse_principal(value)
    assert raised.value.code == PAC_PRINCIPAL_SHAPE_INVALID
    assert raised.value.data == {"principal": value}
    # the message must say WHERE the input went wrong, not just "unknown"
    assert any(
        needle in str(raised.value)
        for needle in (
            "bare short names",
            "four segments",
            "delivery exits",
            "whitespace or control",
            "non-empty",
            "exactly user:<owner>",
        )
    )


def test_principals_are_never_normalized():
    # no trimming, lowercasing, or alias folding: a value that needs fixing is
    # rejected naming the field; a valid mixed-case value round-trips verbatim
    assert parse_principal("user:AliceSmith").uri == "user:AliceSmith"
    assert (
        parse_principal("agent:AliceSmith:Node.One.Example:KanBan-1").owner
        == "AliceSmith"
    )
    with pytest.raises(PacError):
        parse_principal("user:allen ")


def test_kind_classification_and_the_legacy_verdict():
    assert principal_kind("user:allen") == "user"
    assert principal_kind("agent:allen:m:a") == "agent"
    # a pre-URI short name gets the HONEST verdict: unknown era, never a guess
    assert principal_kind("allen") is None
    assert principal_kind("") is None


def test_execution_binding_match_requires_owner_machine_and_actor():
    binding = dict(owner="allen", machine="test-node", actor_name="kanban")
    assert principal_matches_local_actor("agent:allen:test-node:kanban", **binding)
    assert not principal_matches_local_actor("agent:allen:test-node:other", **binding)
    # same name on a foreign machine is a different agent: no association
    assert not principal_matches_local_actor(
        "agent:allen:far-away.example:kanban", **binding
    )
    # a different owner's agent with the same machine/actor spelling
    assert not principal_matches_local_actor("agent:bob:test-node:kanban", **binding)
    # user principals and legacy names never match a runtime binding
    assert not principal_matches_local_actor("user:allen", **binding)
    assert not principal_matches_local_actor("allen", **binding)


def test_user_grammar_leaf_rejects_what_the_class_has_always_rejected():
    assert canonical_user_uri("allen") == "user:allen"
    assert parse_user_uri("user:allen") == "allen"
    for bad in ("", "allen:", "user:", "user:allen:extra"):
        assert parse_user_uri(bad) is None, bad
    with pytest.raises(ValueError):
        canonical_user_uri("allen:extra")
    with pytest.raises(ValueError):
        canonical_user_uri(" allen")


def test_user_uri_accepts_immutable_id_segment() -> None:
    assert canonical_user_uri("u.abc") == "user:u.abc"


def test_email_shaped_owner_is_a_legal_principal() -> None:
    """Production owners are email-shaped (2026-09-15): '@' is a legal
    character in the user owner segment; the grammar only rejects emptiness,
    colons, surrounding whitespace, and control characters."""
    principal = parse_principal("user:bob.smith@example.com")
    assert principal.kind == "user"
    assert principal.owner == "bob.smith@example.com"
    assert principal_kind("user:bob.smith@example.com") == "user"
    # agent machines may be dotted hostnames; owners may carry '@' there too
    assert (
        parse_principal("agent:bob.smith@example.com:hq.example-tailnet.ts.net:kanban").owner
        == "bob.smith@example.com"
    )


def test_principal_parser_accepts_immutable_user_id_segment() -> None:
    principal = parse_principal("user:u.abc")
    assert principal == Principal(uri="user:u.abc", kind="user", owner="u.abc")
