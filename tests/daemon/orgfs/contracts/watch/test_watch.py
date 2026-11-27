# Decisions:
# - Migrated from contracts/test_orgfs_api.py (frozen ``watch`` signature)
#   and authority/test_orgfs_actor_boundary.py (watchers run outside the
#   facade lock, committed effects keep commit order).  The originals probe
#   ``facade._lock`` and ``_effect_sequence``; here the same properties are
#   observed through public calls only: a watcher that reads the facade must
#   not deadlock, and a later write is visible before an earlier watcher
#   finishes.  ``test_effect_lane_overload_rejects_mutation_before_commit``
#   needs a private capacity constant and is not migrated.
# - Design §2.5 step 3: watch events fire only after the commit is durable.
"""watch and change-event contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import inspect
import threading

import pytest

from hyprial.daemon.impl.orgfs import OrgFs, OrgFsError

from ..support import eventually


def test_watch_matches_frozen_signature_verbatim(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    for signature in (inspect.signature(OrgFs.watch), inspect.signature(type(fs).watch)):
        assert tuple(signature.parameters) == ("self", "space_id", "glob", "callback")
        assert all(
            parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        )
    space = fs.create_space("team")
    with pytest.raises(TypeError):
        fs.watch(space.space_id)  # type: ignore[call-arg]


def test_watch_reports_every_change_kind_in_order(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    seen = []
    fs.watch(space.space_id, "**", seen.append)
    fs.mkdir(space.space_id, "folder")

    created = fs.write_text(space.space_id, "a.md", "one")
    fs.write_text(space.space_id, "a.md", "two")
    fs.move(space.space_id, "a.md", "folder/b.md")
    fs.remove(space.space_id, "folder/b.md")
    orgfs_impl.restore_last(fs, space.space_id, f"id:{created.node_id}")

    def check() -> None:
        events = [
            (event.kind, event.node.path, event.old_path)
            for event in seen
            if event.node.node_id == created.node_id
        ]
        assert [kind for kind, _path, _old in events] == [
            "created",
            "modified",
            "moved",
            "removed",
            "restored",
        ]
        assert events[2][1:] == ("folder/b.md", "a.md")
        assert events[-1][1] == "folder/b.md"

    eventually(check)
    assert all(event.space_id == space.space_id for event in seen)


def test_watch_glob_filters_and_close_stops_delivery(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    markdown = []
    registration = fs.watch(space.space_id, "*.md", markdown.append)

    fs.write_text(space.space_id, "keep.md", "one")
    fs.write_text(space.space_id, "skip.txt", "two")
    eventually(lambda: _assert_paths(markdown, ["keep.md"]))
    registration.close()
    fs.write_text(space.space_id, "after.md", "three")
    everything = []
    fs.watch(space.space_id, "**", everything.append)
    fs.write_text(space.space_id, "last.md", "four")
    eventually(lambda: _assert_paths(everything, ["last.md"]))

    assert [event.node.path for event in markdown] == ["keep.md"]


def test_watchers_can_read_the_facade_without_deadlock(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    observed: list[str] = []
    delivered = threading.Event()

    def watcher(event) -> None:
        observed.append(fs.read_text(space.space_id, f"id:{event.node.node_id}")[0])
        delivered.set()

    fs.watch(space.space_id, "*.md", watcher)
    with ThreadPoolExecutor(max_workers=1) as pool:
        write = pool.submit(fs.write_text, space.space_id, "note.md", "visible")
        assert delivered.wait(5)
        write.result(timeout=5)

    assert observed == ["visible"]


def test_watch_events_keep_commit_order_across_writers(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first_started = threading.Event()
    release_first = threading.Event()
    observed: list[str] = []
    second_delivered = threading.Event()

    def watcher(event) -> None:
        if event.node.path == "first":
            first_started.set()
            assert release_first.wait(5)
        observed.append(event.node.path)
        if event.node.path == "second":
            second_delivered.set()

    fs.watch(space.space_id, "**", watcher)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fs.mkdir, space.space_id, "first")
        assert first_started.wait(5)
        second = pool.submit(fs.mkdir, space.space_id, "second")
        eventually(lambda: _assert_resolves(fs, space.space_id, "second"))
        assert observed == []
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert second_delivered.wait(5)
    assert observed == ["first", "second"]


def test_failed_write_emits_no_event(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "file.txt", "one")
    seen = []
    fs.watch(space.space_id, "**", seen.append)

    with pytest.raises(OrgFsError):
        fs.write_text(space.space_id, "file.txt", "two", expect_version="stale")
    fs.write_text(space.space_id, "marker.txt", "marker")

    eventually(lambda: _assert_paths(seen, ["marker.txt"]))


def _assert_paths(events, expected: list[str]) -> None:
    assert [event.node.path for event in events] == expected


def _assert_resolves(fs, space_id: str, path: str) -> None:
    assert fs.resolve(space_id, path)
