"""The adapter runtime state is one real SQLite database.

Covers the storage-cutover guarantees: honest file format, one shared
database with per-adapter namespacing, concurrent multi-process writes,
indexed dedup lookups, queryable dead letters, the meta schema-version
guard, and the no-migration retire path for JSON-era files.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark.state.readers import (
    ObservedChat,
    observed_chats,
    replied_chats,
    retire_legacy_state,
)
from hyprial.daemon.impl.adapters.lark.state.records import (
    STATE_SCHEMA_VERSION,
    DeadLetter,
    ReplyRoute,
    RequestCorrelation,
)
from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore
_SQLITE_MAGIC = b"SQLite format 3\x00"


def letter(message_id: str, **overrides: str) -> DeadLetter:
    values = {
        "message_id": message_id,
        "event_id": f"evt-{message_id}",
        "chat_id": "oc_dm",
        "chat_type": "p2p",
        "conversation_id": "lark:oc_dm:oc_dm",
        "sender_id": "ou_sender",
        "sender_type": "user",
        "text": "body",
        "reason": "forward-error",
        "created_at": "2026-08-14T00:00:00+00:00",
    }
    values.update(overrides)
    return DeadLetter(**values)  # type: ignore[arg-type]


def test_database_is_real_sqlite_with_meta_schema_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adapters.sqlite3"
    store = LarkStateStore(path, adapter="lark:main")
    store.record_seen("evt-1")

    assert path.read_bytes()[: len(_SQLITE_MAGIC)] == _SQLITE_MAGIC
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT value FROM meta WHERE key = 'schemaVersion'"
        ).fetchone()
    assert row == (str(STATE_SCHEMA_VERSION),)


def test_retired_chats_leave_the_scan_set_but_keep_their_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adapters.sqlite3"
    alpha = LarkStateStore(path, adapter="lark:alpha")
    alpha.record_inbound(
        RequestCorrelation(
            harness_message_id="hm-1",
            message_id="om-1",
            chat_id="oc_gone",
            conversation_id="lark:oc_gone:oc_gone",
        ),
        event_id="evt-inbound",
    )
    alpha.record_dead_letter(letter("om-dead"))
    assert alpha.recent_chats() == ("oc_dm", "oc_gone")

    retired_at = datetime(2026, 8, 20, tzinfo=UTC)
    assert alpha.retire_chat("oc_gone", code=230002, now=retired_at)
    # Idempotent: the first observation stands.
    assert not alpha.retire_chat("oc_gone", code=230002, now=retired_at)
    assert alpha.retired_chat_code("oc_gone") == 230002
    assert alpha.retired_chat_code("oc_dm") is None
    assert alpha.recent_chats() == ("oc_dm",)
    # Retirement excludes the chat from sweeps only; audit rows stay.
    assert alpha.request("hm-1") is not None

    # The marker is durable state: a restarted worker keeps it, and another
    # adapter sharing the database is unaffected.
    reopened = LarkStateStore(path, adapter="lark:alpha")
    assert reopened.recent_chats() == ("oc_dm",)
    assert reopened.retired_chat_code("oc_gone") == 230002
    beta = LarkStateStore(path, adapter="lark:beta")
    assert beta.retired_chat_code("oc_gone") is None

    # unretire_chat's return value reports whether a row was actually
    # lifted: True exactly once, False once the row is gone.
    assert reopened.unretire_chat("oc_gone") is True
    assert reopened.retired_chat_code("oc_gone") is None
    assert reopened.recent_chats() == ("oc_dm", "oc_gone")
    assert reopened.unretire_chat("oc_gone") is False
    assert reopened.unretire_chat("oc_never_retired") is False


def test_observed_chats_answers_first_run_without_creating_state(
    tmp_path: Path,
) -> None:
    """First-run asks which chat the user already talked to this bot in.

    That question is asked before any route exists and must not create what it is
    asking about: a machine with no database gets an empty answer and stays
    empty.  Once inbound traffic exists the answer is ordered by chat id, carries
    the recorded chat type, is namespaced per adapter, and excludes retired chats
    exactly like ``recent_chats``.  The file is not rewritten by the read.
    """

    path = tmp_path / "adapters.sqlite3"
    assert observed_chats(path, adapter="lark:alpha") == ()
    assert not path.exists()

    alpha = LarkStateStore(path, adapter="lark:alpha")
    alpha.record_inbound(
        RequestCorrelation(
            harness_message_id="hm-1",
            message_id="om-1",
            chat_id="oc_dm",
            conversation_id="lark:oc_dm:oc_dm",
        ),
        event_id="evt-inbound",
    )
    alpha.record_chat_type("oc_dm", "p2p")
    alpha.record_dead_letter(letter("om-dead", chat_id="oc_group", chat_type="group"))

    before = path.read_bytes()
    assert observed_chats(path, adapter="lark:alpha") == (
        ObservedChat(chat_id="oc_dm", chat_type="p2p"),
        ObservedChat(chat_id="oc_group", chat_type="group"),
    )
    # Another adapter sharing the database is a different namespace.
    assert observed_chats(path, adapter="lark:beta") == ()
    assert path.read_bytes() == before

    alpha.retire_chat("oc_group", code=230002, now=datetime(2026, 8, 20, tzinfo=UTC))
    assert observed_chats(path, adapter="lark:alpha") == (
        ObservedChat(chat_id="oc_dm", chat_type="p2p"),
    )


def test_observed_chats_treats_a_foreign_file_as_absent(tmp_path: Path) -> None:
    """A path that is not a database is absence of evidence, not a crash."""

    path = tmp_path / "adapters.sqlite3"
    path.write_text("not a database", encoding="utf-8")

    assert observed_chats(path, adapter="lark:alpha") == ()


def test_replied_chats_needs_a_paired_exchange_not_a_claim(
    tmp_path: Path,
) -> None:
    """First-run asks whether two-way messaging actually works.

    The only honest answer is a completed exchange recorded by the adapter: an
    inbound message that was forwarded (``request_correlations``) and the native
    id the platform returned for its reply (``reply_routes``), matched on one
    harness message id in one chat.  Inbound traffic alone, or a reply recorded
    against some other message, is not a round trip.  A machine with no database
    answers empty and stays empty, the answer is namespaced per adapter, retired
    chats stop counting exactly like ``recent_chats``, and the read does not
    rewrite the file.
    """

    path = tmp_path / "adapters.sqlite3"
    assert replied_chats(path, adapter="lark:alpha") == ()
    assert not path.exists()

    alpha = LarkStateStore(path, adapter="lark:alpha")
    alpha.record_inbound(
        RequestCorrelation(
            harness_message_id="hm-1",
            message_id="om-1",
            chat_id="oc_dm",
            conversation_id="lark:oc_dm:oc_dm",
        ),
        event_id="evt-inbound",
    )
    # Inbound only: the platform never confirmed a send back.
    assert replied_chats(path, adapter="lark:alpha") == ()

    # A reply that cannot be paired with that inbound message proves nothing.
    alpha.record_reply(
        ReplyRoute(
            message_id="om-other",
            actor_id="agent:owner:machine:default",
            actor_key="default",
            harness_message_id="hm-other",
            conversation_id="lark:oc_dm:oc_dm",
            chat_id="oc_dm",
        )
    )
    assert replied_chats(path, adapter="lark:alpha") == ()

    alpha.record_reply(
        ReplyRoute(
            message_id="om-reply",
            actor_id="agent:owner:machine:default",
            actor_key="default",
            harness_message_id="hm-1",
            conversation_id="lark:oc_dm:oc_dm",
            chat_id="oc_dm",
        )
    )
    before = path.read_bytes()
    assert replied_chats(path, adapter="lark:alpha") == ("oc_dm",)
    # Another adapter sharing the database is a different namespace.
    assert replied_chats(path, adapter="lark:beta") == ()
    assert path.read_bytes() == before

    # A chat the platform permanently refused is no more evidence of a working
    # channel than it is a routing candidate.
    alpha.retire_chat("oc_dm", code=230002, now=datetime(2026, 8, 20, tzinfo=UTC))
    assert replied_chats(path, adapter="lark:alpha") == ()


def test_replied_chats_treats_a_foreign_file_as_absent(tmp_path: Path) -> None:
    """A path that is not a database is absence of evidence, not a crash."""

    path = tmp_path / "adapters.sqlite3"
    path.write_text("not a database", encoding="utf-8")

    assert replied_chats(path, adapter="lark:alpha") == ()


def test_unknown_schema_version_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "adapters.sqlite3"
    LarkStateStore(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value = '999' WHERE key = 'schemaVersion'")

    with pytest.raises(ValueError, match="unsupported Lark state schema"):
        LarkStateStore(path)


def test_adapters_share_one_database_but_never_each_others_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adapters.sqlite3"
    alpha = LarkStateStore(path, adapter="lark:alpha")
    beta = LarkStateStore(path, adapter="lark:beta")

    alpha.record_seen("evt-shared-id")
    alpha.record_inbound(
        RequestCorrelation(
            harness_message_id="hm-1",
            message_id="om-1",
            chat_id="oc_alpha",
            conversation_id="lark:oc_alpha:oc_alpha",
        ),
        event_id="evt-inbound",
    )
    alpha.record_dead_letter(letter("om-dead"))
    alpha.set_conversation_timezone("lark:oc_alpha:oc_alpha", "Asia/Taipei")

    # Same ids, other namespace: invisible.
    assert not beta.seen("evt-shared-id")
    assert not beta.seen_message("om-1")
    assert beta.request("hm-1") is None
    assert beta.dead_letters() == ()
    assert beta.recent_chats() == ()
    assert beta.conversation_timezone("lark:oc_alpha:oc_alpha") is None

    # And the namespace that wrote them still sees them.
    assert alpha.seen("evt-shared-id")
    assert alpha.request("hm-1") is not None
    assert len(alpha.dead_letters()) == 1

    # One file on disk serves both.
    assert not (tmp_path / "adapters-alpha.sqlite3").exists()
    databases = {item.name for item in tmp_path.iterdir()}
    assert "adapters.sqlite3" in databases


def test_two_adapter_processes_write_concurrently_without_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adapters.sqlite3"
    count = 150
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hyprial.daemon.impl.adapters.lark.state.records import DeadLetter\nfrom hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore\n"
        "path, adapter, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])\n"
        "store = LarkStateStore(path, adapter=adapter)\n"
        "for index in range(count):\n"
        "    store.record_seen(f'evt-{adapter}-{index}')\n"
        "    store.record_dead_letter(DeadLetter(\n"
        "        message_id=f'om-{adapter}-{index}',\n"
        "        event_id=f'evt-{adapter}-{index}',\n"
        "        chat_id=f'oc-{adapter}',\n"
        "        chat_type='p2p',\n"
        "        conversation_id=f'lark:oc-{adapter}:oc-{adapter}',\n"
        "        sender_id='ou_sender',\n"
        "        sender_type='user',\n"
        "        text='body',\n"
        "        reason='forward-error',\n"
        "        created_at='2026-08-14T00:00:00+00:00',\n"
        "    ))\n"
        "store.close()\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), adapter, str(count)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for adapter in ("lark:alpha", "lark:beta")
    ]
    try:
        for process in processes:
            _stdout, stderr = process.communicate(timeout=120)
            assert process.returncode == 0, stderr.decode()
    finally:
        # A failed first child must not abandon its still-running peer/pipes.
        # These are exactly the processes created by this test, not daemons.
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    for adapter in ("lark:alpha", "lark:beta"):
        store = LarkStateStore(path, adapter=adapter)
        try:
            for index in range(count):
                assert store.seen(f"evt-{adapter}-{index}")
            assert len(store.dead_letters()) == count
        finally:
            store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_dedup_lookups_hit_an_index_not_a_scan(tmp_path: Path) -> None:
    path = tmp_path / "adapters.sqlite3"
    store = LarkStateStore(path, adapter="lark:main")
    store.record_seen("evt-1")
    store.record_consumed("evt-2", "om-2")

    with sqlite3.connect(path) as db:
        for table, key in (
            ("seen_events", "event_id"),
            ("seen_messages", "message_id"),
        ):
            plan = " ".join(
                row[3]
                for row in db.execute(
                    f"EXPLAIN QUERY PLAN SELECT 1 FROM {table}"
                    f" WHERE adapter = ? AND {key} = ?",
                    ("lark:main", "probe"),
                ).fetchall()
            )
            assert "SEARCH" in plan, plan
            assert f"SCAN {table}" not in plan, plan


def test_dead_letters_are_queryable_by_reason_chat_and_time(
    tmp_path: Path,
) -> None:
    store = LarkStateStore(tmp_path / "adapters.sqlite3", adapter="lark:main")
    store.record_dead_letter(
        letter(
            "om-old",
            reason="forward-error",
            chat_id="oc_a",
            created_at="2026-08-01T00:00:00+00:00",
        )
    )
    store.record_dead_letter(
        letter(
            "om-recent",
            reason="forward-error",
            chat_id="oc_b",
            created_at="2026-08-14T09:00:00+00:00",
        )
    )
    store.record_dead_letter(
        letter(
            "om-unsupported",
            reason="unsupported-message-type",
            chat_id="oc_a",
            created_at="2026-08-14T10:00:00+00:00",
        )
    )

    by_reason = store.dead_letters(reason="forward-error")
    assert {item.message_id for item in by_reason} == {"om-old", "om-recent"}
    by_chat = store.dead_letters(chat_id="oc_a")
    assert {item.message_id for item in by_chat} == {"om-old", "om-unsupported"}
    recent = store.dead_letters(since="2026-08-14T00:00:00+00:00")
    assert {item.message_id for item in recent} == {"om-recent", "om-unsupported"}
    combined = store.dead_letters(
        reason="forward-error", since="2026-08-14T00:00:00+00:00"
    )
    assert [item.message_id for item in combined] == ["om-recent"]


def test_dead_letter_summary_counts_reasons_without_loading_bodies(
    tmp_path: Path,
) -> None:
    store = LarkStateStore(tmp_path / "adapters.sqlite3", adapter="lark:main")
    store.record_dead_letter(
        letter(
            "om-old",
            reason="forward-error",
            created_at="2026-08-14T08:59:59+00:00",
        )
    )
    store.record_dead_letter(
        letter(
            "om-one",
            reason="forward-error",
            created_at="2026-08-14T09:00:00+00:00",
        )
    )
    store.record_dead_letter(
        letter(
            "om-two",
            reason="no-target",
            created_at="2026-08-14T09:01:00+00:00",
        )
    )

    assert store.dead_letter_summary(since="2026-08-14T09:00:00+00:00") == (
        2,
        {"forward-error": 1, "no-target": 1},
    )


def test_dead_letter_alert_claim_is_atomic_and_survives_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adapters.sqlite3"
    first = LarkStateStore(path, adapter="lark:main")
    second = LarkStateStore(path, adapter="lark:main")
    window = "2026-08-14T09:00:00+00:00"

    assert first.claim_dead_letter_alert(
        window_start=window, claimed_at="2026-08-14T09:10:00+00:00"
    )
    assert not second.claim_dead_letter_alert(
        window_start=window, claimed_at="2026-08-14T09:11:00+00:00"
    )

    reopened = LarkStateStore(path, adapter="lark:main")
    assert not reopened.claim_dead_letter_alert(
        window_start=window, claimed_at="2026-08-14T09:12:00+00:00"
    )
    reopened.release_dead_letter_alert(window_start=window)
    assert second.claim_dead_letter_alert(
        window_start=window, claimed_at="2026-08-14T09:13:00+00:00"
    )


def test_dead_letter_round_trips_optional_fields(tmp_path: Path) -> None:
    store = LarkStateStore(tmp_path / "adapters.sqlite3", adapter="lark:main")
    full = letter(
        "om-full",
        detail="stack detail",
        create_time="1723600000000",
        reply_to="om-parent",
        message_type="post",
    )
    minimal = letter("om-minimal")
    store.record_dead_letter(full)
    store.record_dead_letter(minimal)

    stored = {item.message_id: item for item in store.dead_letters()}
    assert stored["om-full"] == full
    assert stored["om-minimal"] == minimal
    assert stored["om-minimal"].detail is None
    assert stored["om-minimal"].message_type is None


def test_legacy_json_file_is_retired_unread_and_kept(tmp_path: Path) -> None:
    legacy_path = (
        tmp_path / "adapters" / "lark" / "main" / "state.sqlite3"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_payload = json.dumps(
        {
            "schemaVersion": 1,
            "requestCorrelations": {},
            "replyRoutes": {},
            "seenEvents": {"evt-legacy": True},
            "seenMessages": {"om-legacy": True},
            "deadLetters": {
                "om-legacy": {
                    "message_id": "om-legacy",
                    "event_id": "evt-legacy",
                    "chat_id": "oc_dm",
                    "chat_type": "p2p",
                    "conversation_id": "lark:oc_dm:oc_dm",
                    "sender_id": "ou_sender",
                    "sender_type": "user",
                    "text": "audit me later",
                    "reason": "forward-error",
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "detail": None,
                    "create_time": None,
                    "reply_to": None,
                    "message_type": None,
                }
            },
            "chatTypes": {},
            "conversationTimezones": {},
            "pendingCommandResponses": {},
        }
    )
    legacy_path.write_text(legacy_payload, encoding="utf-8")

    store = LarkStateStore(
        tmp_path / "adapters.sqlite3",
        adapter="lark:main",
        legacy_path=legacy_path,
    )

    # Fresh start: nothing was migrated, the old ids are unknown.
    assert not store.seen("evt-legacy")
    assert not store.seen_message("om-legacy")
    assert store.dead_letters() == ()
    # The old file was renamed aside, byte for byte, not read and not deleted.
    assert not legacy_path.exists()
    retired = legacy_path.with_name("state.sqlite3.retired")
    assert retired.read_text(encoding="utf-8") == legacy_payload


def test_json_file_at_database_path_is_retired_not_parsed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adapters.sqlite3"
    legacy_payload = json.dumps({"schemaVersion": 1, "seenEvents": {"evt": True}})
    path.write_text(legacy_payload, encoding="utf-8")

    store = LarkStateStore(path, adapter="lark:main")

    assert not store.seen("evt")
    assert path.read_bytes()[: len(_SQLITE_MAGIC)] == _SQLITE_MAGIC
    retired = path.with_name("adapters.sqlite3.retired")
    assert retired.read_text(encoding="utf-8") == legacy_payload


def test_retire_never_overwrites_an_earlier_retired_file(
    tmp_path: Path,
) -> None:
    first = tmp_path / "state.sqlite3"
    first.write_text("first generation", encoding="utf-8")
    assert retire_legacy_state(first) == tmp_path / "state.sqlite3.retired"

    second = tmp_path / "state.sqlite3"
    second.write_text("second generation", encoding="utf-8")
    assert retire_legacy_state(second) == tmp_path / "state.sqlite3.retired.1"

    assert (tmp_path / "state.sqlite3.retired").read_text(
        encoding="utf-8"
    ) == "first generation"
    assert (tmp_path / "state.sqlite3.retired.1").read_text(
        encoding="utf-8"
    ) == "second generation"
    assert retire_legacy_state(tmp_path / "state.sqlite3") is None


def test_reply_routes_and_timezones_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "adapters.sqlite3"
    store = LarkStateStore(path, adapter="lark:main")
    route = ReplyRoute(
        message_id="om-route",
        actor_id="agent:node-a",
        actor_key="node-a",
        harness_message_id="hm-route",
        conversation_id="lark:oc_dm:oc_dm",
        chat_id="oc_dm",
    )
    store.record_reply(route)
    store.set_conversation_timezone("lark:oc_dm:oc_dm", "UTC")
    store.close()

    reopened = LarkStateStore(path, adapter="lark:main")
    assert reopened.reply("om-route") == route
    assert reopened.conversation_timezone("lark:oc_dm:oc_dm") == "UTC"
    reopened.set_conversation_timezone("lark:oc_dm:oc_dm", None)
    assert reopened.conversation_timezone("lark:oc_dm:oc_dm") is None
