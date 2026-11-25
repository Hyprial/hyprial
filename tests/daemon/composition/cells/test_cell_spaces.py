"""CellSpaces: cheap owner-home discovery and crash-safe home creation.

HS1: two threads of one daemon (the cell runtime flush and the conversation
recorder) create at most one owner home over one OrgFS facade; homes from
two devices still read as a union and write to the lowest space id.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from hyprial.daemon.impl.cells.model import CellScope
from hyprial.daemon.impl.composition.cells.spaces import (
    OWNER_HOME_KIND,
    OWNER_HOME_NAME,
    CellSpaces,
)
from hyprial.daemon.impl.composition.conversations import OwnerHomePlacement
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.identity import org_space_name


class _Counting:
    def __init__(self, inner: LocalOrgFs) -> None:
        self._inner = inner
        self.meta_reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def space_meta(self, space_id: str) -> Any:
        self.meta_reads += 1
        return self._inner.space_meta(space_id)


def test_org_spaces_are_never_probed_for_a_home_tag() -> None:
    inner = LocalOrgFs(author="user:alice")
    for org in ("group-a", "group-b", "group-c"):
        inner.create_space(org_space_name(org))
    fs = _Counting(inner)
    spaces = CellSpaces(fs)
    assert len(spaces.read_spaces()) == 3
    spaces.read_spaces()
    assert fs.meta_reads == 0


def test_an_untagged_home_left_by_a_crash_is_adopted_not_duplicated() -> None:
    fs = LocalOrgFs(author="user:alice")
    orphan = fs.create_space(OWNER_HOME_NAME)
    spaces = CellSpaces(fs)
    home = spaces.private_space("user:alice")
    assert home is not None and home.space_id == orphan.space_id
    assert fs.space_meta(orphan.space_id)["kind"] == OWNER_HOME_KIND
    assert [info.space_id for info in fs.spaces()] == [orphan.space_id]


class _Racing:
    """Holds both threads after their first ``spaces()`` and before ``create_space``.

    Every thread's first listing finds no home, so both are past the
    "no home" check before either creates.  ``create_space`` then waits
    (bounded) for the other thread to reach it too; with the per-facade lock
    the other thread is parked on the lock instead and the wait times out.
    """

    def __init__(self, inner: LocalOrgFs) -> None:
        self._inner = inner
        self._listed = threading.Barrier(2, timeout=5.0)
        self._creating = threading.Barrier(2, timeout=0.5)
        self._seen: set[int] = set()
        self._seen_lock = threading.Lock()
        self.creates = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def spaces(self) -> Any:
        found = self._inner.spaces()
        me = threading.get_ident()
        with self._seen_lock:
            first = me not in self._seen
            self._seen.add(me)
        if first:
            self._listed.wait()
        return found

    def create_space(self, name: str, **kwargs: Any) -> Any:
        try:
            self._creating.wait()
        except threading.BrokenBarrierError:
            pass
        self.creates += 1
        return self._inner.create_space(name, **kwargs)


def _run_both(first: Callable[[], str | None], second: Callable[[], str | None]) -> list[Any]:
    results: list[Any] = [None, None]

    def run(index: int, writer: Callable[[], str | None]) -> None:
        results[index] = writer()

    threads = [
        threading.Thread(target=run, args=(0, first)),
        threading.Thread(target=run, args=(1, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert not any(thread.is_alive() for thread in threads)
    return results


def _homes(fs: LocalOrgFs) -> list[str]:
    return [info.space_id for info in fs.spaces() if info.name == OWNER_HOME_NAME]


@pytest.mark.parametrize("swap", [False, True])
def test_two_threads_racing_on_one_facade_create_one_home(swap: bool) -> None:
    inner = LocalOrgFs(author="user:alice")
    fs = _Racing(inner)
    runtime_spaces = CellSpaces(fs)
    placement = OwnerHomePlacement(None, fs=fs)

    def runtime_write() -> str | None:
        (ref,) = runtime_spaces.write_spaces(CellScope("owner"), "user:alice")
        return ref.space_id

    def recorder_write() -> str | None:
        return placement.write_space("user:alice")

    writers = (recorder_write, runtime_write) if swap else (runtime_write, recorder_write)
    first, second = _run_both(*writers)
    (home,) = _homes(inner)
    assert fs.creates == 1
    assert first == second == home
    assert inner.space_meta(home)["kind"] == OWNER_HOME_KIND


def test_a_second_thread_in_the_create_to_tag_window_takes_the_same_home() -> None:
    inner = LocalOrgFs(author="user:alice")
    tagging = threading.Event()
    listed_in_window = threading.Event()
    release = threading.Event()
    tags: list[str] = []

    class _SlowTag:
        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

        def spaces(self) -> Any:
            found = inner.spaces()
            if tagging.is_set():
                listed_in_window.set()
            return found

        def update_space_meta(self, space_id: str, values: Any) -> Any:
            tags.append(space_id)
            tagging.set()
            release.wait(timeout=5.0)
            return inner.update_space_meta(space_id, values)

    fs = _SlowTag()
    results: list[Any] = [None, None]

    def write(index: int) -> None:
        home = CellSpaces(fs).private_space("user:alice")
        results[index] = None if home is None else home.space_id

    first = threading.Thread(target=write, args=(0,))
    first.start()
    assert tagging.wait(timeout=5.0)
    second = threading.Thread(target=write, args=(1,))
    second.start()
    assert listed_in_window.wait(timeout=5.0)
    release.set()
    for thread in (first, second):
        thread.join(timeout=10.0)
        assert not thread.is_alive()
    (home,) = _homes(inner)
    assert results == [home, home]
    assert tags == [home]


def test_homes_of_two_devices_read_as_a_union_and_write_to_the_lowest_id() -> None:
    fs = LocalOrgFs(author="user:alice")
    created = [fs.create_space(OWNER_HOME_NAME) for _ in range(2)]
    for info in created:
        fs.update_space_meta(info.space_id, {"kind": OWNER_HOME_KIND})
    ids = sorted(info.space_id for info in created)
    spaces = CellSpaces(fs)
    assert [ref.space_id for ref in spaces.home_spaces("user:alice")] == ids
    assert [ref.space_id for ref in spaces.read_spaces()] == ids
    home = spaces.private_space("user:alice")
    assert home is not None and home.space_id == ids[0]
    assert OwnerHomePlacement(None, fs=fs).write_space("user:alice") == ids[0]
    assert sorted(_homes(fs)) == ids


def test_reading_never_creates_a_home() -> None:
    fs = LocalOrgFs(author="user:alice")
    spaces = CellSpaces(fs)
    assert spaces.read_spaces() == ()
    assert spaces.home_spaces("user:alice") == ()
    assert OwnerHomePlacement(None, fs=fs).read_spaces(fs, "user:alice") == ()
    assert fs.spaces() == ()


def test_a_uid_authority_cell_is_written_to_the_writer_owner_home(tmp_path: Any) -> None:
    import json

    from hyprial.daemon.impl.cells.model import CellBinding
    from hyprial.daemon.impl.cells.ports import CellSpec
    from tests.daemon.cells.runtime.cell_runtime_support import ALICE, runtime

    fs = LocalOrgFs(author="user:alice")
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(CellSpec(kind="agent", binding=CellBinding.subject("a." + "m" * 26),
                                 scope=CellScope.owner()))
    cells.run_once()
    assert cells.dirty() == frozenset()
    (home,) = CellSpaces(fs).home_spaces("user:alice")
    path = f"cells/agent/{cell.cell_id}.json"
    assert fs.document_authors(home.space_id, path) == ("user:alice",)
    assert json.loads(fs.read_text(home.space_id, path)[0])["authority"] == ALICE
    assert not any(info.owner.startswith("user:u.") for info in fs.spaces())
    cells.close()


def test_a_uid_authority_finds_no_owner_home_and_creates_none() -> None:
    from tests.daemon.cells.runtime.cell_runtime_support import ALICE

    fs = LocalOrgFs(author="user:alice")
    fs.create_space(org_space_name("group-acme"))
    before = [(info.space_id, info.owner, info.name) for info in fs.spaces()]
    spaces = CellSpaces(fs)
    assert spaces.private_space(ALICE) is None
    assert spaces.write_spaces(CellScope.owner(), ALICE) == ()
    assert [(info.space_id, info.owner, info.name) for info in fs.spaces()] == before


def test_the_org_scope_ignores_the_writer_argument() -> None:
    fs = LocalOrgFs(author="user:alice")
    fs.create_space(org_space_name("group-acme"))
    fs.create_space(org_space_name("group-beta"))
    spaces = CellSpaces(fs)
    reference = spaces.write_spaces(CellScope.of_org(), "user:alice")
    assert len(reference) == 2
    for writer in ("user:bob", "user:u.ou_" + "a" * 32, ""):
        assert spaces.write_spaces(CellScope.of_org(), writer) == reference
        assert spaces.write_spaces(CellScope.of_org("acme"), writer) == (
            spaces.write_spaces(CellScope.of_org("acme"), "user:alice")
        )


def test_a_username_change_keeps_the_writer_and_its_owner_home(tmp_path: Any) -> None:
    import json
    from types import SimpleNamespace

    from hyprial.daemon.impl.composition.cells.wiring import local_author

    home = tmp_path / "home"
    home.mkdir()
    settings = {"version": 1, "owner": "alice", "username": "alice", "userId": "ou_" + "a" * 32}
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    fs = LocalOrgFs(author="user:alice")
    app = SimpleNamespace(owner="alice")
    writer = local_author(app, fs)
    first = CellSpaces(fs).private_space(writer)
    settings["username"] = "alice-renamed"
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    assert local_author(SimpleNamespace(owner=settings["owner"]), fs) == writer == "user:alice"
    assert first is not None
    assert CellSpaces(fs).private_space(writer) == first
