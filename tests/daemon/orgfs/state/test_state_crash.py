"""Crash points and commit failures of ``FileStateStore`` (design 2.3, 2.4; C7 5.3 items 5, 6).

Every write, fsync, rename, unlink and truncate of an operation is an injection
point.  A simulated crash is a ``BaseException`` raised before the step (or in
the middle of a write), so no cleanup of the store runs: the process view is
exactly what an abrupt exit leaves behind.  A second, smaller group exits a real
child process with ``os._exit`` at a named step.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs.state import (
    Batch,
    FileOps,
    FileStateStore,
    StateStore,
    StateStoreBroken,
)

from .conftest import SPACE_ID, Writer, materialize

DOC_A = "doc-00000000-0000-4000-8000-00000000000a"
DOC_B = "doc-00000000-0000-4000-8000-00000000000b"
DOCS = ("meta", "tree", DOC_A, DOC_B)


class SimulatedCrash(BaseException):
    """Stands for the process dying at an injection point."""


class Steps(FileOps):
    """Counts steps while armed; crashes before step ``crash_at`` (or tears its write)."""

    def __init__(self, crash_at: int | None = None, *, torn: bool = False) -> None:
        self.crash_at = crash_at
        self.torn = torn
        self.armed = False
        self.seen: list[str] = []

    def _due(self) -> bool:
        return self.armed and self.crash_at is not None and len(self.seen) == self.crash_at

    def before(self, step: str) -> None:
        if not self.armed:
            return
        if self._due() and not self.torn:
            raise SimulatedCrash(step)
        self.seen.append(step)

    def write(self, fd: int, data: bytes, step: str) -> None:
        if self._due() and self.torn:
            self.seen.append(step)
            os.write(fd, data[: max(1, len(data) // 2)])
            raise SimulatedCrash(step)
        super().write(fd, data, step)


class Failing(FileOps):
    """Raises ``OSError`` before each named step (an I/O error, not a crash)."""

    def __init__(self, *steps: str) -> None:
        self.failing = set(steps)

    def before(self, step: str) -> None:
        if step in self.failing:
            raise OSError(5, f"injected failure at {step}")


@dataclass
class Scenario:
    name: str
    prepare: Callable[[StateStore, dict[str, Writer]], None]
    operate: Callable[[StateStore, dict[str, Writer]], None]
    atomic_after: str | None


def _writers() -> dict[str, Writer]:
    return {doc_id: Writer(index + 11) for index, doc_id in enumerate(DOCS)}


def _commit(store: StateStore, *appends: tuple[str, bytes], deletes: tuple[str, ...] = ()) -> None:
    store.commit(Batch(appends=tuple(appends), deletes=deletes))


def _prepare_base(store: StateStore, w: dict[str, Writer]) -> None:
    _commit(store, ("meta", w["meta"].put("owner", "alice")))
    _commit(store, (DOC_A, w[DOC_A].replace_text("alpha")), ("tree", w["tree"].put(DOC_A, "row-a")))
    _commit(store, (DOC_B, w[DOC_B].replace_text("beta")), ("tree", w["tree"].put(DOC_B, "row-b")))
    store.compact(DOC_B)
    _commit(store, (DOC_B, w[DOC_B].put("extra", 1)))


def _prepare_many(store: StateStore, w: dict[str, Writer]) -> None:
    _prepare_base(store, w)
    for index in range(30):
        _commit(store, ("tree", w["tree"].put(f"k{index % 4}", index)))


SCENARIOS = [
    Scenario("append-existing", _prepare_base, lambda s, w: _commit(s, ("meta", w["meta"].put("acl", "x"))), None),
    Scenario(
        "append-new-document",
        lambda s, w: _commit(s, ("meta", w["meta"].put("owner", "alice"))),
        lambda s, w: _commit(s, (DOC_A, w[DOC_A].replace_text("new"))),
        None,
    ),
    Scenario(
        "batch-create",
        lambda s, w: _commit(s, ("tree", w["tree"].put("root", 1))),
        lambda s, w: _commit(s, (DOC_A, w[DOC_A].replace_text("new")), ("tree", w["tree"].put(DOC_A, "row"))),
        "pending.rename",
    ),
    Scenario(
        "batch-write-and-delete",
        _prepare_base,
        lambda s, w: _commit(
            s,
            (DOC_A, w[DOC_A].replace_text("alpha-2")),
            ("tree", w["tree"].drop(DOC_B)),
            deletes=(DOC_B,),
        ),
        "pending.rename",
    ),
    Scenario("compact-self", _prepare_many, lambda s, w: s.compact("tree"), None),
    Scenario("compact-given", _prepare_many, lambda s, w: s.compact("tree", w["tree"].full()), None),
    Scenario("compact-content", _prepare_base, lambda s, w: s.compact(DOC_B, w[DOC_B].full()), None),
]


def _observe(space_dir: Path) -> dict[str, Any]:
    store = FileStateStore.open(space_dir)
    try:
        return {doc_id: materialize(store.load(doc_id)) for doc_id in DOCS}
    finally:
        store.close()


def _clean_run(root: Path, scenario: Scenario) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    space_dir = root / "clean" / SPACE_ID
    ops = Steps()
    store = FileStateStore.open(space_dir, create=True, ops=ops)
    writers = _writers()
    scenario.prepare(store, writers)
    before = _observe(space_dir)
    ops.armed = True
    scenario.operate(store, writers)
    store.close()
    return before, _observe(space_dir), list(ops.seen)


def _crash_run(root: Path, scenario: Scenario, index: int, *, torn: bool) -> Path:
    space_dir = root / f"crash-{index}-{int(torn)}" / SPACE_ID
    ops = Steps(index, torn=torn)
    store = FileStateStore.open(space_dir, create=True, ops=ops)
    writers = _writers()
    scenario.prepare(store, writers)
    ops.armed = True
    with pytest.raises(SimulatedCrash):
        scenario.operate(store, writers)
    return space_dir


def _cases(steps: list[str]) -> list[tuple[int, bool]]:
    cases = [(index, False) for index in range(len(steps))]
    cases += [(index, True) for index, step in enumerate(steps) if step.endswith(".write")]
    return cases


def _assert_usable(space_dir: Path) -> None:
    store = FileStateStore.open(space_dir)
    for index, doc_id in enumerate(DOCS):
        _commit(store, (doc_id, Writer(90 + index).put(f"probe-{doc_id}", 1)))
    store.close()
    reopened = FileStateStore.open(space_dir)
    for doc_id in DOCS:
        value = materialize(reopened.load(doc_id))
        assert value is not None and value["m"].get(f"probe-{doc_id}") == 1
    reopened.close()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_every_crash_point_leaves_the_whole_batch_or_none(tmp_path: Path, scenario: Scenario) -> None:
    before, after, steps = _clean_run(tmp_path, scenario)
    assert steps, "the operation performed no injectable step"
    if scenario.name.startswith("compact"):
        assert before == after
    else:
        assert before != after
    rename = steps.index(scenario.atomic_after) if scenario.atomic_after else None
    for index, torn in _cases(steps):
        space_dir = _crash_run(tmp_path, scenario, index, torn=torn)
        outcome = _observe(space_dir)
        label = f"{scenario.name}: crash at step {index} ({steps[index]}, torn={torn})"
        assert outcome in (before, after), label
        if rename is not None:
            assert outcome == (after if index > rename else before), label
        assert not (space_dir / "batch.pending").exists(), label
        _assert_usable(space_dir)


def test_a_crash_during_recovery_still_recovers_the_whole_batch(tmp_path: Path) -> None:
    scenario = SCENARIOS[3]
    _before, after, steps = _clean_run(tmp_path, scenario)
    space_dir = _crash_run(tmp_path, scenario, steps.index("pending.fsync-dir"), torn=False)
    probe = tmp_path / "recover-probe" / SPACE_ID
    probe.parent.mkdir()
    _copy_tree(space_dir, probe)
    recovery = Steps()
    recovery.armed = True
    FileStateStore.open(probe, ops=recovery).close()
    assert "pending.unlink" in recovery.seen
    for index, torn in _cases(recovery.seen):
        attempt = tmp_path / f"recover-{index}-{int(torn)}" / SPACE_ID
        attempt.parent.mkdir()
        _copy_tree(space_dir, attempt)
        ops = Steps(index, torn=torn)
        ops.armed = True
        with pytest.raises(SimulatedCrash):
            FileStateStore.open(attempt, ops=ops)
        assert _observe(attempt) == after, f"recovery crash at {recovery.seen[index]} torn={torn}"


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(exist_ok=True)
    for path in source.iterdir():
        if path.is_dir():
            _copy_tree(path, target / path.name)
        else:
            (target / path.name).write_bytes(path.read_bytes())


def test_a_crash_while_creating_a_space_leaves_it_creatable(tmp_path: Path) -> None:
    probe = Steps()
    probe.armed = True
    FileStateStore.open(tmp_path / "probe" / SPACE_ID, create=True, ops=probe).close()
    assert probe.seen
    for index, torn in _cases(probe.seen):
        space_dir = tmp_path / f"create-{index}-{int(torn)}" / SPACE_ID
        ops = Steps(index, torn=torn)
        ops.armed = True
        with pytest.raises(SimulatedCrash):
            FileStateStore.open(space_dir, create=True, ops=ops)
        store = FileStateStore.open(space_dir, create=True)
        assert store.load("meta") is None
        store.close()
        assert json.loads((space_dir / "space.json").read_text())["layoutVersion"] == 3


_CHILD = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from pycrdt import Doc, Map, Text
    from hyprial.daemon.impl.orgfs.state import Batch, FileOps, FileStateStore

    space_dir, step, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

    class ExitAt(FileOps):
        armed = False
        def before(self, name):
            if self.armed and name == step:
                os._exit(17)

    def edit(doc, mutate):
        before = doc.get_state()
        mutate(doc)
        return doc.get_update(before)

    ops = ExitAt()
    store = FileStateStore.open(space_dir, create=True, ops=ops)
    tree, a, b = Doc(client_id=1), Doc(client_id=2), Doc(client_id=3)
    store.commit(Batch(appends=(("doc-a", edit(a, lambda d: d.get("t", type=Text).insert(0, "alpha"))),
                                ("tree", edit(tree, lambda d: d.get("m", type=Map).update({"doc-a": 1, "doc-b": 1}))))))
    store.commit(Batch(appends=(("doc-b", edit(b, lambda d: d.get("t", type=Text).insert(0, "beta"))),)))
    print("confirmed", flush=True)
    ops.armed = True
    if phase == "batch":
        store.commit(Batch(appends=(("doc-a", edit(a, lambda d: d.get("t", type=Text).insert(5, "-2"))),
                                    ("tree", edit(tree, lambda d: d.get("m", type=Map).pop("doc-b")))),
                           deletes=("doc-b",)))
    else:
        store.commit(Batch(appends=(("doc-a", edit(a, lambda d: d.get("t", type=Text).insert(5, "-2"))),)))
    print("committed", flush=True)
    """
)


@pytest.mark.parametrize(
    ("phase", "step", "applied"),
    [
        ("batch", "pending.fsync", False),
        ("batch", "pending.fsync-dir", True),
        ("batch", "doc.unlink-delta", True),
        ("batch", "pending.unlink", True),
        ("single", "delta.write", False),
        ("single", "delta.fsync", True),
    ],
)
def test_a_real_process_exit_keeps_confirmed_writes_and_batch_atomicity(
    tmp_path: Path, phase: str, step: str, applied: bool
) -> None:
    space_dir = tmp_path / "spaces" / SPACE_ID
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(space_dir), step, phase],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert child.returncode == 17, child.stderr
    assert "confirmed" in child.stdout and "committed" not in child.stdout
    store = FileStateStore.open(space_dir)
    text_a = materialize(store.load("doc-a"))
    tree = materialize(store.load("tree"))
    assert text_a is not None and tree is not None
    assert text_a["t"] == ("alpha-2" if applied else "alpha")
    if phase == "batch":
        assert store.recovered_batch is applied
        assert (store.load("doc-b") is None) is applied
        assert ("doc-b" in tree["m"]) is not applied
    else:
        assert materialize(store.load("doc-b")) == {"m": {}, "t": "beta"}


def _base(space_dir: Path, ops: FileOps) -> tuple[FileStateStore, dict[str, Writer]]:
    store = FileStateStore.open(space_dir, create=True, ops=ops)
    writers = _writers()
    _prepare_base(store, writers)
    return store, writers


@pytest.mark.parametrize("step", ["delta.write", "delta.fsync"])
def test_a_failed_single_append_is_rolled_back_and_the_store_stays_usable(tmp_path: Path, step: str) -> None:
    space_dir = tmp_path / SPACE_ID
    ops = Failing()
    store, w = _base(space_dir, ops)
    delta = space_dir / "meta.delta"
    size = delta.stat().st_size
    ops.failing = {step}
    with pytest.raises(OSError):
        _commit(store, ("meta", w["meta"].put("acl", "lost")))
    ops.failing = set()
    assert delta.stat().st_size == size
    assert materialize(store.load("meta")) == {"m": {"owner": "alice"}, "t": ""}
    _commit(store, ("meta", Writer(50).put("later", 1)))
    store.close()
    assert _observe(space_dir)["meta"] == {"m": {"owner": "alice", "later": 1}, "t": ""}


def test_a_failed_append_that_creates_a_document_leaves_no_file(tmp_path: Path) -> None:
    space_dir = tmp_path / SPACE_ID
    ops = Failing()
    store = FileStateStore.open(space_dir, create=True, ops=ops)
    ops.failing = {"delta.fsync"}
    with pytest.raises(OSError):
        _commit(store, (DOC_A, Writer(1).put("a", 1)))
    assert list((space_dir / "docs").iterdir()) == []
    assert store.load(DOC_A) is None


def test_a_failed_rollback_breaks_the_store_until_reopen(tmp_path: Path) -> None:
    space_dir = tmp_path / SPACE_ID
    ops = Failing()
    store, w = _base(space_dir, ops)
    ops.failing = {"delta.fsync", "delta.rollback-truncate"}
    with pytest.raises(OSError):
        _commit(store, ("meta", w["meta"].put("acl", "maybe")))
    ops.failing = set()
    with pytest.raises(StateStoreBroken):
        store.load("meta")
    with pytest.raises(StateStoreBroken):
        _commit(store, ("meta", Writer(50).put("later", 1)))
    store.close()
    assert _observe(space_dir)["meta"] in (
        {"m": {"owner": "alice"}, "t": ""},
        {"m": {"owner": "alice", "acl": "maybe"}, "t": ""},
    )
    _assert_usable(space_dir)


@pytest.mark.parametrize("step", ["pending.write", "pending.fsync", "pending.rename"])
def test_a_batch_that_fails_before_it_is_durable_applies_nothing(tmp_path: Path, step: str) -> None:
    space_dir = tmp_path / SPACE_ID
    ops = Failing()
    store, w = _base(space_dir, ops)
    before = {doc_id: materialize(store.load(doc_id)) for doc_id in DOCS}
    ops.failing = {step}
    with pytest.raises(OSError):
        _commit(store, (DOC_A, w[DOC_A].replace_text("x")), ("tree", w["tree"].drop(DOC_B)), deletes=(DOC_B,))
    ops.failing = set()
    assert not (space_dir / "batch.pending").exists()
    assert not (space_dir / "batch.pending.tmp").exists()
    assert {doc_id: materialize(store.load(doc_id)) for doc_id in DOCS} == before
    _commit(store, ("meta", Writer(50).put("later", 1)), ("tree", Writer(51).put("later", 1)))
    store.close()
    assert _observe(space_dir)[DOC_B] == before[DOC_B]


@pytest.mark.parametrize("step", ["pending.fsync-dir", "delta.write", "doc.unlink-delta", "pending.unlink"])
def test_a_batch_that_fails_after_it_is_durable_is_redone_on_reopen(tmp_path: Path, step: str) -> None:
    space_dir = tmp_path / SPACE_ID
    ops = Failing()
    store, w = _base(space_dir, ops)
    ops.failing = {step}
    with pytest.raises(OSError):
        _commit(store, (DOC_A, w[DOC_A].replace_text("x")), ("tree", w["tree"].drop(DOC_B)), deletes=(DOC_B,))
    ops.failing = set()
    with pytest.raises(StateStoreBroken):
        store.load(DOC_A)
    store.close()
    reopened = FileStateStore.open(space_dir)
    assert reopened.recovered_batch is True
    assert materialize(reopened.load(DOC_A)) == {"m": {}, "t": "x"}
    assert reopened.load(DOC_B) is None
    assert materialize(reopened.load("tree")) == {"m": {DOC_A: "row-a"}, "t": ""}


@pytest.mark.parametrize(
    "step", ["state.write", "state.fsync", "state.rename", "state.fsync-dir", "delta.compact-truncate", "delta.compact-fsync"]
)
def test_a_failed_compaction_keeps_the_content_and_the_store_usable(tmp_path: Path, step: str) -> None:
    space_dir = tmp_path / SPACE_ID
    ops = Failing()
    store, w = _base(space_dir, ops)
    before = materialize(store.load(DOC_B))
    ops.failing = {step}
    with pytest.raises(OSError):
        store.compact(DOC_B)
    ops.failing = set()
    assert not list((space_dir / "docs").glob("*.tmp"))
    assert materialize(store.load(DOC_B)) == before
    _commit(store, (DOC_B, Writer(60).put("later", 1)))
    store.compact(DOC_B)
    store.close()
    assert _observe(space_dir)[DOC_B] == {"m": {"extra": 1, "later": 1}, "t": "beta"}
