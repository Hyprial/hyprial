"""The conversation recorder over the real custody facade and an isolated LocalOrgFs.

Rows come from ``app._inbox.interaction_rows_after``; each INTERACTION row is
appended to its conversation's record in the owner-home space, read back,
and only then granted for body release.  No cell document is written: the
recorder never composes the cell runtime (records are written without one).
SYSTEM rows are recorded with their class (plan slice S1), a row between
ends with no thread is filed under its pair key, a row with an unaddressed
end keeps its body, an authority that cannot be resolved records and grants
nothing, and an OrgFS outage only delays the records.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.cells import params as cells_params
from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.conversations import (
    RECORDS_DATABASE,
    RecordDraft,
    conversation_cell_id,
    conversation_pair_key,
    retry_delay_s,
)
from hyprial.daemon.impl.conversations.record import recorder as recorder_module
from hyprial.daemon.impl.delivery import DEFAULT_BODY_RETENTION_MS
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.conversations.keys import direct_key
from hyprial.kernel import DISPATCH_MESSAGE_PREFIX, canonical_agent_uri
from tests.daemon.conversations.conversation_support import (
    OWNER_PRINCIPAL,
    FlakyOrgFs,
    Log,
    cell_runtime_for,
    home_space,
    inbox_bodies,
    record_lines,
)
from tests.daemon.delivery.message.test_message_pending_wait import (
    READER,
    SESSION,
    _message,
    app as app,
    owned_applications as owned_applications,
)

DRIVER = "agent:op:node-1:driver"
THREAD = direct_key(DRIVER, READER).id
CELL = conversation_cell_id(OWNER_PRINCIPAL, THREAD, "conversation-1")
COORDINATOR = canonical_agent_uri("op", "node-1", "dispatch-coordinator")
HOUR_MS = 3_600_000


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000_000

    def __call__(self) -> int:
        return self.now


def _recorder(app: Any, tmp_path: Any, *, fs: Any = None, runtime: bool = True, **options: Any):
    local = fs if fs is not None else LocalOrgFs(author=OWNER_PRINCIPAL)
    if runtime:
        app._cell_runtime = cell_runtime_for(tmp_path, local)
    log = Log()
    recorder = compose_conversation_recorder(
        app, fs=local, start=False, clock_ms=Clock(), logger=log, **options
    )
    assert recorder is not None
    return recorder, local, log


def _database(app: Any) -> Any:
    return app._inbox._database


def _cursor(app: Any) -> dict[str, int]:
    with closing(sqlite3.connect(app.state_dir / RECORDS_DATABASE)) as db:
        return dict(db.execute("SELECT source, position FROM feed_cursor").fetchall())


def test_interaction_rows_are_recorded_read_back_and_then_granted(app, tmp_path) -> None:
    for message_id in ("m-1", "m-2"):
        app._inbox.receive(_message(message_id))
    recorder, fs, _ = _recorder(app, tmp_path)

    result = recorder.run_once()

    assert (result.recorded, result.granted, result.failed) == (2, 2, False)
    home = home_space(fs)
    assert home is not None
    lines = record_lines(fs, home, CELL)
    assert [line["messageId"] for line in lines] == ["m-1", "m-2"]
    assert {line["threadId"] for line in lines} == {THREAD}
    assert {line["cellId"] for line in lines} == {CELL}
    bodies = inbox_bodies(_database(app))
    assert all(bodies[m]["granted"] and not bodies[m]["dropped"] for m in ("m-1", "m-2"))
    written = fs.listdir(home, "cells/conversation")
    assert [node.name for node in written] == [CELL]
    assert not any(node.name.endswith(".json") for node in written)


def test_a_rerun_appends_nothing_and_the_cursor_continues(app, tmp_path) -> None:
    app._inbox.receive(_message("m-1"))
    recorder, fs, _ = _recorder(app, tmp_path)
    recorder.run_once()
    first = _cursor(app)

    assert recorder.run_once().recorded == 0
    app._inbox.receive(_message("m-2"))
    assert recorder.run_once().recorded == 1

    assert _cursor(app)["inbox"] > first["inbox"]
    assert [line["messageId"] for line in record_lines(fs, home_space(fs), CELL)] == [
        "m-1",
        "m-2",
    ]


def test_a_lost_local_index_does_not_append_a_record_twice(app, tmp_path) -> None:
    app._inbox.receive(_message("m-1"))
    recorder, fs, _ = _recorder(app, tmp_path)
    recorder.run_once()
    recorder.close(5.0)
    app._conversation_recorder = None
    (app.state_dir / RECORDS_DATABASE).unlink()

    again = compose_conversation_recorder(app, fs=fs, start=False, clock_ms=Clock())
    assert again is not None
    result = again.run_once()

    assert result.recorded == 1 and result.granted == 1
    assert len(record_lines(fs, home_space(fs), CELL)) == 1


def test_a_threaded_cell_id_is_unchanged_by_the_pair_rule() -> None:
    """Plan slice S1: literal ids computed on dev before the pair key existed."""

    assert THREAD == "t.pcemxskk5bbr2ehimzv5ibfgyi"
    assert CELL == "c.oaaogrvkivlie6ggdg34u6gbsu"


def test_system_rows_are_recorded_with_their_class_and_granted(app, tmp_path) -> None:
    dispatch = f"{DISPATCH_MESSAGE_PREFIX}{uuid.uuid4()}"
    app._inbox.receive(_message(dispatch))
    app._inbox.receive(
        replace(_message("m-reply"), intent="reply",
                idempotency_key=f"reply:{DISPATCH_MESSAGE_PREFIX}{uuid.uuid4()}")
    )
    recorder, fs, _ = _recorder(app, tmp_path)

    result = recorder.run_once()

    assert (result.recorded, result.granted) == (2, 2)
    lines = record_lines(fs, home_space(fs), CELL)
    assert [(line["messageId"], line["messageClass"]) for line in lines] == [
        (dispatch, "system"), ("m-reply", "system"),
    ]
    bodies = inbox_bodies(_database(app))
    assert bodies[dispatch]["granted"] and bodies["m-reply"]["granted"]
    with closing(sqlite3.connect(_database(app))) as db:
        classes = dict(db.execute("SELECT message_id, body_class FROM inbox").fetchall())
    assert classes == {dispatch: "system", "m-reply": "system"}


def test_a_reserved_actor_row_is_filed_under_its_pair_key(app, tmp_path) -> None:
    app._inbox.receive(replace(_message("m-1"), sender=COORDINATOR))
    recorder, fs, _ = _recorder(app, tmp_path)

    result = recorder.run_once()

    pair = conversation_pair_key(COORDINATOR, READER)
    assert pair is not None and pair.startswith("p.")
    assert pair == conversation_pair_key(READER, COORDINATOR)
    cell = conversation_cell_id(OWNER_PRINCIPAL, pair, "conversation-1")
    assert (result.recorded, result.granted) == (1, 1)
    [line] = record_lines(fs, home_space(fs), cell)
    assert (line["threadId"], line["cellId"], line["messageClass"]) == (
        pair, cell, "interaction",
    )


def test_an_unthreaded_row_is_skipped_and_keeps_its_body(app, tmp_path) -> None:
    app._inbox.receive(replace(_message("m-1"), sender="system:hyprial:node-1"))
    recorder, fs, log = _recorder(app, tmp_path)

    result = recorder.run_once()

    assert (result.recorded, result.skipped, result.granted) == (0, 1, 0)
    assert not inbox_bodies(_database(app))["m-1"]["granted"]
    assert "conversation.row_skipped" in log.names()
    assert recorder.run_once().skipped == 0


def test_the_redaction_hook_runs_before_the_write(app, tmp_path) -> None:
    for message_id in ("m-1", "m-2", "m-3"):
        app._inbox.receive(_message(message_id))
    seen: list[str] = []

    def redact(draft: RecordDraft) -> RecordDraft | None:
        seen.append(draft.message.message_id)
        if draft.message.message_id == "m-1":
            return None
        if draft.message.message_id == "m-2":
            return replace(draft, body=b'{"message":"[redacted]"}')
        return draft

    recorder, fs, _ = _recorder(app, tmp_path, redact=redact)
    result = recorder.run_once()

    assert seen == ["m-1", "m-2", "m-3"]
    assert (result.recorded, result.omitted, result.granted) == (3, 1, 1)
    lines = {line["messageId"]: line for line in record_lines(fs, home_space(fs), CELL)}
    assert set(lines) == {"m-1", "m-2", "m-3"}
    assert lines["m-1"]["bodyOmitted"] == "redacted" and "body" not in lines["m-1"]
    assert lines["m-2"]["redacted"] is True and lines["m-3"]["redacted"] is False
    bodies = inbox_bodies(_database(app))
    assert [m for m in ("m-1", "m-2", "m-3") if bodies[m]["granted"]] == ["m-3"]
    assert recorder.run_once().recorded == 0


def test_a_raising_redaction_hook_delays_the_record_and_keeps_the_body(app, tmp_path) -> None:
    app._inbox.receive(_message("m-1"))
    broken = [True]

    def flaky(draft: RecordDraft) -> RecordDraft | None:
        if broken[0]:
            raise RuntimeError("redaction failed")
        return draft

    recorder, fs, _ = _recorder(app, tmp_path, redact=flaky)
    result = recorder.run_once()

    assert result.failed and (result.recorded, result.granted, result.skipped) == (0, 0, 0)
    assert home_space(fs) is None
    assert not inbox_bodies(_database(app))["m-1"]["granted"]

    broken[0] = False
    again = recorder.run_once()
    assert (again.recorded, again.granted, again.failed) == (1, 1, False)


def test_an_unavailable_thread_authority_delays_the_record(app, tmp_path, monkeypatch) -> None:
    app._inbox.receive(_message("m-1"))
    recorder, fs, _ = _recorder(app, tmp_path)

    def unavailable(*_args: Any, **_kwargs: Any) -> str | None:
        raise RuntimeError("thread authority unavailable")

    import hyprial.daemon.impl.composition.conversations.wiring as wiring
    import hyprial.daemon.impl.conversations as conversations

    with monkeypatch.context() as patch:
        patch.setattr(wiring, "direct_key", unavailable)
        patch.setattr(conversations, "direct_key", unavailable)
        result = recorder.run_once()

    assert result.failed and (result.recorded, result.skipped) == (0, 0)
    assert not inbox_bodies(_database(app))["m-1"]["granted"]
    again = recorder.run_once()
    assert (again.recorded, again.granted, again.failed) == (1, 1, False)
    assert [line["messageId"] for line in record_lines(fs, home_space(fs), CELL)] == ["m-1"]


def test_records_are_written_without_any_cell_runtime(app, tmp_path, monkeypatch) -> None:
    composed: list[Any] = []
    monkeypatch.setattr(cells_params, "cell_runtime", composed.append)
    app._inbox.receive(_message("m-1"))
    recorder, fs, _ = _recorder(app, tmp_path, runtime=False)

    result = recorder.run_once()

    assert (result.recorded, result.granted, result.failed) == (1, 1, False)
    home = home_space(fs)
    assert home is not None
    assert [line["messageId"] for line in record_lines(fs, home, CELL)] == ["m-1"]
    assert inbox_bodies(_database(app))["m-1"]["granted"]
    assert getattr(app, "_cell_runtime", None) is None
    assert composed == [app]


def test_an_authority_that_cannot_be_resolved_records_and_grants_nothing(
    app, tmp_path
) -> None:
    class FlakyAuthorOrgFs:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.broken = False

        @property
        def author(self) -> str:
            if self.broken:
                raise RuntimeError("author unavailable")
            return self.inner.author

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    flaky = FlakyAuthorOrgFs(LocalOrgFs(author=OWNER_PRINCIPAL))
    app._inbox.receive(_message("m-1"))
    recorder, _, _ = _recorder(app, tmp_path, fs=flaky)

    flaky.broken = True
    failed = recorder.run_once()

    assert failed.unavailable and (failed.recorded, failed.granted) == (0, 0)
    assert home_space(flaky.inner) is None
    assert not inbox_bodies(_database(app))["m-1"]["granted"]

    flaky.broken = False
    again = recorder.run_once()
    assert (again.recorded, again.granted, again.failed) == (1, 1, False)
    assert [line["messageId"] for line in record_lines(
        flaky.inner, home_space(flaky.inner), CELL
    )] == ["m-1"]


def test_orgfs_down_delays_records_and_delivery_does_not_wait(app, tmp_path) -> None:
    """Both rows are queued at the reader's id address, so their records land
    in the conversation cell of that address's thread."""

    flaky = FlakyOrgFs(LocalOrgFs(author=OWNER_PRINCIPAL))
    recorder, _, log = _recorder(app, tmp_path, fs=flaky)
    flaky.down = True
    reader = app.agents.get("reader").uri
    app._inbox.receive(_message("m-1", recipient=reader))

    failed = recorder.run_once()
    assert failed.failed and (failed.recorded, failed.granted) == (0, 0)

    read = app.handle("message.read_body", {**SESSION, "messageId": "m-1"})
    assert read["ok"] is True and read["messageId"] == "m-1"
    assert app._inbox.ack(reader, "m-1").acknowledged
    app._inbox.receive(_message("m-2", recipient=reader))
    assert recorder.run_once().failed
    assert log.names().count("conversation.record_unavailable") == 1
    assert not any(row["granted"] for row in inbox_bodies(_database(app)).values())

    flaky.down = False
    caught_up = recorder.run_once()

    assert (caught_up.recorded, caught_up.granted, caught_up.failed) == (2, 2, False)
    assert "conversation.record_resumed" in log.names()
    cell = conversation_cell_id(
        OWNER_PRINCIPAL, direct_key(DRIVER, reader).id, "conversation-1"
    )
    lines = record_lines(flaky.inner, home_space(flaky.inner), cell)
    assert [line["messageId"] for line in lines] == ["m-1", "m-2"]


def test_a_failed_grant_is_retried_on_a_later_pass(app, tmp_path, monkeypatch) -> None:
    app._inbox.receive(_message("m-1"))
    recorder, _, _ = _recorder(app, tmp_path)
    real = app._inbox.grant_body_release
    calls: list[int] = []

    def flaky_grant(grant: Any, **kwargs: Any) -> bool:
        calls.append(grant.position)
        if len(calls) == 1:
            raise TimeoutError("actor busy")
        return real(grant, **kwargs)

    monkeypatch.setattr(app._inbox, "grant_body_release", flaky_grant)

    first = recorder.run_once()
    assert (first.recorded, first.granted) == (1, 0)
    second = recorder.run_once()
    assert (second.recorded, second.granted) == (0, 1)
    assert inbox_bodies(_database(app))["m-1"]["granted"]


def test_a_failing_grant_does_not_starve_newer_grants(app, tmp_path, monkeypatch) -> None:
    for message_id in ("m-1", "m-2"):
        app._inbox.receive(_message(message_id))
    recorder, _, _ = _recorder(app, tmp_path)
    monkeypatch.setattr(recorder_module, "GRANT_RETRY_BATCH", 1)
    real = app._inbox.grant_body_release
    first_m2 = [True]

    def grant(body: Any, **kwargs: Any) -> bool:
        row = app._inbox.interaction_row_at(body.source, body.position)
        if row is not None and row.message.message_id == "m-1":
            raise TimeoutError("always busy")
        if first_m2[0]:
            first_m2[0] = False
            raise TimeoutError("busy once")
        return real(body, **kwargs)

    monkeypatch.setattr(app._inbox, "grant_body_release", grant)

    assert recorder.run_once().granted == 0
    assert recorder.run_once().granted == 0
    assert recorder.run_once().granted == 1
    bodies = inbox_bodies(_database(app))
    assert bodies["m-2"]["granted"] and not bodies["m-1"]["granted"]


def _retention(app: Any) -> list[int | None]:
    with closing(sqlite3.connect(_database(app))) as db:
        return [row[0] for row in db.execute("SELECT body_retention_ms FROM inbox")]


def test_the_grant_carries_the_per_agent_retention(app, tmp_path) -> None:
    app._inbox.receive(_message("m-1"))
    subjects: list[str] = []

    def retention_for(row: Any) -> int:
        subjects.append(row.message.recipient)
        return 2 * HOUR_MS

    recorder, _, _ = _recorder(app, tmp_path, retention_for=retention_for)
    recorder.run_once()

    assert subjects == [READER]
    assert _retention(app) == [2 * HOUR_MS]


def test_the_default_retention_resolves_the_cell_param_for_the_agent(app, tmp_path) -> None:
    app._inbox.receive(_message("m-1"))
    recorder, _, _ = _recorder(app, tmp_path)
    recorder.run_once()

    assert _retention(app) == [DEFAULT_BODY_RETENTION_MS]


def test_the_retry_delay_backs_off_and_is_capped() -> None:
    delays = [retry_delay_s(failures) for failures in range(1, 10)]
    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert delays == sorted(delays) and max(delays) == 60.0
    assert retry_delay_s(0) == 0.0


def test_a_record_body_hashes_to_the_granted_digest(app, tmp_path) -> None:
    app._inbox.receive(_message("m-1"))
    recorder, fs, _ = _recorder(app, tmp_path)
    recorder.run_once()

    line = record_lines(fs, home_space(fs), CELL)[0]
    assert line["payloadSha256"] == hashlib.sha256(_message("m-1").payload).hexdigest()
    with closing(sqlite3.connect(_database(app))) as db:
        stored = db.execute("SELECT body_payload_sha256 FROM inbox").fetchone()[0]
    assert stored == line["payloadSha256"]


@pytest.mark.parametrize("missing", ["interaction_rows_after", "grant_body_release"])
def test_no_recorder_without_the_delivery_facade(missing, tmp_path) -> None:
    inbox = SimpleNamespace(
        interaction_rows_after=lambda *a, **k: None,
        interaction_row_at=lambda *a, **k: None,
        grant_body_release=lambda *a, **k: False,
        bind_body_retention=lambda **k: None,
        version=0,
    )
    delattr(inbox, missing)
    log = Log()
    bare = SimpleNamespace(_inbox=inbox, state_dir=tmp_path, node_id="node-1")
    assert compose_conversation_recorder(bare, start=False, logger=log) is None
    assert "conversation.recorder_unavailable" in log.names()
