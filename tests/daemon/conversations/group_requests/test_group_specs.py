"""Declarations of the group methods and the ``kinds`` list parameter (slice GC2).

The group methods bind the conversation reads' owner-user selector, so over
HTTP the caller is always the owner and the IPC caller aliases are refused
before dispatch.  Their examples validate against their own declarations.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.daemon.impl.request_catalog.conversations import CONVERSATIONS_LIST_SPEC
from hyprial.daemon.impl.request_catalog.conversations.examples import CONVERSATION_EXAMPLES
from hyprial.daemon.impl.request_catalog.conversations.group_examples import GROUP_EXAMPLES
from hyprial.daemon.impl.request_catalog.conversations.groups import GROUP_BINDINGS
from hyprial.kernel import validate_method_example_set
from hyprial.shell.impl.request_http.catalog import build_view
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.server import RequestHttpServer

HOST_ONLY = frozenset({"from", "actor", "sessionRef", "onBehalfOf"})
CALLER = {
    "INVALID_ARGUMENT", "SENDER_UNRESOLVED", "AGENT_ADDRESS_RETIRED", "SENDER_NOT_AUTHENTICATED",
    "AMBIGUOUS_TARGET", "INVALID_AGENT_NAME", "STALE_SESSION", "SESSION_SUPERSEDED",
    "INVALID_SESSION_SOURCE",
}
SPECS = {spec.name: spec for spec, _handler in GROUP_BINDINGS}


def test_the_four_methods_and_their_errors() -> None:
    declared = {name: {error.code for error in spec.errors} - CALLER for name, spec in SPECS.items()}

    assert declared == {
        "conversations.create": {
            "CALLER_NOT_AUTHORIZED", "AGENT_NOT_FOUND", "UNSUPPORTED_TARGET", "DAEMON_UNAVAILABLE",
        },
        "conversations.get": {
            "CONVERSATION_NOT_FOUND", "CONVERSATION_NOT_MEMBER", "DAEMON_UNAVAILABLE",
        },
        "conversations.members.add": {
            "CONVERSATION_NOT_FOUND", "CONVERSATION_NOT_MEMBER", "CALLER_NOT_AUTHORIZED",
            "AGENT_NOT_FOUND", "UNSUPPORTED_TARGET", "DAEMON_UNAVAILABLE",
        },
        "conversations.members.remove": {
            "CONVERSATION_NOT_FOUND", "CONVERSATION_NOT_MEMBER", "CALLER_NOT_AUTHORIZED",
            "DAEMON_UNAVAILABLE",
        },
        "conversations.post": {
            "SUMMARY_REQUIRED", "CONVERSATION_NOT_FOUND", "CONVERSATION_NOT_MEMBER",
            "DAEMON_UNAVAILABLE",
        },
    }
    for spec in SPECS.values():
        assert CALLER <= {error.code for error in spec.errors}, spec.name


def test_the_group_methods_bind_the_list_selector() -> None:
    for name, spec in SPECS.items():
        assert spec.http_exposed is True, name
        assert spec.http_binding == CONVERSATIONS_LIST_SPEC.http_binding, name
        assert spec.audience == CONVERSATIONS_LIST_SPEC.audience, name
        assert spec.owner == "daemon.conversations", name
        properties = spec.params_schema["properties"]
        for field in HOST_ONLY:
            assert properties[field]["x-hyprial-http-forbidden"] is True, (name, field)
        assert spec.mutates is (name != "conversations.get"), name


def test_every_group_example_set_validates_against_its_declaration() -> None:
    assert {example_set.method for example_set in GROUP_EXAMPLES} == set(SPECS)
    for example_set in GROUP_EXAMPLES:
        validate_method_example_set(SPECS[example_set.method], example_set)


def test_the_list_examples_keep_their_first_cases_and_add_one_with_groups() -> None:
    [listed] = [item for item in CONVERSATION_EXAMPLES if item.method == "conversations.list"]

    assert [case.name for case in listed.cases] == [
        "one-conversation", "no-conversations", "unknown-parameter", "with-groups",
        "groups-only",
    ]
    with_groups = listed.cases[-2]
    assert list(with_groups.params["kinds"]) == ["direct", "group"]
    kinds = [head["kind"] for head in with_groups.result["conversations"]]
    assert kinds == ["group", "agent", "agent"]


def _server(calls: list[dict[str, Any]]) -> RequestHttpServer:
    config = RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"), owner="h01-owner", actor="user:h01-owner",
        host="127.0.0.1", port=0, allowed_origins=(), allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=Path("/tmp/request-http-runtime"), source=Path("/tmp/config.json"),
    )

    def request_once(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append({"method": method, "params": dict(params)})
        return {"ok": True, "participant": params["from"], "conversations": [],
                "nextCursor": None}

    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item.update(declared=True, reason=None,
                    state="available" if entry["httpExposed"] else "disabled")
        item["callable"] = item["state"] == "available"
        entries.append(item)
    server = RequestHttpServer(config, request_once=request_once)
    view = build_view(
        {"version": __version__, "nodeId": "h01-node", "owner": "h01-owner",
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


def test_http_list_with_kinds_injects_the_owner_and_refuses_a_browser_caller() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)

    served = server._invoke("conversations.list", "http-kinds", time.monotonic(),
                            {"kinds": ["direct", "group"]}, None)
    refused = server._invoke("conversations.list", "http-forged", time.monotonic(),
                             {"kinds": ["group"], "from": "agent:h01-owner:n:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"},
                             None)

    assert served.status == 200, served.payload
    assert calls[0] == {"method": "conversations.list",
                        "params": {"kinds": ["direct", "group"], "from": "user:h01-owner"}}
    assert refused.status == 400
    assert refused.payload["dispatch"] == "not-dispatched"
    assert len(calls) == 1
