"""Writer bindings in verification and their cache invalidation (contract OC0 §1.4.5, §1.4.6).

The org label store that supplies non-local bindings arrives later (OC3);
here a mutable fake stands in for it, at the port.  Review ruling 2: the
callable must not raise, the runtime still defends (previous snapshot, one
failure log per failed call, an empty snapshot when the first call fails).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.codec import encode
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.kernel import keyed_cell_id, subject_cell_id

from tests.daemon.cells.runtime.cell_runtime_support import (
    ALICE,
    ORG,
    authority_for,
    org_fs,
    runtime,
    uid_for,
)

BOB = authority_for("user:bob")
BOB_REF = f"u.{uid_for('user:bob')}"
AGENT_ID = "a." + "k" * 26


class Labels:
    """A mutable writer-binding source; ``failing`` makes the next calls raise."""

    def __init__(self, **bindings: str) -> None:
        self.bindings = dict(bindings)
        self.failing = False
        self.calls = 0

    def __call__(self) -> dict[str, str]:
        self.calls += 1
        if self.failing:
            raise RuntimeError("label store unreadable")
        return dict(self.bindings)


def _write_as(fs: Any, space: str, cell: Cell, author: str, **options: Any) -> None:
    for directory in ("cells", f"cells/{cell.kind}"):
        try:
            fs.mkdir(space, directory)
        except Exception:  # noqa: BLE001 - already there
            pass
    fs.author, previous = author, fs.author
    try:
        fs.write_text(space, f"cells/{cell.kind}/{cell.cell_id}.json", encode(cell), **options)
    finally:
        fs.author = previous


def _bob_org_cell(revision: int = 1, name: str = "bob-org", space_id: str = "sp-bob") -> Cell:
    return Cell(
        cell_id=keyed_cell_id("org", BOB, space_id),
        kind="org",
        authority=BOB,
        scope=CellScope.of_org(ORG),
        binding=CellBinding.keyed(space_id),
        name=name,
        members=(CellMember(BOB_REF, "owner"),),
        params={
            "org.name": ORG,
            "org.issuer": "https://login.example.com",
            "org.directorySpace": space_id,
            "org.tailnet": space_id,
            "org.parent": None,
            "org.members": {
                BOB_REF: {
                    "username": "bob",
                    "joinedAtMs": 1_000,
                    "invitedBy": BOB_REF,
                    "devices": [
                        {
                            "deviceUid": "d." + "b" * 32,
                            "keyId": None,
                            "publicKey": None,
                            "verifiedAtMs": 1_000,
                        }
                    ],
                }
            },
        },
        revision=revision,
    )


def _plant_bob(fs: Any, space: str, cell: Cell | None = None) -> Cell:
    fs.invite(space, "user:bob")
    cell = cell or _bob_org_cell(space_id=space)
    _write_as(fs, space, cell, "user:bob")
    return cell


def _rewrite_bob(fs: Any, space: str, cell: Cell) -> None:
    path = f"cells/{cell.kind}/{cell.cell_id}.json"
    _text, version = fs.read_text(space, path)
    _write_as(fs, space, cell, "user:bob", expect_version=version)


def _events(log: Any, name: str) -> list[tuple[str, dict[str, Any]]]:
    return [(level, fields) for level, event, fields in log.events if event == name]


def test_a_local_document_by_the_local_writer_is_adopted_by_another_device(
    tmp_path: Path,
) -> None:
    fs, _space = org_fs()
    first, _ = runtime(tmp_path, fs, name="first.sqlite3")
    cell = first.ensure(CellSpec(kind="agent", binding=CellBinding.subject(AGENT_ID)))
    first.run_once()
    second, log = runtime(tmp_path, fs, name="second.sqlite3")
    second.run_once()
    assert second.lookup(cell.cell_id) == cell
    assert _events(log, "cell.document_rejected") == []
    first.close()
    second.close()


def test_a_bound_foreign_keyed_cell_is_indexed_and_an_unbound_one_is_not(
    tmp_path: Path,
) -> None:
    fs, space = org_fs()
    bob = _plant_bob(fs, space)
    unbound, log = runtime(tmp_path, fs, name="unbound.sqlite3")
    unbound.run_once()
    unbound.run_once()
    assert unbound.lookup(bob.cell_id) is None
    rejected = _events(log, "cell.document_rejected")
    assert [(level, fields["reason"]) for level, fields in rejected] == [
        ("info", "authority-unbound")
    ]
    bound, _ = runtime(tmp_path, fs, name="bound.sqlite3", writer_bindings=Labels(**{BOB: "user:bob"}))
    bound.run_once()
    assert bound.lookup(bob.cell_id) == bob
    unbound.close()
    bound.close()


def test_a_foreign_agent_cell_is_never_indexed_bound_or_not(tmp_path: Path) -> None:
    fs, space = org_fs()
    fs.invite(space, "user:bob")
    foreign = Cell(cell_id=subject_cell_id("agent", AGENT_ID), kind="agent", authority=BOB,
                   scope=CellScope.of_org(), binding=CellBinding.subject(AGENT_ID))
    _write_as(fs, space, foreign, "user:bob")
    for name, labels in (("unbound", Labels()), ("bound", Labels(**{BOB: "user:bob"}))):
        cells, _ = runtime(tmp_path, fs, name=f"{name}.sqlite3", writer_bindings=labels)
        cells.run_once()
        cells.run_once()
        assert cells.index().of_kind("agent") == ()
        cells.close()


def test_the_stale_write_re_read_uses_the_pass_bindings_and_blocks(tmp_path: Path) -> None:
    fs, space = org_fs()
    fs.invite(space, "user:bob")
    foreign = Cell(cell_id=subject_cell_id("agent", AGENT_ID), kind="agent", authority=BOB,
                   scope=CellScope.of_org(), binding=CellBinding.subject(AGENT_ID))
    _write_as(fs, space, foreign, "user:bob")
    path = f"cells/agent/{foreign.cell_id}.json"
    before = fs.read_text(space, path)
    cells, log = runtime(tmp_path, fs)
    mine = cells.ensure(CellSpec(kind="agent", binding=CellBinding.subject(AGENT_ID)))
    cells.run_once()
    assert fs.read_text(space, path) == before
    blocked = _events(log, "cell.document_blocked")
    assert [fields["reason"] for _level, fields in blocked] == ["authority-unbound"]
    assert cells.lookup(mine.cell_id) == mine and mine.cell_id in cells.dirty()
    cells.close()


def test_the_local_binding_cannot_be_overridden_by_a_label(tmp_path: Path) -> None:
    fs, _space = org_fs()
    first, _ = runtime(tmp_path, fs, name="first.sqlite3")
    cell = first.ensure(CellSpec(kind="agent", binding=CellBinding.subject(AGENT_ID)))
    first.run_once()
    second, _ = runtime(tmp_path, fs, name="second.sqlite3",
                        writer_bindings=Labels(**{ALICE: "user:mallory"}))
    second.run_once()
    assert second.lookup(cell.cell_id) == cell
    first.close()
    second.close()


def test_org_cell_before_label_is_adopted_on_the_first_pass_after_the_label(
    tmp_path: Path,
) -> None:
    fs, space = org_fs()
    bob = _plant_bob(fs, space)
    labels = Labels()
    cells, log = runtime(tmp_path, fs, writer_bindings=labels)
    cells.run_once()
    assert cells.lookup(bob.cell_id) is None
    labels.bindings[BOB] = "user:bob"
    cells.run_once()
    assert cells.lookup(bob.cell_id) == bob
    changed = _events(log, "cell.writer_bindings_changed")
    assert changed == [("info", {"bindings": 2})]
    cells.close()


def test_a_pass_in_flight_while_the_label_lands_does_not_keep_the_old_rejection(
    tmp_path: Path,
) -> None:
    inner, space = org_fs()
    bob = _plant_bob(inner, space)
    labels = Labels()

    class LandsDuringScan:
        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

        def read_text_snapshot(self, *args: Any, **kwargs: Any) -> Any:
            labels.bindings[BOB] = "user:bob"
            return inner.read_text_snapshot(*args, **kwargs)

    cells, _ = runtime(tmp_path, LandsDuringScan(), writer_bindings=labels)
    cells.run_once()
    assert cells.lookup(bob.cell_id) is None
    cells.run_once()
    assert cells.lookup(bob.cell_id) == bob
    cells.close()


def test_label_removed_keeps_the_held_cell_and_rejects_new_versions(tmp_path: Path) -> None:
    fs, space = org_fs()
    bob = _plant_bob(fs, space)
    labels = Labels(**{BOB: "user:bob"})
    cells, _ = runtime(tmp_path, fs, writer_bindings=labels)
    cells.run_once()
    assert cells.lookup(bob.cell_id) == bob
    del labels.bindings[BOB]
    cells.run_once()
    assert cells.lookup(bob.cell_id) == bob
    newer = _bob_org_cell(revision=2, name="newer", space_id=space)
    _rewrite_bob(fs, space, newer)
    cells.run_once()
    assert cells.lookup(bob.cell_id) == bob
    cells.close()


def test_label_restored_adopts_the_newest_revision(tmp_path: Path) -> None:
    fs, space = org_fs()
    bob = _plant_bob(fs, space)
    labels = Labels(**{BOB: "user:bob"})
    cells, _ = runtime(tmp_path, fs, writer_bindings=labels)
    cells.run_once()
    del labels.bindings[BOB]
    cells.run_once()
    newer = _bob_org_cell(revision=3, name="newest", space_id=space)
    _rewrite_bob(fs, space, newer)
    cells.run_once()
    assert cells.lookup(bob.cell_id) == bob
    labels.bindings[BOB] = "user:bob"
    cells.run_once()
    assert cells.lookup(bob.cell_id) == newer
    cells.close()


def test_a_well_behaved_binding_source_logs_no_failure(tmp_path: Path) -> None:
    fs, space = org_fs()
    bob = _plant_bob(fs, space)
    labels = Labels(**{BOB: "user:bob"})
    cells, log = runtime(tmp_path, fs, writer_bindings=labels)
    cells.run_once()
    cells.run_once()
    assert labels.calls == 2
    assert cells.lookup(bob.cell_id) == bob
    assert _events(log, "cell.writer_bindings_failed") == []
    assert _events(log, "cell.writer_bindings_changed") == []
    cells.close()


def test_a_failing_binding_source_keeps_the_previous_snapshot_and_logs_each_failure(
    tmp_path: Path,
) -> None:
    fs, space = org_fs()
    _plant_bob(fs, space)
    labels = Labels(**{BOB: "user:bob"})
    cells, log = runtime(tmp_path, fs, writer_bindings=labels)
    cells.run_once()
    labels.failing = True
    newer = _bob_org_cell(revision=2, name="while failing", space_id=space)
    _rewrite_bob(fs, space, newer)
    cells.run_once()
    cells.run_once()
    assert cells.lookup(newer.cell_id) == newer
    failed = _events(log, "cell.writer_bindings_failed")
    assert [level for level, _fields in failed] == ["warn", "warn"]
    assert failed[0][1]["errorType"] == "RuntimeError"
    assert _events(log, "cell.writer_bindings_changed") == []
    cells.close()


def test_a_first_call_failure_uses_only_the_local_binding(tmp_path: Path) -> None:
    fs, space = org_fs()
    bob = _plant_bob(fs, space)
    labels = Labels(**{BOB: "user:bob"})
    labels.failing = True
    cells, log = runtime(tmp_path, fs, writer_bindings=labels)
    mine = cells.ensure(CellSpec(kind="agent", binding=CellBinding.subject(AGENT_ID)))
    cells.run_once()
    assert cells.lookup(bob.cell_id) is None
    assert cells.dirty() == frozenset()
    assert cells.lookup(mine.cell_id) == mine
    assert len(_events(log, "cell.writer_bindings_failed")) == 1
    labels.failing = False
    cells.run_once()
    assert cells.lookup(bob.cell_id) == bob
    cells.close()
