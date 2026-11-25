"""The group log in the owner's chat space (slice GC1)."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.composition.cells.spaces import OWNER_HOME_KIND, OWNER_HOME_NAME
from hyprial.daemon.impl.composition.conversations.placement import (
    OwnerHomeGroupPlacement,
    OwnerHomePlacement,
)
from hyprial.daemon.impl.conversations import ConversationDocuments, RecordUnavailable
from hyprial.daemon.impl.conversations.groups import GroupLog, fold_group
from hyprial.daemon.impl.orgfs import LocalOrgFs
from tests.daemon.conversations.conversation_support import FlakyOrgFs, home_space
from tests.daemon.conversations.groups.group_support import (
    GROUP,
    OWNER,
    REVIEWER,
    WORKER,
    added,
    created,
    message,
)


def _placement(fs) -> OwnerHomeGroupPlacement:
    return OwnerHomeGroupPlacement(OwnerHomePlacement(SimpleNamespace(), fs=fs), OWNER)


def _log(fs, writer: str = "node-a") -> GroupLog:
    return GroupLog(placement=_placement(fs), writer=writer)


def _segments(fs, space: str) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for node in fs.listdir(space, "conversations", recursive=True):
        if node.kind == "doc":
            content, _ = fs.read_text(space, node.path)
            found[str(node.path)] = [json.loads(line) for line in content.splitlines() if line]
    return found


class _FixedPlacement:
    """Writes to one chosen home; reads the union of the tagged homes."""

    def __init__(self, fs, homes: list[str]) -> None:
        self._fs = fs
        self.homes = homes
        self.target = homes[0]

    def fs(self):
        return self._fs

    def read_fs(self):
        return self._fs

    def read_spaces(self, fs):
        return tuple(self.homes)

    def write_space(self):
        return self.target


def _homes(fs, count: int) -> list[str]:
    homes = []
    for _ in range(count):
        info = fs.create_space(OWNER_HOME_NAME)
        fs.update_space_meta(info.space_id, {"kind": OWNER_HOME_KIND})
        homes.append(info.space_id)
    return homes


def test_append_and_read_round_trip() -> None:
    fs = LocalOrgFs(author=OWNER)
    log = _log(fs)
    entries = [created(1), added(WORKER, at=2), message("m1", OWNER, at=3)]
    stored = log.append(GROUP, entries)
    assert set(stored) == {"e-created", entries[1].entry_id, "m1"}
    assert log.read(GROUP) == tuple(entries)
    space = home_space(fs)
    assert list(_segments(fs, space)) == [f"conversations/{GROUP}/log-node-a/000000.jsonl"]
    assert [line["entryId"] for line in _segments(fs, space)[
        f"conversations/{GROUP}/log-node-a/000000.jsonl"]] == ["e-created",
                                                              entries[1].entry_id, "m1"]


def test_appending_the_same_entry_id_twice_is_a_no_op() -> None:
    fs = LocalOrgFs(author=OWNER)
    log = _log(fs)
    log.append(GROUP, [created(1)])
    space = home_space(fs)
    before = _segments(fs, space)
    stored = log.append(GROUP, [created(1)])
    assert stored == {"e-created": created(1)}
    assert _segments(fs, space) == before
    log.append(GROUP, [created(1), message("m1", OWNER, at=2), message("m1", OWNER, at=2)])
    assert [entry.entry_id for entry in log.read(GROUP)] == ["e-created", "m1"]


def test_an_entry_in_a_rolled_over_segment_is_not_appended_again() -> None:
    fs = LocalOrgFs(author=OWNER)
    log = _log(fs)
    big = "x" * (150 * 1024)
    first = message("m1", OWNER, at=1, text=big)
    second = message("m2", OWNER, at=2, text=big)
    log.append(GROUP, [first, second])
    space = home_space(fs)
    assert len(_segments(fs, space)) == 2
    before = _segments(fs, space)
    log.append(GROUP, [first, second])
    assert _segments(fs, space) == before
    log.append(GROUP, [message("m3", OWNER, at=3)])
    after = _segments(fs, space)
    assert [line["entryId"] for line in after[
        f"conversations/{GROUP}/log-node-a/000001.jsonl"]] == ["m2", "m3"]
    assert [entry.entry_id for entry in log.read(GROUP)] == ["m1", "m2", "m3"]


def test_two_writers_share_one_document() -> None:
    fs = LocalOrgFs(author=OWNER)
    first, second = _log(fs, "node-a"), _log(fs, "node-b")
    first.append(GROUP, [created(1), added(WORKER, at=2)])
    second.append(GROUP, [added(REVIEWER, at=3, writer="node-b"),
                          message("m1", OWNER, at=4, writer="node-b")])
    second.append(GROUP, [added(WORKER, at=2)])
    segments = _segments(fs, home_space(fs))
    assert sorted(segments) == [f"conversations/{GROUP}/log-node-a/000000.jsonl",
                                f"conversations/{GROUP}/log-node-b/000000.jsonl"]
    assert [line["entryId"] for line in segments[
        f"conversations/{GROUP}/log-node-b/000000.jsonl"]] == [
        added(REVIEWER, at=3).entry_id, "m1"]
    assert first.read(GROUP) == second.read(GROUP)
    state = fold_group(first.read(GROUP))
    assert [m.member_id for m in state.members] == [OWNER, WORKER, REVIEWER]


def test_two_home_spaces_read_as_one_union() -> None:
    fs = LocalOrgFs(author=OWNER)
    homes = _homes(fs, 2)
    placement = _FixedPlacement(fs, homes)
    log = GroupLog(placement=placement, writer="node-a")
    log.append(GROUP, [created(1)])
    placement.target = homes[1]
    log.append(GROUP, [added(WORKER, at=2), created(1)])
    assert [entry.entry_id for entry in log.read(GROUP)] == ["e-created",
                                                            added(WORKER, at=2).entry_id]
    assert [[line["entryId"] for line in lines]
            for lines in _segments(fs, homes[1]).values()] == [[added(WORKER, at=2).entry_id]]


def test_an_entry_that_would_not_read_back_is_refused_before_any_write() -> None:
    fs = LocalOrgFs(author=OWNER)
    log = _log(fs)
    broken = dataclasses.replace(message("m1", OWNER, at=1), intent=None)
    with pytest.raises(ValueError):
        log.append(GROUP, [broken])
    assert list(fs.spaces()) == []


def test_the_owner_home_placement_reads_every_home() -> None:
    fs = LocalOrgFs(author=OWNER)
    homes = _homes(fs, 2)
    seeded = GroupLog(placement=_FixedPlacement(fs, [max(homes)]), writer="node-b")
    seeded.append(GROUP, [created(1)])
    log = _log(fs)
    log.append(GROUP, [added(WORKER, at=2)])
    assert _segments(fs, min(homes))
    assert [entry.entry_id for entry in log.read(GROUP)] == ["e-created",
                                                            added(WORKER, at=2).entry_id]


def test_a_read_never_creates_a_space() -> None:
    fs = LocalOrgFs(author=OWNER)
    log = _log(fs)
    assert log.read(GROUP) == ()
    assert list(fs.spaces()) == []
    assert home_space(fs) is None


def test_a_read_of_an_unknown_group_is_empty() -> None:
    fs = LocalOrgFs(author=OWNER)
    log = _log(fs)
    log.append(GROUP, [created(1)])
    assert log.read("g." + "b" * 26) == ()


def test_entries_naming_another_group_are_ignored() -> None:
    fs = LocalOrgFs(author=OWNER)
    log = _log(fs)
    log.append(GROUP, [created(1)])
    other = "g." + "c" * 26
    space = home_space(fs)
    path = f"conversations/{other}/log-node-a/000000.jsonl"
    fs.mkdir(space, f"conversations/{other}")
    fs.mkdir(space, f"conversations/{other}/log-node-a")
    fs.write_text(space, path, created(1).to_line(), create_only=True)
    assert log.read(other) == ()


def test_an_unavailable_orgfs_is_record_unavailable() -> None:
    flaky = FlakyOrgFs(LocalOrgFs(author=OWNER))
    log = _log(flaky)
    log.append(GROUP, [created(1)])
    flaky.down = True
    with pytest.raises(RecordUnavailable):
        log.append(GROUP, [message("m1", OWNER, at=2)])
    with pytest.raises(RecordUnavailable):
        log.read(GROUP)


def test_a_group_id_is_required() -> None:
    log = _log(LocalOrgFs(author=OWNER))
    with pytest.raises(ValueError):
        log.append("c." + "a" * 26, [created(1)])
    with pytest.raises(ValueError):
        log.read("../cells")


def test_old_conversation_readers_ignore_the_group_directory() -> None:
    fs = LocalOrgFs(author=OWNER)
    _log(fs).append(GROUP, [created(1), added(WORKER, at=2), message("m1", OWNER, at=3)])
    placement = OwnerHomePlacement(SimpleNamespace(), fs=fs)
    documents = ConversationDocuments(
        fs=placement.read_fs,
        spaces=lambda reader: placement.read_spaces(reader, OWNER),
    )
    assert documents.known_cells() == {}
    assert documents.cell_entries(GROUP) == ()


def test_groups_names_the_group_directories_of_every_home() -> None:
    """Slice GC2: the list enumeration; no space is created and stray names are skipped."""

    fs = FlakyOrgFs(LocalOrgFs(author=OWNER))
    homes = _homes(fs.inner, 2)
    placement = _FixedPlacement(fs, homes)
    log = GroupLog(placement=placement, writer="node-a")
    assert log.groups() == ()
    other = "g." + "d" * 26
    log.append(GROUP, [created(1)])
    placement.target = homes[1]
    log.append(other, [dataclasses.replace(created(1), conversation_id=other)])
    fs.inner.mkdir(homes[1], "conversations/not-a-group")
    assert log.groups() == tuple(sorted((GROUP, other)))
    assert _log(LocalOrgFs(author=OWNER)).groups() == ()
    homes_log = _log(fs)
    assert homes_log.groups() == tuple(sorted((GROUP, other)))
    fs.down = True
    with pytest.raises(RecordUnavailable):
        homes_log.groups()


class _InterleavingOrgFs:
    """Holds the first segment write until another thread has read the segment tail."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.first_writer: int | None = None
        self.other_read = threading.Event()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def read_text_snapshot(self, space, path):
        if self.first_writer not in (None, threading.get_ident()):
            self.other_read.set()
        return self.inner.read_text_snapshot(space, path)

    def write_text(self, space, path, content, **options):
        if self.first_writer is None:
            self.first_writer = threading.get_ident()
            self.other_read.wait(timeout=0.5)
        return self.inner.write_text(space, path, content, **options)


def test_concurrent_appends_of_one_daemon_both_land() -> None:
    """E2E-031 GC-11: two members replying at once must not fail the second as stale."""

    fs = LocalOrgFs(author=OWNER)
    _log(fs).append(GROUP, [created(1), added(WORKER, at=2), added(REVIEWER, at=3)])
    racing = _InterleavingOrgFs(fs)
    failures: list[BaseException] = []

    def reply(entry_id: str, author: str) -> None:
        try:
            _log(racing).append(GROUP, [message(entry_id, author, at=4)])
        except BaseException as error:  # noqa: BLE001 - collected for the assertion below
            failures.append(error)

    first = threading.Thread(target=reply, args=("m-worker", WORKER))
    first.start()
    while racing.first_writer is None and first.is_alive():
        time.sleep(0.005)
    second = threading.Thread(target=reply, args=("m-reviewer", REVIEWER))
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert failures == []
    assert {"m-worker", "m-reviewer"} <= {entry.entry_id for entry in _log(fs).read(GROUP)}
