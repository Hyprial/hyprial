"""G0 goldens for the ``message.*`` request surface.

Recorded against the real application, custody inbox authority and link
service: the agent, ``user:`` and ``route:`` send branches, the consumer
reads (list, batch, wait, read_body), reply/ack/status, the non-consuming
query and history reads, the send lookup and the settle hint.  S2 (envelope),
S4 (thread stamping), S5 (delivery split) and S6 (history hydration) must
reproduce every recorded step byte for byte.

The roundtrip runs two ways: ``agent-roundtrip`` with the default link
bypass and ``agent-roundtrip-enforced`` under enforce with no link.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.support.golden.app import (
    ALPHA,
    BETA,
    GAMMA,
    OWNER_USER,
    SESSIONS,
    GoldenDaemon,
    golden_daemon as golden_daemon,
)
from tests.support.golden.snapshot import Golden

GROUP = "message"
ROUTE_GROUP = "route:golden-lark:group"
ROUTE_DM = "route:golden-lark:dm"


def _session(name: str) -> dict[str, str]:
    actor = {"alpha": ALPHA, "beta": BETA, "gamma": GAMMA}[name]
    return {"from": actor, "sessionRef": SESSIONS[name]}


def _send(
    golden: Golden,
    key: str,
    to: list[str],
    *,
    sender: str = "alpha",
    label: str | None = None,
    **extra: Any,
) -> Any:
    return golden.call(
        "message.send",
        {
            **_session(sender),
            "to": to,
            "summary": f"summary {key}",
            "message": f"body {key}",
            "conversationId": f"conv-{key}",
            "idempotencyKey": f"key-{key}",
            **extra,
        },
        label=label,
    )


def _message_id(result: Any) -> str:
    assert isinstance(result, dict) and result.get("ok") is True, result
    return str(result["messageId"])


def _no_settle() -> None:
    return None


def _roundtrip(
    daemon: GoldenDaemon, golden: Golden, settle: Callable[[], None] = _no_settle
) -> Golden:
    request = _message_id(_send(golden, "request", [BETA]))
    settle()
    _send(golden, "request", [BETA], label="idempotent-replay")
    golden.call("message.query", {"from": BETA, "view": "inbox"}, label="peer-inbox")
    golden.call("message.query", {"from": ALPHA, "view": "outbox"}, label="sender-outbox")
    golden.call("message.pending.wait", {**_session("beta"), "knownMessageIds": [], "holdMs": 0})
    golden.call("message.pending.list", {**_session("beta")})
    golden.call("message.status", {**_session("alpha"), "messageId": request}, label="sent")
    golden.call("message.read_body", {**_session("beta"), "messageId": request})
    golden.call("message.read_body", {**_session("beta"), "messageId": request}, label="repeat")
    golden.call("message.settle_hint", {**_session("beta"), "messageId": request})
    golden.call("message.pending.batch", {**_session("beta")})
    golden.call(
        "message.pending.wait",
        {**_session("beta"), "knownMessageIds": [request], "holdMs": 0},
        label="known",
    )
    reply = golden.call(
        "message.reply",
        {**_session("beta"), "messageId": request, "summary": "answer", "message": "the answer"},
    )
    settle()
    golden.call(
        "message.reply",
        {**_session("beta"), "messageId": request, "summary": "answer", "message": "the answer"},
        label="second-reply",
    )
    golden.call("message.status", {**_session("alpha"), "messageId": request}, label="answered")
    golden.call("message.pending.list", {**_session("alpha")}, label="reply-arrived")
    reply_id = reply.get("replyMessageId") if isinstance(reply, dict) else None
    if isinstance(reply_id, str):
        golden.call("message.read_body", {**_session("alpha"), "messageId": reply_id})
        golden.call("message.ack", {**_session("alpha"), "messageId": reply_id})
        settle()
        golden.call("message.ack", {**_session("alpha"), "messageId": reply_id}, label="repeat")
    golden.call("message.ack", {**_session("beta"), "messageId": request}, label="request")
    golden.call("message.pending.list", {**_session("beta")}, label="after-ack")
    for name in ("alpha", "beta"):
        golden.call(
            "message.history",
            {**_session(name), "conversationId": "conv-request"},
            label=name,
        )
    golden.call(
        "message.history",
        {**_session("gamma"), "conversationId": "conv-request"},
        label="non-participant",
    )
    golden.call(
        "message.history",
        {**_session("alpha"), "conversationId": "conv-request", "limit": 1},
        label="limit-1",
    )
    golden.call("message.status", {**_session("alpha")}, label="all")
    golden.call("message.query", {"from": BETA, "view": "inbox"}, label="peer-inbox-after")
    golden.call("outbox.list", {})
    golden.call("outbox.prune", {"dryRun": True}, label="dry-run")
    golden.call("progress.list", {**_session("alpha")})
    golden.observe("network-leg", daemon.transport.delivered)
    return golden


def scenario_agent_roundtrip(daemon: GoldenDaemon) -> Golden:
    return _roundtrip(daemon, daemon.golden(GROUP, "agent-roundtrip"))


def scenario_operator(daemon: GoldenDaemon) -> Golden:
    golden = daemon.golden(GROUP, "operator")
    sent = golden.call(
        "message.send",
        {
            "from": OWNER_USER,
            "onBehalfOf": ALPHA,
            "to": [BETA],
            "summary": "operator",
            "message": "operator body",
            "conversationId": "conv-operator",
            "idempotencyKey": "key-operator",
        },
        label="on-behalf-of",
    )
    golden.call(
        "message.send",
        {
            "from": OWNER_USER,
            "to": [BETA],
            "summary": "owner",
            "message": "owner body",
            "conversationId": "conv-owner",
            "idempotencyKey": "key-owner",
        },
        label="as-owner",
    )
    golden.call("message.pending.list", {"from": BETA}, label="unfenced")
    golden.call("message.pending.list", {"from": OWNER_USER, "onBehalfOf": BETA}, label="on-behalf-of")
    golden.call("message.query", {"from": "beta", "view": "inbox"}, label="bare-name")
    message_id = sent.get("messageId") if isinstance(sent, dict) else None
    if isinstance(message_id, str):
        golden.call(
            "message.status",
            {"from": OWNER_USER, "onBehalfOf": ALPHA, "messageId": message_id},
        )
        golden.call(
            "message.ack", {"from": OWNER_USER, "onBehalfOf": BETA, "messageId": message_id}
        )
    golden.call(
        "message.history",
        {"from": OWNER_USER, "onBehalfOf": ALPHA, "conversationId": "conv-operator"},
    )
    golden.call("message.send", {"from": "alpha", "to": [BETA], "summary": "s", "message": "m",
                                 "conversationId": "conv-bare", "idempotencyKey": "key-bare"},
                label="bare-claim-without-session")
    return golden


def scenario_user_and_route(daemon: GoldenDaemon) -> Golden:
    golden = daemon.golden(GROUP, "user-and-route")
    _send(golden, "user", [OWNER_USER])
    _send(golden, "user", [OWNER_USER], label="idempotent-replay")
    golden.call(
        "message.send.lookup",
        {**_session("alpha"), "target": OWNER_USER, "text": "body user",
         "conversationId": "conv-user", "idempotencyKey": "key-user"},
        label="user-target-refused",
    )
    golden.call(
        "message.send.lookup",
        {**_session("alpha"), "target": OWNER_USER, "text": "different text",
         "conversationId": "conv-user", "idempotencyKey": "key-user"},
        label="user-target-refused-text-mismatch",
    )
    golden.call(
        "message.send.lookup",
        {**_session("alpha"), "target": OWNER_USER, "text": "body never",
         "conversationId": "conv-never", "idempotencyKey": "key-never"},
        label="user-target-refused-unknown",
    )
    _send(golden, "user-other", ["user:reviewer"], label="other-user")
    _send(golden, "route-group", [ROUTE_GROUP])
    _send(golden, "route-group", [ROUTE_GROUP], label="idempotent-replay")
    _send(golden, "route-dm", [ROUTE_DM])
    _send(golden, "route-missing", ["route:golden-lark:missing"], label="unknown-route")
    _send(golden, "route-adapter", ["route:no-such-adapter:group"], label="unknown-adapter")
    _send(golden, "route-bad", ["route:golden-lark"], label="malformed-route")
    _send(golden, "fanout", [BETA, OWNER_USER, ROUTE_GROUP], label="mixed-targets")
    for label, text, key in (
        ("agent-target", "body fanout", "fanout"),
        ("agent-text-mismatch", "different text", "fanout"),
        ("agent-unknown", "body never", "never"),
    ):
        golden.call(
            "message.send.lookup",
            {**_session("alpha"), "target": BETA, "text": text,
             "conversationId": f"conv-{key}", "idempotencyKey": f"key-{key}"},
            label=label,
        )
    golden.call(
        "message.send.lookup",
        {"from": OWNER_USER, "onBehalfOf": ALPHA, "target": BETA, "text": "body fanout",
         "conversationId": "conv-fanout", "idempotencyKey": "key-fanout"},
        label="operator-on-behalf-of-schema-refused",
    )
    golden.observe("lark-route-calls", daemon.gateway.calls)
    golden.observe(
        "user-deliveries",
        [
            {"messageId": r.message_id, "key": r.idempotency_key, "owner": r.owner,
             "sender": r.sender, "conversationId": r.conversation_id}
            for r in daemon.user_delivery.requests
        ],
    )
    return golden


def scenario_refusals(daemon: GoldenDaemon) -> Golden:
    golden = daemon.golden(GROUP, "refusals")
    base = {**_session("alpha"), "message": "m", "conversationId": "conv-r", "idempotencyKey": "key-r"}
    golden.call("message.send", {**base, "to": [BETA]}, label="missing-summary")
    golden.call("message.send", {**base, "summary": "s", "to": []}, label="empty-to")
    golden.call("message.send", {**base, "summary": "s"}, label="missing-to")
    golden.call("message.send", {**base, "summary": "s", "to": [BETA], "replyTo": 7}, label="bad-reply-to")
    golden.call("message.send", {**base, "summary": "s", "to": [BETA], "message": ""}, label="empty-message")
    golden.call(
        "message.send",
        {**base, "summary": "s", "to": [BETA],
         "resourcePaths": [{"path": str(daemon.root / "x.txt"), "kind": "file"}]},
        label="resources-on-agent",
    )
    golden.call(
        "message.send",
        {**base, "summary": "s", "to": [BETA], "sessionRef": "session-bogus"},
        label="stale-session",
    )
    golden.call(
        "message.send",
        {**base, "summary": "s", "to": [BETA], "onBehalfOf": GAMMA},
        label="agent-on-behalf-of",
    )
    golden.call("message.send", {**base, "summary": "s", "to": ["channel:nowhere"]}, label="bad-target")
    golden.call(
        "message.reply",
        {**_session("beta"), "messageId": "no-such-message", "summary": "s", "message": "m"},
        label="unknown-request",
    )
    golden.call("message.reply", {**_session("beta"), "summary": "s", "message": "m"}, label="missing-id")
    golden.call("message.ack", {**_session("beta"), "messageId": "no-such-message"}, label="unknown")
    golden.call("message.read_body", {**_session("beta"), "messageId": "no-such-message"}, label="unknown")
    golden.call("message.read_body", {"from": BETA, "messageId": "no-such-message"}, label="unfenced")
    golden.call("message.status", {**_session("alpha"), "messageId": "no-such-message"}, label="unknown")
    golden.call("message.history", {**_session("alpha")}, label="missing-conversation")
    golden.call(
        "message.history",
        {**_session("alpha"), "conversationId": "conv-r", "cursor": "not-a-cursor"},
        label="bad-cursor",
    )
    golden.call(
        "message.pending.wait", {**_session("beta"), "holdMs": -1}, label="negative-hold"
    )
    golden.call(
        "message.pending.wait", {**_session("beta"), "knownMessageIds": "x"}, label="bad-known"
    )
    golden.call(
        "message.pending.batch",
        {**_session("beta"), "cursor": "a", "afterCursor": "b"},
        label="cursor-and-after",
    )
    golden.call("message.pending.batch", {**_session("beta"), "cursor": "unknown"}, label="unknown-cursor")
    golden.call("message.pending.list", {**_session("beta"), "fetched": True}, label="fetched-marker")
    golden.call("message.pending.list", {"from": BETA, "fetched": True}, label="unfenced-fetch")
    golden.call("message.query", {"from": BETA, "view": "drafts"}, label="bad-view")
    golden.call("message.query", {"from": BETA}, label="missing-view")
    golden.call("message.send.lookup", {**_session("alpha"), "target": OWNER_USER}, label="schema")
    golden.call("message.settle_hint", {**_session("beta")}, label="missing-id")
    return golden


SCENARIOS: dict[str, tuple[dict[str, bool], Callable[[GoldenDaemon], Golden]]] = {
    "agent-roundtrip": ({}, scenario_agent_roundtrip),
    "agent-roundtrip-enforced": ({"bypass": False}, scenario_agent_roundtrip),
    "operator": ({}, scenario_operator),
    "user-and-route": ({}, scenario_user_and_route),
    "refusals": ({}, scenario_refusals),
}


def _run(build: Callable[..., GoldenDaemon], name: str) -> Golden:
    options, scenario = SCENARIOS[name]
    golden = scenario(build(**options))
    golden.name = name
    return golden


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_message_golden(golden_daemon: Callable[..., GoldenDaemon], name: str) -> None:
    _run(golden_daemon, name).check()
