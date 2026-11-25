"""The group methods served by the daemon itself (slice GC2 wiring).

Every call here enters through ``app.handle`` (the IPC router and the
production ``METHOD_REGISTRY``) or through the request HTTP server's
catalog route forwarding to ``app.handle``, never through the handlers or
a test-local registry: a group is created, read, grown, listed among
direct conversations, shrunk, and refused for an unknown id and for a
non-member with the declared codes.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.shell.impl.request_http.catalog import build_view
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.daemon.conversations.group_requests.group_request_support import (
    OWNER,
    GroupDaemon,
    group_daemon,
    member_ids,
)
from tests.support.golden.app import OWNER as OWNER_NAME, golden_daemon as golden_daemon

UNKNOWN = "g.unknowngroupaaaaaaaaaaaaaa"


def _handle(booted: GroupDaemon, method: str, who: str = OWNER, **params: Any) -> dict[str, Any]:
    return booted.app.handle(
        method, {**booted.caller(who), **params}, _trusted_message_origin="ipc"
    )


def _refused(booted: GroupDaemon, method: str, who: str = OWNER, **params: Any) -> str:
    with pytest.raises(DaemonRequestError) as raised:
        _handle(booted, method, who, **params)
    return raised.value.code


def test_the_group_methods_round_trip_through_the_daemon(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    alpha, beta = booted.agent_id("alpha"), booted.agent_id("beta")

    created = _handle(booted, "conversations.create", title="release", members=["alpha"],
                      idempotencyKey="wired-create")
    group = created["conversationId"]
    got = _handle(booted, "conversations.get", conversationId=group)
    booted.advance()
    added = _handle(booted, "conversations.members.add", conversationId=group, member="beta",
                    idempotencyKey="wired-add")
    listed = _handle(booted, "conversations.list", kinds=["direct", "group"])
    booted.advance()
    removed = _handle(booted, "conversations.members.remove", conversationId=group,
                      memberId=beta, idempotencyKey="wired-remove")

    assert group.startswith("g.")
    assert member_ids(created) == [OWNER, alpha]
    assert got["members"] == created["members"]
    assert (got["self"], got["canManageMembers"]) == (OWNER, True)
    assert member_ids(added) == [OWNER, alpha, beta]
    assert [(head["kind"], head["conversationId"], head["memberCount"])
            for head in listed["conversations"]] == [("group", group, 3)]
    assert member_ids(removed) == [OWNER, alpha]
    assert removed["entryId"] is not None


def test_the_daemon_answers_the_declared_group_refusals(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _handle(booted, "conversations.create", title="release", members=["alpha"],
                    idempotencyKey="wired-create")["conversationId"]

    assert _refused(booted, "conversations.get", conversationId=UNKNOWN) == (
        "CONVERSATION_NOT_FOUND"
    )
    assert _refused(booted, "conversations.get", "gamma", conversationId=group) == (
        "CONVERSATION_NOT_MEMBER"
    )
    assert _refused(booted, "conversations.members.add", "gamma", conversationId=group,
                    member="beta", idempotencyKey="k") == "CONVERSATION_NOT_MEMBER"
    assert _refused(booted, "conversations.members.remove", conversationId=UNKNOWN,
                    memberId=booted.agent_id("alpha"), idempotencyKey="k") == (
        "CONVERSATION_NOT_FOUND"
    )


def _http_server(booted: GroupDaemon) -> RequestHttpServer:
    config = RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"), owner=OWNER_NAME, actor=OWNER,
        host="127.0.0.1", port=0, allowed_origins=(), allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=Path("/tmp/request-http-runtime"), source=Path("/tmp/config.json"),
    )

    def request_once(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        return booted.app.handle(method, dict(params), _trusted_message_origin="ipc")

    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item.update(declared=True, reason=None,
                    state="available" if entry["httpExposed"] else "disabled")
        item["callable"] = item["state"] == "available"
        entries.append(item)
    server = RequestHttpServer(config, request_once=request_once)
    view = build_view(
        {"version": __version__, "nodeId": "node", "owner": OWNER_NAME,
         "epoch": "epoch-1", "phase": "serving"},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "epoch-1",
         "packageVersion": __version__, "methods": entries},
        socket_path="/tmp/daemon.sock", local_entries=local_catalog()["methods"],
    )
    server._view = replace(view, routes=dict(view.routes))
    server._startup_declaration = view.declaration_digest
    server._startup_http = view.http_contract_digest
    server._snapshot_at = time.monotonic()
    return server


def test_a_group_is_read_over_the_http_catalog_route(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    server = _http_server(booted)
    created = server._invoke(
        "conversations.create", "http-create", time.monotonic(),
        {"title": "release", "members": ["alpha"], "idempotencyKey": "http-create"}, None,
    )
    assert created.status == 200, created.payload
    group = created.payload["result"]["conversationId"]

    got = server._invoke("conversations.get", "http-get", time.monotonic(),
                         {"conversationId": group}, None)
    unknown = server._invoke("conversations.get", "http-unknown", time.monotonic(),
                             {"conversationId": UNKNOWN}, None)

    assert got.status == 200, got.payload
    assert got.payload["result"] == _handle(booted, "conversations.get", conversationId=group)
    assert unknown.code == "CONVERSATION_NOT_FOUND", unknown.payload
