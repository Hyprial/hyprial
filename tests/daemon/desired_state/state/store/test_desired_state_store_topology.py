"""Topological guards for the SQLite-only desired-state authority (U0a-2).

Two structural facts the design rests on, each pinned so that BREAKING it
turns a test red -- not "today it passes":

  1. One document per state directory.  Two stores aiming DIFFERENT
     documents at the same directory would silently clobber each other's
     committed state; the store refuses that construction loudly.

  2. Who constructs the stores.  The daemon's LifecycleCoordinator and
     the daemon's DesiredStateStore must receive the ONE shared
     ``StateDatabase`` instance (that is what makes in-process write
     contention impossible by construction).  The sanctioned constructions
     are:
       - application.py: LifecycleCoordinator(self.state_db, ...,
                         journal_store=self._state_persistence.settled_journal)
                         DesiredStateStore(..., state_db=self.state_db)
       - management.py:   DesiredStateStore(...) behind the
                          OfflineManagementLease / DaemonStateOwnershipFence
                          (flock-mutually-exclusive with a running daemon,
                          and that path never builds a process manager)
       - cli.py:          a read-only `.load()` (card 3c116ad2: the upgrade's
                          restore budget scales with the desired-state rows).
                          It cannot take the ownership fence -- it runs while
                          the freshly restarted daemon owns the document --
                          so the test pins that this site only ever loads.
       - transfer/runtime_state.py: explicit offline runtime downgrade behind
                          the same DaemonStateOwnershipFence; no process manager.
     A fifth construction site -- or one of these losing its shared
     instance / fence / read-only shape -- must update this test deliberately.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import (
    DesiredState,
    DesiredStateStore,
)

from hyprial.kernel import DesiredStateError

_SRC = Path(__file__).resolve().parents[5] / 'src/hyprial'


def test_two_stores_with_different_documents_in_one_dir_fail_loudly(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.json").write_text("[]", encoding="utf-8")
    (tmp_path / "b.json").write_text("[]", encoding="utf-8")
    live = DesiredStateStore(tmp_path / "a.json")

    with pytest.raises(DesiredStateError, match="one state directory"):
        DesiredStateStore(tmp_path / "b.json")
    assert live.load() is not None


def test_reopening_the_same_document_in_one_dir_is_fine(
    tmp_path: Path,
) -> None:
    first = DesiredStateStore(tmp_path / "desired-state.json")
    first.set_mailbox_role(True)
    second = DesiredStateStore(tmp_path / "desired-state.json")
    assert second.load() == DesiredState(as_mailbox=True)


def _construction_sites(call: str) -> list[tuple[Path, str]]:
    """Every ``call(...)`` occurrence in src, with its surrounding text."""

    origins = {
        "LifecycleCoordinator": {
            "daemon/impl/application/actors/lifecycle_ops.py": "daemon/application.py",
        },
        "DesiredStateStore": {
            "daemon/impl/application/__init__.py": "daemon/application.py",
            "shell/impl/cli/commands/upgrade/commands.py": "cli.py",
            "shell/impl/cli/commands/squire/user_proxy.py": "cli.py",
            "daemon/impl/operations/management.py": "daemon/management.py",
            "daemon/impl/transfer/landing/runtime.py": "transfer/runtime_state.py",
        },
    }.get(call, {})
    grouped: dict[Path, list[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        constructions = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name) and node.func.id == call]
        if not constructions:
            continue
        if re.search(rf"(class {call}\b|def {call}\b)", text):
            raise AssertionError(
                f"scan assumption broken: {path} defines {call}; the scan "
                "only looks for construction call sites"
            )
        relative = str(path.relative_to(_SRC))
        if relative in origins and len(constructions) != len({call}):
            raise AssertionError(f"Additional constructor at the exact native site: {relative}")
        origin = _SRC / origins.get(relative, relative)
        grouped.setdefault(origin, []).append(text)
    return [(path, "\n".join(grouped[path])) for path in sorted(grouped)]


def _assert_shared_lifecycle_constructor(text: str) -> None:
    calls = [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "LifecycleCoordinator"
    ]
    assert len(calls) == 1, "expected exactly one LifecycleCoordinator call"
    call = calls[0]
    assert call.args and ast.unparse(call.args[0]) == "self.state_db", (
        "LifecycleCoordinator no longer receives the daemon's shared StateDatabase"
    )
    journal = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "journal_store"),
        None,
    )
    assert journal is not None and ast.unparse(journal) == (
        "self._state_persistence.settled_journal"
    ), "LifecycleCoordinator no longer uses the StatePersistence journal owner"


def test_lifecycle_process_manager_has_exactly_one_construction_site() -> None:
    sites = _construction_sites("LifecycleCoordinator")
    assert [path.name for path, _text in sites] == ["application.py"], (
        "a second LifecycleCoordinator construction point appeared: the "
        "journal and the desired-state store share ONE StateDatabase only "
        "because the daemon wires the same instance through this single "
        "site; wire the new site through the same shared instance and "
        "update this test deliberately"
    )
    text = sites[0][1]
    _assert_shared_lifecycle_constructor(text)

    persistence = "\n".join((_SRC / "daemon/impl/state_persistence" / name).read_text(encoding="utf-8")
                            for name in ("authority.py", "ports.py"))
    assert "self.state_db: StateDatabase = desired_store.state_db" in persistence
    assert "self._journal_store = _LifecycleStore(self.state_db)" in persistence
    assert "settlement_owner = _SettlementWaitingOwner(self)" in persistence
    assert "self.state_db = owner.state_db" in persistence
    assert "self.settled_journal = _JournalPort(settlement_owner)" in persistence
    assert "self._state_db = owner.state_db" in persistence


def test_lifecycle_topology_guard_rejects_split_db_or_journal() -> None:
    text = (_SRC / "daemon/impl/application/actors/lifecycle_ops.py").read_text(encoding="utf-8")
    split_db = text.replace(
        "manager = LifecycleCoordinator(\n            self.state_db,",
        "manager = LifecycleCoordinator(\n            StateDatabase(self.state_dir / 'other.sqlite3'),",
        1,
    )
    assert split_db != text, "DB negative control no longer targets the constructor"
    with pytest.raises(AssertionError, match="shared StateDatabase"):
        _assert_shared_lifecycle_constructor(split_db)

    split_journal = text.replace(
        "journal_store=self._state_persistence.settled_journal,",
        "journal_store=None,",
        1,
    )
    assert split_journal != text, "journal negative control lost its target"
    with pytest.raises(AssertionError, match="StatePersistence journal owner"):
        _assert_shared_lifecycle_constructor(split_journal)


def test_desired_state_store_has_exactly_four_construction_sites() -> None:
    sites = _construction_sites("DesiredStateStore")
    assert [path.name for path, _text in sites] == [
        "cli.py",
        "application.py",
        "management.py",
        "runtime_state.py",
    ], (
        "a DesiredStateStore construction point appeared or vanished in src: "
        "the daemon's store must receive the shared StateDatabase and the "
        "offline store must stay behind the ownership fence; update this "
        "test deliberately"
    )
    for path, text in sites:
        match = re.search(r"DesiredStateStore\(([^)]*)\)", text, re.DOTALL)
        assert match is not None
        if path.name == "application.py":
            assert "state_db=self.state_db" in match.group(1), (
                "the daemon's DesiredStateStore no longer shares the "
                "daemon's StateDatabase"
            )
        elif path.name == "cli.py":
            # 3c116ad2: the upgrade's restore budget scales with this
            # machine's desired-state rows, read through the store's own
            # load (never a second reader).  Read-only by contract and it
            # cannot take the ownership fence -- it runs while the freshly
            # restarted daemon owns the document -- so what is pinned here
            # is that the site only ever loads: a save() at this site would
            # be an unfenced writer against a live daemon.
            after = text[match.start() :]
            store_call = re.search(
                r"DesiredStateStore\([^)]*\)\.load\(\)", after, re.DOTALL
            )
            assert store_call is not None, (
                "the CLI's DesiredStateStore site must be a read-only "
                "`.load()` on the constructed store; anything else is an "
                "unfenced mutation path against the running daemon"
            )
        else:
            before = text[: match.start()]
            fence = before.rfind("DaemonStateOwnershipFence.acquire")
            assert fence != -1, (
                "the offline DesiredStateStore lost its ownership fence "
                "(it would no longer be mutually exclusive with a running "
                "daemon)"
            )
