"""UUID addressing §2.1: every record URI is minted by ``mint_agent_uri``.

With the switch off the minted URI is the historical name form, byte for
byte; with it on, each incarnation's own ``a.`` id is the address segment.
"""

from __future__ import annotations

import sqlite3

import pytest

from hyprial.identity.impl.agents import Agent, AgentRegistry
from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand
from hyprial.identity.impl.agents.registry.naming.id_form import stamp_agent_id
from hyprial.kernel import LifecycleMutationRequest

from .conftest import NODE, OWNER


@pytest.mark.usefixtures("switch_off")
def test_switch_off_mints_the_name_form(registry: AgentRegistry) -> None:
    assert registry.create("foo").uri == f"agent:{OWNER}:{NODE}:foo"
    assert registry.create_transfer_hosted("tx", pinned_owner="alice").uri == (
        f"agent:alice:{NODE}:tx"
    )
    rotated = registry.rotate_secret_authority("foo")
    assert rotated.uri == f"agent:{OWNER}:{NODE}:foo"


@pytest.mark.usefixtures("id_form")
def test_switch_on_mints_each_incarnation_id(registry: AgentRegistry) -> None:
    foo = registry.create("foo")
    assert foo.uri == f"agent:{OWNER}:{NODE}:{foo.agent_id}"
    hosted = registry.create_transfer_hosted("tx", pinned_owner="alice")
    assert hosted.uri == f"agent:alice:{NODE}:{hosted.agent_id}"
    assert registry.require("foo") == foo
    assert registry.uri_for("foo") == foo.uri
    assert registry.local_actor(foo.uri) == "foo"


@pytest.mark.usefixtures("id_form")
def test_switch_on_lifecycle_create_mints_the_id(registry: AgentRegistry) -> None:
    attempt = "op:forward:0"
    registry.apply_lifecycle(LifecycleMutationRequest(
        f"lifecycle:{attempt}", attempt, "op", None, CreateAgentCommand("c", "w"),
    ))
    agent = registry.require("w")
    assert agent.uri == f"agent:{OWNER}:{NODE}:{agent.agent_id}"


@pytest.mark.usefixtures("id_form")
def test_switch_on_rotation_re_mints_the_uri(registry: AgentRegistry) -> None:
    before = registry.create("foo")
    after = registry.rotate_secret_authority("foo")
    assert after.uri == f"agent:{OWNER}:{NODE}:{after.agent_id}"
    assert after.uri != before.uri
    assert registry.require("foo") == after
    assert registry.get(after.uri).actor == "foo"


@pytest.mark.usefixtures("id_form")
def test_from_json_without_uri_mints_from_the_token() -> None:
    record = Agent.from_json(
        {"actor": "foo", "owner": OWNER, "machine": NODE, "entityToken": "1" * 32},
        "legacy",
    )
    assert record.uri == f"agent:{OWNER}:{NODE}:{record.agent_id}"


@pytest.mark.usefixtures("switch_off")
def test_from_json_without_uri_keeps_the_name_form_when_off() -> None:
    record = Agent.from_json(
        {"actor": "foo", "owner": OWNER, "machine": NODE, "entityToken": "1" * 32},
        "legacy",
    )
    assert record.uri == f"agent:{OWNER}:{NODE}:foo"


@pytest.mark.usefixtures("id_form")
def test_switch_on_rotation_carries_the_uri_keyed_fences(registry: AgentRegistry) -> None:
    before = registry.create("bar")
    registry.record_external_binding(
        "bar", harness="pi", runtime="headless", session_id="s-1"
    )
    registry.retire_session_refs(
        "bar", before.entity_token, ["ref-b"], reason="test", destroy_attempt="t1",
    )
    after = registry.rotate_secret_authority("bar")
    assert registry.is_session_ref_retired("bar", "ref-b")
    assert registry.is_session_ref_retired(after.uri, "ref-b")
    assert registry.is_session_ref_retired(before.uri, "ref-b")
    binding = registry.lifecycle_binding("bar")
    assert binding is not None and binding["actor"] == after.uri
    stale = registry._db.execute(
        "SELECT 1 FROM lifecycle_resources WHERE resource_key = ?",
        (f"binding:{before.uri}",),
    ).fetchone()
    assert stale is None
    registry.destroy("bar")
    assert registry.is_session_ref_retired("bar", "ref-b")
    assert registry.is_session_ref_retired(after.uri, "ref-b")


@pytest.mark.parametrize("switch", ["switch_off", "id_form"])
def test_rotation_carries_the_incarnation_session_history(
    registry: AgentRegistry, request: pytest.FixtureRequest, switch: str
) -> None:
    """A rotated record keeps the refs it registered, under its new token."""

    request.getfixturevalue(switch)
    before = registry.create("foo")
    registry.record_session_ref("foo", before.entity_token, "session-a")

    after = registry.rotate_secret_authority("foo")

    assert after.entity_token != before.entity_token
    assert [
        (str(row["entity_token"]), str(row["session_ref"]))
        for row in registry._db.execute(
            "SELECT entity_token, session_ref FROM agent_session_ref_history "
            "WHERE actor = 'foo'"
        )
    ] == [(after.entity_token, "session-a")]


class _FailingDb:
    """A connection stand-in whose every statement fails with ``message``."""

    def __init__(self, message: str) -> None:
        self.message = message

    def execute(self, *_args: object) -> None:
        raise sqlite3.OperationalError(self.message)


@pytest.mark.usefixtures("id_form")
def test_stamp_agent_id_skips_only_a_missing_agent_id_column(
    registry: AgentRegistry,
) -> None:
    token = registry.create("foo").entity_token
    stamp_agent_id(_FailingDb("no such column: agent_id"), "agent_secret_grants", token)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        stamp_agent_id(_FailingDb("database is locked"), "agent_secret_grants", token)
