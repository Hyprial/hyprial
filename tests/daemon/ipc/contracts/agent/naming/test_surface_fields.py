"""UUID addressing lane C: every agent row names its id and its display name.

``agent.create``/``agent.get``/``agent.list``/``ps`` agent rows carry the
additive ``agentId`` and ``displayName`` keys from the record, and a local
registered agent's ``targets`` row carries its ``agentId``.  Users, routes
and unregistered actors gain nothing.  With id-form minting on, the target
URI is the id address and the row still names the same id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hyprial.identity.impl.agents.addressing as addressing
from tests.daemon.routing.addressing.test_agent_address_normalization import (
    _AutoConfirmTransport,
    application,
    owned_applications as owned_applications,
)


@pytest.fixture
def app(tmp_path: Path):
    instance = application(tmp_path, _AutoConfirmTransport(), ())
    try:
        yield instance
    finally:
        instance._close()


def _fields(row: dict) -> tuple[object, object]:
    return row["agentId"], row["displayName"]


def test_agent_rows_carry_the_id_and_the_display_name(app) -> None:
    created = app.handle("agent.create", {"name": "worker"})["agent"]
    agent = app.agents.require("worker")
    assert _fields(created) == (agent.agent_id, None)
    app.agents.rename("worker", "reviewer")
    got = app.handle("agent.get", {"name": "reviewer"})["agent"]
    assert _fields(got) == (agent.agent_id, "reviewer")
    listed = app.handle("agent.list", {})["agents"]
    assert [_fields(row) for row in listed] == [(agent.agent_id, "reviewer")]
    ps_rows = app.handle("ps", {})["agents"]
    assert [_fields(row) for row in ps_rows] == [(agent.agent_id, "reviewer")]


def test_a_local_agent_target_row_carries_its_agent_id(app) -> None:
    app.handle("agent.create", {"name": "worker"})
    agent = app.agents.require("worker")
    rows = app.handle("targets", {"kind": "agent"})["targets"]
    assert [(row["targetUri"], row["agentId"]) for row in rows] == [
        (agent.uri, agent.agent_id)
    ]


def test_a_legacy_token_record_lists_its_target_with_a_null_agent_id(
    app, monkeypatch
) -> None:
    from dataclasses import replace

    app.handle("agent.create", {"name": "worker"})
    legacy = replace(app.agents.require("worker"), entity_token="incarnation-1")
    monkeypatch.setattr(app.agents, "list", lambda *args, **kwargs: [legacy])
    rows = app.handle("targets", {"kind": "agent"})["targets"]
    assert [(row["targetUri"], row["agentId"]) for row in rows] == [(legacy.uri, None)]


def test_a_target_row_without_a_record_has_no_agent_id(tmp_path: Path) -> None:
    stranger = "agent:op:node-1:visitor"
    instance = application(tmp_path, _AutoConfirmTransport(), (stranger,))
    try:
        rows = instance.handle("targets", {"kind": "agent"})["targets"]
    finally:
        instance._close()
    assert [row["targetUri"] for row in rows] == [stranger]
    assert "agentId" not in rows[0]


def test_id_form_target_row_is_the_id_address(app, monkeypatch) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    app.handle("agent.create", {"name": "worker"})
    agent = app.agents.require("worker")
    assert agent.uri.endswith(":" + agent.agent_id)
    rows = app.handle("targets", {"kind": "agent"})["targets"]
    assert [(row["targetUri"], row["agentId"]) for row in rows] == [
        (agent.uri, agent.agent_id)
    ]
