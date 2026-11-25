"""HTTP exposure of ``conversations.*``: declaration and projection pins.

The reads share ``threads.*``'s host-injected owner-user selector.  The IPC
caller aliases (``from``, ``actor``, ``sessionRef``, ``onBehalfOf``) are
supplied by the trusted host/MCP caller only; a browser may not report them,
so the HTTP projection omits them and the listener refuses them before
dispatch.  IPC keeps every field with its unchanged meaning.
"""

from __future__ import annotations

from hyprial.daemon import daemon_openapi
from hyprial.daemon.impl.request_catalog.conversations import (
    CONVERSATIONS_HISTORY_SPEC,
    CONVERSATIONS_LIST_SPEC,
)

HOST_ONLY = frozenset({"from", "actor", "sessionRef", "onBehalfOf"})
SPECS = (CONVERSATIONS_LIST_SPEC, CONVERSATIONS_HISTORY_SPEC)


def test_both_reads_bind_the_owner_user_selector_and_stay_read_only() -> None:
    for spec in SPECS:
        assert spec.http_exposed is True
        assert spec.mutates is False
        assert spec.retry == "safe"
        assert spec.audience == ("local-operator", "bound-agent")
        binding = spec.http_binding
        assert binding is not None
        assert binding.identity.mode == "local-owner-user-selector"
        assert binding.identity.param == "from"
        assert binding.readiness is None


def test_host_only_identity_fields_are_declared_and_projected_out() -> None:
    for spec in SPECS:
        properties = spec.params_schema["properties"]
        assert HOST_ONLY <= set(properties)
        for name in HOST_ONLY:
            assert properties[name]["x-hyprial-http-forbidden"] is True, name

    document = daemon_openapi()
    required = {
        "conversations.list": set(),
        "conversations.history": {"conversationId"},
    }
    for name, kept in required.items():
        operation = document["paths"][f"/daemon/request/{name}"]["post"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert HOST_ONLY.isdisjoint(schema.get("properties", {})), name
        assert HOST_ONLY.isdisjoint(schema.get("required", [])), name
        assert kept <= set(schema.get("required", [])), name
        kinds = {case["kind"] for case in operation["x-hyprial-examples"]}
        assert {"success", "empty", "invalid-params"} <= kinds, name
        entry = next(
            item for item in document["x-hyprial-methods"] if item["name"] == name
        )
        assert entry["httpExposed"] is True
        assert entry["httpBinding"] == {
            "identity": {"mode": "local-owner-user-selector", "param": "from"},
            "readiness": None,
        }
        assert entry["x-hyprial-examples"] == operation["x-hyprial-examples"]
