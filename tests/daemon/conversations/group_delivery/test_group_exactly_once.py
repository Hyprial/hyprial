"""Each woken member gets its row once, across retries and restarts (slice GC4).

Draft §4.3 on one machine: the row id and idempotency key derive from
``(group, entry, member)``, so the inbox's own dedup makes every repeat a
no-op; the restart pass reads the group documents and creates the rows a
stopped daemon never created, once.  Entries older than the inbox's dedup
window are never delivered, so an acknowledged row is never re-created.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from hyprial.daemon.impl.inbox.service.core import InboxServiceCoreMixin
from tests.daemon.conversations.group_delivery.delivery_support import (
    batch,
    group_daemon,
    group_rows,
    make_group,
    reply,
    row_ids,
    send,
    sweep,
    woken,
    write_unannounced,
)
from tests.support.golden.app import golden_daemon as golden_daemon


def test_the_row_id_is_per_group_entry_and_member(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))

    sent = send(booted, group, "please cut the release", key="once-id-1",
                mentions=[booted.agent_id("alpha")])

    (row,) = group_rows(booted, "alpha", group)
    member = booted.agent_id("alpha")
    assert row.message_id == str(uuid5(
        NAMESPACE_URL, f"hyprial:group-row:{group}:{sent['messageId']}:{member}"))
    assert row.idempotency_key == f"group:{group}:{sent['messageId']}:{member}"


def test_a_replayed_send_gives_no_second_row(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    mentions = [booted.agent_id("alpha"), booted.agent_id("beta")]
    send(booted, group, "please cut the release", key="once-replay-1", mentions=mentions)
    first = {name: row_ids(booted, name, group) for name in ("alpha", "beta")}

    again = send(booted, group, "please cut the release", key="once-replay-1", mentions=mentions)

    assert again["deliveries"][0]["duplicate"] is True
    assert {name: row_ids(booted, name, group) for name in ("alpha", "beta")} == first
    assert all(len(ids) == 1 for ids in first.values())


def test_a_restart_between_entry_and_rows_creates_them_once(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    booted.advance()
    write_unannounced(booted, group, "written before the stop", entry_id="entry-crash-1",
                      at_ms=booted.daemon.clock.ms(),
                      mentions=(booted.agent_id("alpha"), booted.agent_id("beta")))
    assert group_rows(booted, "alpha") == [] and group_rows(booted, "beta") == []

    sweep(booted)
    sweep(booted)

    for name in ("alpha", "beta"):
        assert len(group_rows(booted, name, group)) == 1, name
        assert woken(booted, name)


def test_a_second_pass_after_the_ack_creates_nothing(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    send(booted, group, "please cut the release", key="once-acked-1",
         mentions=[booted.agent_id("alpha")])
    (row,) = group_rows(booted, "alpha", group)
    batch(booted, "alpha")
    reply(booted, "alpha", row.message_id, "done")
    assert group_rows(booted, "alpha") == []

    sweep(booted)

    assert group_rows(booted, "alpha") == []


def test_the_pass_skips_entries_older_than_the_dedup_window(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    write_unannounced(booted, group, "too old to deliver", entry_id="entry-old-1",
                      at_ms=booted.daemon.clock.ms(),
                      mentions=(booted.agent_id("alpha"),))
    booted.advance(InboxServiceCoreMixin.dedup_window_ms + 1)

    sweep(booted)

    assert group_rows(booted, "alpha") == []


def test_the_row_window_is_the_inbox_dedup_window() -> None:
    from hyprial.daemon.impl.application.messaging.inbox_surface.groups.delivery import (
        ROW_WINDOW_MS,
    )

    assert ROW_WINDOW_MS == InboxServiceCoreMixin.dedup_window_ms


def test_the_daemon_start_runs_the_pass_on_its_own_thread(golden_daemon, monkeypatch) -> None:
    import inspect

    from hyprial.daemon.impl.composition.conversations import group_delivery, wiring

    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    booted.advance()
    write_unannounced(booted, group, "written before the stop", entry_id="entry-start-1",
                      at_ms=booted.daemon.clock.ms(),
                      mentions=(booted.agent_id("alpha"),))

    thread = group_delivery.start_group_catch_up(booted.app)
    assert thread is not None and thread.name == group_delivery.CATCH_UP_THREAD
    thread.join(10)
    assert not thread.is_alive()
    assert group_delivery.start_group_catch_up(booted.app) is None
    group_delivery.close_group_catch_up(booted.app)

    assert len(group_rows(booted, "alpha", group)) == 1
    source = inspect.getsource(wiring.compose_conversation_recorder)
    assert "start_group_catch_up(app)" in source
    assert "close_group_catch_up(app)" in inspect.getsource(wiring.close_conversation_recorder)


def test_a_member_added_after_an_entry_gets_no_row_for_it(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    booted.advance()
    write_unannounced(booted, group, "before beta joined", entry_id="entry-early-1",
                      at_ms=booted.daemon.clock.ms(),
                      mentions=(booted.agent_id("alpha"), booted.agent_id("beta")))
    booted.advance()
    from tests.daemon.conversations.group_delivery.delivery_support import handle

    handle(booted, "conversations.members.add", conversationId=group, member="beta",
           idempotencyKey="add-beta-late")

    sweep(booted)

    assert len(group_rows(booted, "alpha", group)) == 1
    assert group_rows(booted, "beta") == []


def test_the_pass_without_a_local_transport_raises(golden_daemon, monkeypatch) -> None:
    import pytest

    booted = group_daemon(golden_daemon, monkeypatch)
    monkeypatch.setattr(booted.app, "_local_delivery", None)

    with pytest.raises(RuntimeError):
        sweep(booted)
