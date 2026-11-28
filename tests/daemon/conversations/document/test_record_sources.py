"""RM1: where the record read gets OrgFS from, and what it never does.

A read uses the injected facade or the OrgFS runtime the daemon already
composed; it never composes OrgFS and never creates an owner-home space.
A started daemon composes the runtime at startup, which the daemon-level
test stands in for by setting ``app._orgfs_runtime``.  Also pinned: a
redacted record entry keeps the queue's original body while the queue holds
it, and a segment read as pending is read again later.

Two owner homes: within one process ``CellSpaces.private_space`` creates
at most one home per OrgFS facade, so the cell runtime's thread and the
recorder no longer create two.  A record can still be split across two
homes when two devices of one owner each create one, or when it was written
before that fix.  Tests here hold whatever homes exist and pin the two-home
read; the split case is built directly on a bare ``LocalOrgFs``.

Restart: a started daemon composes its OrgFS runtime at startup
(``_startup_compose_delivery_plane`` calls ``_require_orgfs_runtime()``)
and the conversation recorder and its placement right after
(``_startup_adopt_domain_services``).  The restart test runs exactly those
two calls on a real ``OrgFsRuntime`` in the daemon state directory, nothing
patched.  The golden harness cannot register its agents twice on one home,
so the restart is a fresh application on a fresh home of the same owner
and node that receives the first one's OrgFS state directory after the
first one closed.  Its queue is empty, so its first ``conversations.history``
read can only answer from the record on disk.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.cells.spaces import (
    OWNER_HOME_KIND,
    OWNER_HOME_NAME,
    CellSpaces,
)
from hyprial.daemon.impl.composition.conversations import (
    OwnerHomePlacement,
    compose_conversation_recorder,
)
from hyprial.daemon.impl.conversations import (
    ConversationDocuments,
    RecordEntry,
    RecordUnavailable,
    SegmentStore,
    visible_messages,
)
from hyprial.daemon.impl.delivery.ports import SOURCE_INBOX, SOURCE_SENDER_COPY
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.kernel import ORGFS_CONTENT_PENDING
from tests.daemon.conversations.conversation_support import OWNER_PRINCIPAL, record_lines
from tests.daemon.conversations.document.document_support import (
    RecordDaemon,
    cell_of,
    ids,
    record_daemon,
)
from tests.support.golden.app import (
    ALPHA,
    BETA,
    build_golden_daemon,
    close_golden_daemon,
    golden_daemon as golden_daemon,
)


def _never() -> Any:
    raise AssertionError("a read must not compose OrgFS")


def test_the_placement_reads_only_an_orgfs_that_is_already_up() -> None:
    app = SimpleNamespace(_orgfs_runtime=None, _require_orgfs_runtime=_never)
    placement = OwnerHomePlacement(app)
    with pytest.raises(RecordUnavailable):
        placement.read_fs()
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    app._orgfs_runtime = SimpleNamespace(facade=fs)
    assert placement.read_fs() is fs
    assert placement.read_spaces(fs, OWNER_PRINCIPAL) == ()
    assert fs.spaces() == ()
    placement.close()
    with pytest.raises(RecordUnavailable):
        placement.read_fs()


def test_a_daemon_reads_the_record_through_its_composed_orgfs(golden_daemon, monkeypatch) -> None:
    daemon = golden_daemon()
    app = daemon.app
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    runtime = SimpleNamespace(facade=fs, close=lambda: None)
    monkeypatch.setattr(app, "_require_orgfs_runtime", lambda: runtime, raising=False)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    booted = RecordDaemon(daemon, recorder, SimpleNamespace(inner=fs, down=False))
    sent = booted.send(ALPHA, BETA, "conv-up", "up")
    booted.settle()
    booted.forget(sent)
    cell = cell_of(ALPHA, BETA, "conv-up")
    assert booted.history(ALPHA, cell)["recordAvailable"] is False
    monkeypatch.setattr(app, "_orgfs_runtime", runtime, raising=False)
    page = booted.history(ALPHA, cell)
    assert page["recordAvailable"] is True
    assert ids(page) == [sent]


def _redact(draft: Any) -> Any:
    return draft.__class__(**{
        **{name: getattr(draft, name) for name in draft.__dataclass_fields__},
        "body": draft.body.replace(b"secret", b"[redacted]"),
    })


def _redacted_conversation(golden_daemon, monkeypatch) -> tuple[RecordDaemon, list[str], str]:
    booted = record_daemon(golden_daemon, monkeypatch, redact=_redact)
    sent = [
        booted.send(ALPHA, BETA, "conv-red", "before"),
        str(booted.app.handle(
            "message.send",
            {"from": ALPHA, "sessionRef": "session-alpha", "to": [BETA], "summary": "s",
             "message": "a secret", "conversationId": "conv-red", "idempotencyKey": "key-red"},
        )["messageId"]),
    ]
    booted.daemon.clock.advance(5)
    sent.append(booted.send(ALPHA, BETA, "conv-red", "after"))
    booted.settle()
    lines = record_lines(booted.fs.inner, booted.space(), cell_of(ALPHA, BETA, "conv-red"))
    assert {line["messageId"] for line in lines if line["redacted"]} == {sent[1]}
    return booted, sent, cell_of(ALPHA, BETA, "conv-red")


def test_a_redacted_entry_keeps_the_body_the_queue_still_holds(golden_daemon, monkeypatch) -> None:
    booted, sent, cell = _redacted_conversation(golden_daemon, monkeypatch)
    booted.forget(sent[0], sent[2])
    for mode in (None, "tail"):
        page = booted.history(ALPHA, cell, **({} if mode is None else {"mode": mode}))
        assert page["recordAvailable"] is True
        assert ids(page) == sent
        assert [entry["message"] for entry in page["entries"]] == [
            "body before", "a secret", "body after",
        ]


def test_a_redacted_entry_shows_once_the_queue_no_longer_holds_the_body(
    golden_daemon, monkeypatch
) -> None:
    booted, sent, cell = _redacted_conversation(golden_daemon, monkeypatch)
    booted.forget(*sent)
    page = booted.history(ALPHA, cell)
    assert ids(page) == sent
    assert [entry["message"] for entry in page["entries"]] == [
        "body before", "a [redacted]", "body after",
    ]
    assert "redacted" not in page["entries"][1]


class _Pending:
    """A facade whose segment reads in *spaces* answer ``content-pending``.

    ``spaces`` None holds every space; ``release`` lets reads through.
    """

    def __init__(self, fs: Any, spaces: Collection[str] | None = None) -> None:
        self.inner = fs
        self.held_in = spaces
        self.held = True
        self.refused: list[tuple[str, str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def release(self) -> None:
        self.held = False

    def read_text(self, space: str, path: str) -> Any:
        if self.held and (self.held_in is None or space in self.held_in):
            self.refused.append((space, path))
            raise OrgFsError(ORGFS_CONTENT_PENDING, {"message": "not arrived"})
        return self.inner.read_text(space, path)


def _versions(fs: Any, spaces: Sequence[str]) -> dict[tuple[str, str], str]:
    return {
        (space, str(node.path)): str(node.version)
        for space in spaces
        for node in fs.listdir(space, "cells/conversation", recursive=True)
        if node.kind == "doc"
    }


def test_a_pending_segment_is_read_again_on_the_next_refresh(golden_daemon, monkeypatch) -> None:
    # One process creates at most one owner home, but a record can still sit
    # in one home or be split across two (two devices of one owner, or data
    # written before that fix); every segment is held, whichever homes hold it.
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = booted.send(ALPHA, BETA, "conv-p", "p")
    booted.settle()
    pending = _Pending(booted.fs.inner)
    placement = booted.app._conversation_placement
    homes = placement.read_spaces(pending, OWNER_PRINCIPAL)
    now = [0.0]
    documents = ConversationDocuments(
        fs=lambda: pending,
        spaces=lambda fs: placement.read_spaces(fs, OWNER_PRINCIPAL),
        clock=lambda: now[0],
    )
    cell = cell_of(ALPHA, BETA, "conv-p")
    before = _versions(booted.fs.inner, homes)
    assert documents.known_cells().get(cell, ()) == ()
    assert pending.refused
    pending.release()
    now[0] = 60.0
    assert {entry.message_id for entry in documents.known_cells()[cell]} == {sent}
    assert _versions(booted.fs.inner, homes) == before


def _split_record(fs: LocalOrgFs) -> tuple[list[str], list[RecordEntry]]:
    """Two tagged owner homes with one cell's record split across them."""

    homes = []
    for _ in range(2):
        info = fs.create_space(OWNER_HOME_NAME)
        fs.update_space_meta(info.space_id, {"kind": OWNER_HOME_KIND})
        homes.append(info.space_id)
    entries = [
        RecordEntry(
            key=f"{source}:{at}:{message_id}", source=source, position=at,
            message_id=message_id, conversation_id="conv-split", thread_id="t.split",
            cell_id="c.split", sender=ALPHA, recipient=BETA, intent="request",
            created_at_ms=at, recorded_at_ms=at, payload_sha256="0" * 64,
            body=f"body {message_id}".encode(),
        )
        for message_id, at, source in (
            ("m1", 1, SOURCE_INBOX), ("m1", 1, SOURCE_SENDER_COPY),
            ("m2", 2, SOURCE_INBOX), ("m2", 2, SOURCE_SENDER_COPY),
        )
    ]
    target = [homes[1]]
    store = SegmentStore(fs=lambda: fs, space_for=lambda _owner: target[0], writer="node-1")
    store.append(OWNER_PRINCIPAL, "c.split", entries[:1], 0)
    target[0] = homes[0]
    store.append(OWNER_PRINCIPAL, "c.split", entries[1:], 0)
    return homes, entries


@pytest.mark.parametrize("held_home", [0, 1])
def test_a_record_split_across_two_homes_reads_as_one_document(held_home: int) -> None:
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    homes, entries = _split_record(fs)
    pending = _Pending(fs, {homes[held_home]})
    now = [0.0]
    documents = ConversationDocuments(
        fs=lambda: pending,
        spaces=lambda reader: tuple(
            ref.space_id for ref in CellSpaces(reader).home_spaces(OWNER_PRINCIPAL)
        ),
        clock=lambda: now[0],
    )
    readable = [entry for entry in entries if (entry.key == entries[0].key) == (held_home == 0)]
    listed = documents.known_cells().get("c.split", ())
    assert sorted(entry.key for entry in listed) == sorted(entry.key for entry in readable)
    assert {entry.key for entry in documents.cell_entries("c.split")} == {
        entry.key for entry in readable
    }
    pending.release()
    now[0] = 60.0
    listed = documents.known_cells()["c.split"]
    assert sorted(entry.key for entry in listed) == sorted(entry.key for entry in entries)
    full = documents.cell_entries("c.split")
    assert sorted(entry.key for entry in full) == sorted(entry.key for entry in entries)
    messages = visible_messages(full, {ALPHA})
    assert [message.message_id for message in messages] == ["m1", "m2"]
    assert [message.payload for message in messages] == [b"body m1", b"body m2"]


def _start(
    root: Path, monkeypatch: Any, before: Callable[[Path], None] = lambda _state: None
) -> RecordDaemon:
    daemon = build_golden_daemon(root, monkeypatch)
    app = daemon.app
    before(Path(app.state_dir))
    runtime = app._require_orgfs_runtime()
    assert isinstance(runtime, OrgFsRuntime)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    return RecordDaemon(daemon, recorder, runtime.facade)


def test_the_first_read_after_a_restart_reads_the_record_on_disk(tmp_path, monkeypatch) -> None:
    roots = [tmp_path / "before", tmp_path / "after"]
    for root in roots:
        root.mkdir()
    first = _start(roots[0], monkeypatch)
    old_state = Path(first.app.state_dir)
    try:
        sent = [first.send(ALPHA, BETA, "conv-restart", f"m{index}") for index in range(3)]
        first.settle()
    finally:
        close_golden_daemon(first.app)
    second = _start(
        roots[1], monkeypatch,
        lambda state: shutil.copytree(old_state / "orgfs", state / "orgfs", dirs_exist_ok=True),
    )
    try:
        assert ids(second.history(ALPHA, cell_of(ALPHA, BETA, "conv-restart"), mode="tail")) == sent
        second.forget(*sent)
        cell = cell_of(ALPHA, BETA, "conv-restart")
        page = second.history(ALPHA, cell)
        assert page["recordAvailable"] is True
        assert ids(page) == sent
        assert [entry["message"] for entry in page["entries"]] == ["body m0", "body m1", "body m2"]
        listed = second.listed(BETA)
        assert listed["recordAvailable"] is True
        assert cell in {head["conversationId"] for head in listed["conversations"]}
    finally:
        close_golden_daemon(second.app)
