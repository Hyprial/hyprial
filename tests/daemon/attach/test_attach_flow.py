"""S8 flow: attach -> send -> pending/read -> reply -> ack -> detach.

A real golden daemon (enforced sender authentication, enforced links, the
production cell runtime and conversation recorder over an isolated OrgFS).
``agent.attach`` registers an agent identity with custody=external and its
agent cell with one external harness member; the daemon never launches or
supervises a harness for it.  The external harness reads and answers
through the existing ``message.*`` methods.  ``agent.detach`` removes the
member and keeps the identity, its ids and its inbox; attach again restores
the member on the same incarnation.  Both are idempotent by name.
"""

from __future__ import annotations

import time
from typing import Any

from tests.daemon.acceptance.acceptance_support import boot_like_services, send
from tests.daemon.attach.attach_support import CUSTODY_PARAM, attach, detach, operator
from tests.support.golden.app import ORG, OWNER_USER, golden_daemon as golden_daemon


def _pending(app: Any, actor: str) -> list[str]:
    listed = app.handle("message.pending.list", operator(actor))
    return [str(item["messageId"]) for item in listed["messages"]]


def _document(app: Any, cell_id: str) -> str | None:
    from hyprial.identity import org_space_name

    fs = app._require_orgfs_runtime().facade.inner
    [space] = [info for info in fs.spaces() if info.name == org_space_name(ORG)]
    try:
        content, _ = fs.read_text(space.space_id, f"cells/agent/{cell_id}.json")
    except Exception:  # noqa: BLE001 - absent document
        return None
    return content


def test_attach_send_read_reply_ack_detach(golden_daemon, monkeypatch) -> None:
    daemon, _recorder = boot_like_services(
        golden_daemon(bypass=False, links=False), monkeypatch
    )
    app = daemon.app

    attached = attach(app, "ext")
    assert attached["ok"] is True and attached["created"] is True
    assert attached["changed"] is True and attached["attached"] is True
    assert attached["custody"] == "external" and attached["name"] == "ext"
    agent = app.agents.get("ext")
    assert agent is not None and attached["actor"] == agent.uri
    assert attached["agentId"] == agent.agent_id and attached["cellId"] == agent.cell_id
    assert dict(agent.capabilities) == {"custody": "external", "attached": True}
    assert attached["cell"]["state"] == "recorded"
    runtime = app._cell_runtime
    cell = runtime.lookup(agent.cell_id)
    assert [(m.ref, m.role) for m in cell.members] == [(agent.uri, "harness")]
    assert cell.param(CUSTODY_PARAM) == "external"
    assert attached["cell"]["revision"] == cell.revision
    runtime.run_once()
    document = _document(app, agent.cell_id)
    assert document is not None and agent.uri in document and CUSTODY_PARAM in document
    assert all(spec.name != "ext" for spec in app.desired_state.load().harnesses)

    again = attach(app, "ext")
    assert again["created"] is False and again["changed"] is False
    assert again["agentId"] == attached["agentId"]
    assert runtime.lookup(agent.cell_id).revision == cell.revision

    sent = send(app, OWNER_USER, agent.uri, "to-ext")
    [delivery] = sent["deliveries"]
    assert delivery["accepted"] is True, sent
    request = str(sent["messageId"])
    assert _pending(app, agent.uri) == [request]
    body = app.handle("message.read_body", {**operator(agent.uri), "messageId": request})
    assert body["message"] == "body to-ext ☃"
    reply = app.handle(
        "message.reply",
        {**operator(agent.uri), "messageId": request, "summary": "answer", "message": "42"},
    )
    assert reply["ok"] is True and reply["replyMessageId"]
    assert _pending(app, agent.uri) == []

    second = str(send(app, OWNER_USER, agent.uri, "ack-me")["messageId"])
    assert _pending(app, agent.uri) == [second]
    acked = app.handle("message.ack", {**operator(agent.uri), "messageId": second})
    assert acked["ok"] is True and _pending(app, agent.uri) == []

    detached = detach(app, "ext")
    assert detached["ok"] is True and detached["changed"] is True
    assert detached["attached"] is False and detached["agentId"] == attached["agentId"]
    assert "created" not in detached
    kept = app.agents.get("ext")
    assert kept is not None and kept.entity_token == agent.entity_token
    assert dict(kept.capabilities) == {"custody": "external", "attached": False}
    retired = runtime.lookup(agent.cell_id)
    assert retired.members == () and retired.retired is False
    assert retired.param(CUSTODY_PARAM) == "external"
    assert detached["cell"] == {"state": "recorded", "revision": retired.revision}
    assert detach(app, "ext")["changed"] is False
    assert detach(app, agent.uri)["changed"] is False

    queued = send(app, OWNER_USER, agent.uri, "while-detached")
    assert queued["deliveries"][0]["accepted"] is True
    assert _pending(app, agent.uri) == [str(queued["messageId"])]

    back = attach(app, "ext")
    assert back["created"] is False and back["changed"] is True
    assert back["agentId"] == attached["agentId"]
    restored = runtime.lookup(agent.cell_id)
    assert [(m.ref, m.role) for m in restored.members] == [(agent.uri, "harness")]
    assert restored.revision == retired.revision + 1


def test_a_record_flip_reaches_the_cell_through_the_registrar(golden_daemon, monkeypatch) -> None:
    from dataclasses import replace

    daemon, _recorder = boot_like_services(
        golden_daemon(bypass=False, links=False), monkeypatch
    )
    app = daemon.app
    attach(app, "ext")
    agent = app.agents.get("ext")
    runtime = app._cell_runtime
    assert runtime.lookup(agent.cell_id).has_member(agent.uri)
    flipped = replace(agent, capabilities={"custody": "external", "attached": False})
    assert app.agents.compare_and_swap(agent, flipped) is True
    deadline = time.monotonic() + 10.0
    while runtime.lookup(agent.cell_id).members and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.lookup(agent.cell_id).members == ()
