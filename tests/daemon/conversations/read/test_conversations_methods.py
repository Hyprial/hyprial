"""S6-read: ``conversations.list`` / ``conversations.history`` on a real daemon.

The methods run through the request registry of a golden daemon (custody
facade, link service, the real send/reply paths).  ``conversations.history``
shares the paging engine of ``message.history`` (the read behind the GUI's
``sessions.history``); the parity tests pin that the old answer equals the
new one wherever one conversation cell spans the whole legacy conversation.
No OrgFS runtime is attached unless a test attaches one, so every read here
also proves the read side works while OrgFS is unavailable.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.conversations import conversation_cell_id
from hyprial.daemon.impl.delivery import DEFAULT_BODY_RETENTION_MS
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.conversations.keys import direct_key
from hyprial.kernel import DaemonRequestError
from tests.daemon.conversations.conversation_support import OWNER_PRINCIPAL
from tests.support.golden.app import (
    ALPHA,
    BETA,
    GAMMA,
    OWNER_USER,
    SESSIONS,
    golden_daemon as golden_daemon,
)

NAMES = {ALPHA: "alpha", BETA: "beta", GAMMA: "gamma"}


def _session(actor: str) -> dict[str, str]:
    return {"from": actor, "sessionRef": SESSIONS[NAMES[actor]]}


def _send(app: Any, sender: str, to: str, conversation: str, key: str) -> str:
    sent = app.handle(
        "message.send",
        {
            **_session(sender),
            "to": [to],
            "summary": f"summary {key}",
            "message": f"body {key}",
            "conversationId": conversation,
            "idempotencyKey": f"key-{key}",
        },
    )
    return str(sent["messageId"])


def _reply(app: Any, sender: str, message_id: str, key: str) -> str:
    replied = app.handle(
        "message.reply",
        {**_session(sender), "messageId": message_id, "summary": key, "message": f"reply {key}"},
    )
    return str(replied["replyMessageId"])


def _cell(a: str, b: str, legacy: str) -> str:
    key = direct_key(a, b)
    thread = None if key is None else key.id
    assert thread is not None
    return conversation_cell_id(OWNER_PRINCIPAL, thread, legacy)


def _traffic(daemon: Any) -> dict[str, str]:
    app = daemon.app
    first = _send(app, ALPHA, BETA, "conv-1", "one")
    daemon.clock.advance(5)
    _reply(app, BETA, first, "one")
    daemon.clock.advance(5)
    _send(app, ALPHA, GAMMA, "conv-1", "side")
    daemon.clock.advance(5)
    for index in range(5):
        _send(app, ALPHA, BETA, "conv-2", f"two-{index}")
        daemon.clock.advance(1)
    return {
        "ab1": _cell(ALPHA, BETA, "conv-1"),
        "ag1": _cell(ALPHA, GAMMA, "conv-1"),
        "ab2": _cell(ALPHA, BETA, "conv-2"),
    }


def _list(app: Any, caller: dict[str, str], **params: Any) -> dict[str, Any]:
    return METHOD_REGISTRY.invoke(app, "conversations.list", {**caller, **params})


def _history(app: Any, caller: dict[str, str], **params: Any) -> dict[str, Any]:
    return METHOD_REGISTRY.invoke(app, "conversations.history", {**caller, **params})


def test_list_names_each_thread_conversation_once_newest_first(golden_daemon) -> None:
    daemon = golden_daemon()
    cells = _traffic(daemon)
    answer = _list(daemon.app, _session(ALPHA))
    assert answer["ok"] is True and answer["participant"] == ALPHA
    assert answer["nextCursor"] is None
    rows = answer["conversations"]
    assert [row["conversationId"] for row in rows] == [cells["ab2"], cells["ag1"], cells["ab1"]]
    by_id = {row["conversationId"]: row for row in rows}
    assert by_id[cells["ab1"]]["messages"] == 2
    assert by_id[cells["ab2"]]["messages"] == 5
    assert by_id[cells["ag1"]]["peer"] == GAMMA
    assert {row["legacyConversationId"] for row in rows} == {"conv-1", "conv-2"}
    assert all(row["kind"] == "agent" for row in rows)
    assert getattr(daemon.app, "_cell_runtime", None) is None


def test_same_node_inbox_row_and_sender_copy_count_once(golden_daemon) -> None:
    daemon = golden_daemon()
    cells = _traffic(daemon)
    alpha = {row["conversationId"]: row for row in _list(daemon.app, _session(ALPHA))["conversations"]}
    beta = {row["conversationId"]: row for row in _list(daemon.app, _session(BETA))["conversations"]}
    assert alpha[cells["ab2"]]["messages"] == beta[cells["ab2"]]["messages"] == 5
    assert alpha[cells["ab1"]]["lastMessageId"] == beta[cells["ab1"]]["lastMessageId"]
    history = _history(daemon.app, _session(BETA), conversationId=cells["ab2"], limit=50)
    ids = [entry["messageId"] for entry in history["entries"]]
    assert len(ids) == len(set(ids)) == 5


def test_list_pages_and_rejects_thread_filter(golden_daemon) -> None:
    daemon = golden_daemon()
    cells = _traffic(daemon)
    first = _list(daemon.app, _session(ALPHA), limit=2)
    assert [row["conversationId"] for row in first["conversations"]] == [cells["ab2"], cells["ag1"]]
    second = _list(daemon.app, _session(ALPHA), limit=2, cursor=first["nextCursor"])
    assert [row["conversationId"] for row in second["conversations"]] == [cells["ab1"]]
    assert second["nextCursor"] is None
    with pytest.raises(DaemonRequestError) as raised:
        _list(daemon.app, _session(ALPHA), threadId=direct_key(ALPHA, BETA).id)
    assert raised.value.code == "INVALID_ARGUMENT"
    with pytest.raises(DaemonRequestError) as raised:
        _list(daemon.app, _session(BETA), cursor=first["nextCursor"])
    assert raised.value.code == "INVALID_ARGUMENT"


def _project(page: dict[str, Any]) -> dict[str, Any]:
    return {
        key: page[key]
        for key in page
        if key not in {"conversationId", "legacyConversationId", "recordAvailable"}
    }


def _unclassed(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assert all(entry["messageClass"] == "interaction" for entry in entries)
    return [{key: value for key, value in entry.items() if key != "messageClass"} for entry in entries]


@pytest.mark.parametrize("caller", [ALPHA, BETA])
def test_history_equals_message_history_for_a_one_thread_conversation(golden_daemon, caller) -> None:
    daemon = golden_daemon()
    cells = _traffic(daemon)
    app = daemon.app
    old_params = {**_session(caller), "conversationId": "conv-2", "limit": 2}
    new_params = {"conversationId": cells["ab2"], "limit": 2}
    for mode in (None, "tail"):
        extra = {} if mode is None else {"mode": mode}
        old = app.handle("message.history", {**old_params, **extra})
        new = _history(app, _session(caller), **new_params, **extra)
        assert new["conversationId"] == cells["ab2"]
        assert new["legacyConversationId"] == "conv-2"
        assert "threadId" not in new
        assert old["conversationId"] == "conv-2"
        if mode is None:
            assert _unclassed(new["entries"]) == old["entries"]
        else:
            assert new["entries"] == old["entries"]
        assert set(_project(new)) == set(_project(old))
    old_pages, new_pages = [], []
    old_cursor = new_cursor = None
    while True:
        old = app.handle("message.history", {**old_params, **({"cursor": old_cursor} if old_cursor else {})})
        new = _history(app, _session(caller), **new_params, **({"cursor": new_cursor} if new_cursor else {}))
        old_pages.append(old["entries"])
        new_pages.append(_unclassed(new["entries"]))
        assert (old["nextCursor"] is None) == (new["nextCursor"] is None)
        if old["nextCursor"] is None:
            break
        old_cursor, new_cursor = old["nextCursor"], new["nextCursor"]
    assert new_pages == old_pages and len(new_pages) == 3
    tail = _history(app, _session(caller), **new_params, mode="tail")
    older = _history(app, _session(caller), **new_params, mode="older", cursor=tail["nextOlderCursor"])
    old_tail = app.handle("message.history", {**old_params, "mode": "tail"})
    old_older = app.handle("message.history", {**old_params, "mode": "older", "cursor": old_tail["nextOlderCursor"]})
    assert older["entries"] == old_older["entries"]
    after = _history(app, _session(caller), **new_params, mode="after", cursor=older["headCursor"])
    assert after["entries"] == [] and after["nextCursor"] is None


def test_history_keeps_only_the_thread_of_a_shared_legacy_id(golden_daemon) -> None:
    daemon = golden_daemon()
    cells = _traffic(daemon)
    app = daemon.app
    legacy = app.handle("message.history", {**_session(ALPHA), "conversationId": "conv-1"})
    assert {entry["to"] for entry in legacy["entries"]} == {BETA, GAMMA, ALPHA}
    beta = _history(app, _session(ALPHA), conversationId=cells["ab1"], limit=1)
    pages = [beta["entries"]]
    while beta["nextCursor"] is not None:
        beta = _history(app, _session(ALPHA), conversationId=cells["ab1"], limit=1, cursor=beta["nextCursor"])
        pages.append(beta["entries"])
    walked = _unclassed([entry for page in pages for entry in page])
    assert walked == [entry for entry in legacy["entries"] if GAMMA not in (entry["from"], entry["to"])]
    gamma = _history(app, _session(ALPHA), conversationId=cells["ag1"], mode="tail")
    assert [entry["to"] for entry in gamma["entries"]] == [GAMMA]


def test_history_of_a_foreign_or_unknown_conversation_is_an_empty_page(golden_daemon) -> None:
    daemon = golden_daemon()
    cells = _traffic(daemon)
    gamma = _history(daemon.app, _session(GAMMA), conversationId=cells["ab1"])
    assert gamma == {
        "ok": True,
        "conversationId": cells["ab1"],
        "entries": [],
        "nextCursor": None,
        "legacyConversationId": None,
        "recordAvailable": False,
    }
    foreign = _history(daemon.app, _session(GAMMA), conversationId=cells["ab2"], mode="tail")
    assert foreign["entries"] == [] and "threadId" not in foreign


def test_identity_rules_match_message_history(golden_daemon) -> None:
    daemon = golden_daemon()
    cells = _traffic(daemon)
    app = daemon.app
    cases = [
        ({"from": ALPHA}, "ipc"),
        ({"from": ALPHA, "sessionRef": "session-bogus"}, None),
        ({}, None),
    ]
    for caller, origin in cases:
        codes = []
        for method, params in (
            ("message.history", {**caller, "conversationId": "conv-2"}),
            ("conversations.history", {**caller, "conversationId": cells["ab2"]}),
            ("conversations.list", dict(caller)),
        ):
            with pytest.raises(DaemonRequestError) as raised:
                app.handle(method, params, _trusted_message_origin=origin)
            codes.append(raised.value.code)
        assert len(set(codes)) == 1, (caller, codes)
    operator = _list(app, {"from": OWNER_USER, "onBehalfOf": ALPHA})
    assert operator["participant"] == ALPHA
    assert len(operator["conversations"]) == 3


def test_schema_refuses_malformed_ids_and_unknown_params(golden_daemon) -> None:
    daemon = golden_daemon()
    _traffic(daemon)
    for method, params in (
        ("conversations.history", {"conversationId": "conv-2"}),
        ("conversations.history", {"conversationId": "t.aaaaaaaaaaaaaaaaaaaaaaaaaa"}),
        ("conversations.list", {"threadId": "c.aaaaaaaaaaaaaaaaaaaaaaaaaa"}),
        ("conversations.list", {"limit": 0}),
        ("conversations.list", {"extra": 1}),
        ("conversations.history", {"conversationId": "c.aaaaaaaaaaaaaaaaaaaaaaaaaa", "mode": "x"}),
    ):
        with pytest.raises(DaemonRequestError) as raised:
            METHOD_REGISTRY.invoke(daemon.app, method, {**_session(ALPHA), **params})
        assert raised.value.code == "INVALID_ARGUMENT", (method, params)


def test_history_hydrates_dropped_bodies_like_message_history(golden_daemon, monkeypatch) -> None:
    daemon = golden_daemon()
    app = daemon.app
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    monkeypatch.setattr(app, "_require_orgfs_runtime", lambda: SimpleNamespace(facade=fs), raising=False)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    request = _send(app, ALPHA, BETA, "conv-h", "h")
    app.handle("message.read_body", {**_session(BETA), "messageId": request})
    reply = _reply(app, BETA, request, "h")
    app.handle("message.ack", {**_session(ALPHA), "messageId": reply})
    app.handle("message.ack", {**_session(BETA), "messageId": request})
    cell = _cell(ALPHA, BETA, "conv-h")
    before = _history(app, _session(ALPHA), conversationId=cell)
    assert recorder.run_once().granted >= 2
    for _ in range(2):
        daemon.clock.advance(DEFAULT_BODY_RETENTION_MS + 86_400_000)
        app._inbox.prune_inbox(now_ms=daemon.clock.ms())
    assert _history(app, _session(ALPHA), conversationId=cell) == before
    assert sorted(entry["message"] for entry in before["entries"]) == ["body h", "reply h"]
    app._inbox.bind_body_retention(resolver=None)
    residue = _history(app, _session(ALPHA), conversationId=cell)
    old = app.handle("message.history", {**_session(ALPHA), "conversationId": "conv-h"})
    assert _unclassed(residue["entries"]) == old["entries"]
    assert [entry["message"] for entry in residue["entries"]] == ["", ""]


def test_an_inbox_without_the_group_read_is_not_ready(golden_daemon) -> None:
    daemon = golden_daemon()
    _traffic(daemon)

    class _Bare:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def conversation_messages(self, *args: Any, **kwargs: Any) -> Any:
            return self._inner.conversation_messages(*args, **kwargs)

    inbox = daemon.app._inbox
    daemon.app._inbox = _Bare(inbox)
    try:
        for method, params in (
            ("conversations.list", {}),
            ("conversations.history", {"conversationId": "c.aaaaaaaaaaaaaaaaaaaaaaaaaa"}),
        ):
            with pytest.raises(DaemonRequestError) as raised:
                METHOD_REGISTRY.invoke(daemon.app, method, {**_session(ALPHA), **params})
            assert raised.value.code == "DAEMON_NOT_READY"
    finally:
        daemon.app._inbox = inbox


STAMP = "t." + "q" * 26


def test_a_stamped_thread_reads_from_the_cell_the_recorder_writes(golden_daemon, monkeypatch) -> None:
    daemon = golden_daemon()
    app = daemon.app
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    monkeypatch.setattr(app, "_require_orgfs_runtime", lambda: SimpleNamespace(facade=fs), raising=False)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    plain = _send(app, ALPHA, BETA, "conv-s", "plain")
    daemon.clock.advance(5)
    with monkeypatch.context() as patch:
        patch.setattr(app, "_conversation_stamp", lambda sender, recipient: STAMP)
        stamped = _send(app, ALPHA, BETA, "conv-s", "stamped")
    assert recorder.run_once().recorded >= 2
    inferred = _cell(ALPHA, BETA, "conv-s")
    stamped_cell = conversation_cell_id(OWNER_PRINCIPAL, STAMP, "conv-s")
    with closing(sqlite3.connect(recorder._store.path)) as db:
        written = {row[0] for row in db.execute("SELECT cell_id FROM records WHERE cell_id IS NOT NULL")}
    assert written == {inferred, stamped_cell}
    for caller in (ALPHA, BETA):
        rows = {row["conversationId"]: row for row in _list(app, _session(caller))["conversations"]}
        assert set(rows) == {inferred, stamped_cell}
        assert "threadId" not in rows[stamped_cell]
        assert rows[stamped_cell]["kind"] is None
        assert rows[stamped_cell]["peer"] == (BETA if caller == ALPHA else ALPHA)
        assert rows[stamped_cell]["messages"] == rows[inferred]["messages"] == 1
        assert rows[inferred]["kind"] == "agent"
        new = _history(app, _session(caller), conversationId=stamped_cell)
        assert [entry["messageId"] for entry in new["entries"]] == [stamped]
        assert "threadId" not in new
        old = _history(app, _session(caller), conversationId=inferred)
        assert [entry["messageId"] for entry in old["entries"]] == [plain]
