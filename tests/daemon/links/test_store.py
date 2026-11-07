"""WP2 store tests (spec §4.1): local copy, snapshot selection, single writer.

Every test uses ``FakeOrgFs`` (real ``LocalOrgFs`` only in ST11) and never
touches a production home, socket or database.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.links import model
from hyprial.daemon.impl.links.model import LinkEntry, LinkFile
from hyprial.daemon.impl.links.store import LinkStore
from hyprial.daemon.impl.orgfs.api import (
    NodeInfo,
    OrgFsError,
    SpaceInfo,
    TextReadSnapshot,
)

from tests.support.links.org import (
    FakeOrgFs,
    LogRecorder,
    write_reference,
)

A = "agent:o:n1:a"
B = "agent:o:n1:b"
C = "agent:o:n1:c"
D = "agent:o:n1:d"
E = "agent:o:n1:e"
F = "agent:o:n1:f"
X = "agent:p:n2:x"
XS = "agent:o:n2:s"
OWNER = "user:o"
LOCAL = (A, B, C, D, E, F)


def _entry(peer: str, revision: int = 1, now_ms: int = 5) -> LinkEntry:
    return LinkEntry(peer=peer, minted_at_ms=now_ms, revision=revision)


def _file(actor: str, peers: dict[str, LinkEntry], revision: int) -> LinkFile:
    return LinkFile(actor=actor, revision=revision, entries=peers)


class ConflictedLinkOrgFs:
    """OrgFs double with two root-level ``link`` directory siblings."""

    def __init__(self) -> None:
        self.space_id = "s1"
        self.directories: dict[str, dict[str, tuple[str, int]]] = {
            "dir-z": {},
            "dir-a": {},
        }
        self.writes: list[tuple[str, str]] = []

    def put_document(self, directory: str, file: LinkFile) -> None:
        name = f"{file.actor}.json"
        self.directories[directory][name] = (
            json.dumps(model.to_document(file), sort_keys=True),
            1,
        )

    def spaces(self) -> tuple[SpaceInfo, ...]:
        return (
            SpaceInfo(
                space_id=self.space_id,
                name="team",
                owner=OWNER,
                created_at="2026-01-01T00:00:00Z",
            ),
        )

    def mkdir(self, space_id: str, path: str) -> NodeInfo:
        assert space_id == self.space_id and path == "link"
        raise OrgFsError(
            "ambiguous-path", {"candidates": list(self.directories)}
        )

    def listdir(self, space_id: str, node: str) -> tuple[NodeInfo, ...]:
        assert space_id == self.space_id
        if node == "":
            return tuple(
                self._info(directory, "link", "dir")
                for directory in self.directories
            )
        if node == "link":
            raise OrgFsError(
                "ambiguous-path", {"candidates": list(self.directories)}
            )
        directory = node.removeprefix("id:")
        return tuple(
            self._info(f"{directory}:{name}", name, "doc")
            for name in self.directories[directory]
        )

    def read_text_snapshot(
        self, space_id: str, node: str
    ) -> TextReadSnapshot:
        assert space_id == self.space_id
        directory, name = node.removeprefix("id:").split(":", 1)
        content, version = self.directories[directory][name]
        return TextReadSnapshot(
            content=content,
            version=str(version),
            node=self._info(node.removeprefix("id:"), name, "doc"),
        )

    def document_authors(self, space_id: str, node: str) -> tuple[str, ...]:
        assert space_id == self.space_id
        return (OWNER,)

    def write_text(
        self,
        space_id: str,
        node: str,
        content: str,
        *,
        expect_version: str | None = None,
    ) -> NodeInfo:
        assert space_id == self.space_id
        directory, name = node.removeprefix("id:").split("/", 1)
        existing = self.directories[directory].get(name)
        if expect_version is not None:
            assert existing is not None and str(existing[1]) == expect_version
        version = (existing[1] if existing is not None else 0) + 1
        self.directories[directory][name] = (content, version)
        self.writes.append((directory, name))
        return self._info(f"{directory}:{name}", name, "doc", version)

    def watch(self, space_id: str, glob: str, callback: object) -> None:
        assert space_id == self.space_id and glob == "link/*.json"

    def _info(
        self, node_id: str, name: str, kind: str, version: int = 1
    ) -> NodeInfo:
        path = "link" if kind == "dir" else f"link/{name}"
        return NodeInfo(
            space_id=self.space_id,
            node_id=node_id,
            kind=kind,  # type: ignore[arg-type]
            name=name,
            path=path,
            size=None,
            blob_hash=None,
            doc_id=None,
            version=str(version),
            modified_by=OWNER,
            modified_via=None,
            name_conflict=kind == "dir",
            deleted=False,
            content_state="arrived",
            uri=f"orgfs:o:{self.space_id}:{node_id}",
        )


def build_store(
    *,
    fs: FakeOrgFs | ConflictedLinkOrgFs | None = None,
    log: LogRecorder | None = None,
    local: tuple[str, ...] = LOCAL,
    bookkeeping: bool = True,
):
    fs = fs if fs is not None else FakeOrgFs()
    changes: list[int] = []
    prefix = "agent:o:n1:"
    names = {uri[len(prefix) :] for uri in local if uri.startswith(prefix)}

    def is_local(uri: str) -> bool:
        return uri.startswith(prefix) and uri[len(prefix) :] in names

    store = LinkStore(
        fs,
        spaces=lambda: tuple(space.space_id for space in fs.spaces()),
        author_of=lambda uri: model.owner_of(uri),
        is_local_agent=is_local,
        on_change=lambda: changes.append(1),
        logger=log,
        bookkeeping=bookkeeping,
    )
    return store, fs, changes


# -- ST1 -----------------------------------------------------------------


def test_st1_no_space_is_empty_and_write_only_intends() -> None:
    store, fs, _ = build_store()
    index = store.index()
    assert index.spaces == frozenset()
    assert index.writable == frozenset()
    assert dict(index.files) == {}
    assert index.absent == frozenset()
    assert index.conflicted == frozenset()
    assert index.org_available is False
    assert store.refresh() is store.index()
    store.write(_file(A, {B: _entry(B)}, 1))
    assert A in store.intents()
    assert A not in store.index().files
    assert store.flush() == ()
    assert fs.calls == []


# -- ST2 -----------------------------------------------------------------


def test_st2_scan_keeps_legal_files_and_rejects_the_rest_once() -> None:
    log = LogRecorder()
    fs = FakeOrgFs()
    fs.add_space("s1")
    write_reference(fs, A, {B: _entry(B)}, space="s1")
    write_reference(fs, B, {A: _entry(A)}, space="s1")
    # invalid JSON
    fs.put_text("s1", f"link/{D}.json", "{not json", author="user:o")
    # author does not match the actor's owner
    fs.put_document(
        "s1", f"link/{E}.json", model.to_document(_file(E, {}, 1)), author="user:zzz"
    )
    # filename and document actor disagree
    fs.put_document(
        "s1", f"link/{C}.json", model.to_document(_file(B, {}, 1)), author="user:o"
    )
    # from_document rejects the shape
    fs.put_document(
        "s1",
        f"link/{F}.json",
        {"schema": 99, "actor": F, "revision": 1, "peers": []},
        author="user:o",
    )
    store, _, _ = build_store(fs=fs, log=log)
    store.refresh()
    index = store.index()
    assert set(index.files) == {A, B}
    assert log.count("link.file_rejected") == 4


# -- ST3 -----------------------------------------------------------------


def test_st3_highest_revision_snapshot_wins_across_spaces() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    fs.add_space("s2")
    write_reference(fs, A, {B: _entry(B)}, space="s1", revision=2)
    write_reference(fs, A, {}, space="s2", revision=3)
    store, _, _ = build_store(fs=fs)
    store.refresh()
    assert A in store.index().files
    assert B not in store.index().files[A].entries
    assert store.index().files[A].revision == 3


def test_st3_same_revision_same_content_is_usable() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    fs.add_space("s2")
    document = model.to_document(_file(A, {B: _entry(B)}, 2))
    fs.put_document("s1", f"link/{A}.json", document, author="user:o")
    fs.put_document("s2", f"link/{A}.json", document, author="user:o")
    store, _, _ = build_store(fs=fs)
    store.refresh()
    assert A in store.index().files
    assert A not in store.index().conflicted


def test_st3_same_revision_different_content_conflicts() -> None:
    log = LogRecorder()
    fs = FakeOrgFs()
    fs.add_space("s1")
    fs.add_space("s2")
    fs.put_document(
        "s1", f"link/{A}.json", model.to_document(_file(A, {B: _entry(B)}, 2)),
        author="user:o",
    )
    fs.put_document(
        "s2", f"link/{A}.json", model.to_document(_file(A, {C: _entry(C)}, 2)),
        author="user:o",
    )
    store, _, _ = build_store(fs=fs, log=log)
    store.refresh()
    assert A in store.index().conflicted
    assert A not in store.index().files
    assert A not in store.index().absent
    assert log.count("link.file_conflict") == 1


def test_st3_lower_revision_never_rolls_back() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    fs.add_space("s2")
    write_reference(fs, A, {B: _entry(B)}, space="s1", revision=3)
    store, _, _ = build_store(fs=fs)
    store.refresh()
    assert store.index().files[A].revision == 3
    write_reference(fs, A, {}, space="s2", revision=2)
    store.refresh()
    assert store.index().files[A].revision == 3
    assert B in store.index().files[A].entries


def test_st3_leaving_space_drops_below_high_water_to_absent() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    fs.add_space("s2")
    write_reference(fs, A, {B: _entry(B)}, space="s1", revision=2)
    write_reference(fs, A, {}, space="s2", revision=3)
    store, _, _ = build_store(fs=fs)
    store.refresh()
    assert store.index().files[A].revision == 3
    fs.drop_space("s2")
    store.refresh()
    assert A in store.index().absent
    assert A not in store.index().files
    assert store.index().revisions[A] == 3


# -- ST4 -----------------------------------------------------------------


def test_st4_write_is_intent_then_committed_after_every_space() -> None:
    log = LogRecorder()
    fs = FakeOrgFs()
    fs.add_space("s1")
    fs.add_space("s2")
    store, _, _ = build_store(fs=fs, log=log)
    store.refresh()
    assert store.index().writable == {"s1", "s2"}
    document = _file(A, {B: _entry(B)}, 1)
    store.write(document)
    assert store.intents()[A] == document
    assert A not in store.index().files
    assert set(store.flush()) == {A}
    encoded = json.dumps(model.to_document(document), sort_keys=True, separators=(",", ":"))
    for space in ("s1", "s2"):
        assert fs.read_text_snapshot(space, f"link/{A}.json").content == encoded
    assert A in store.index().files
    assert A not in store.intents()
    assert fs.write_versions[-1][2] is None


def test_st4_second_write_carries_expect_version() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    store, _, _ = build_store(fs=fs)
    store.refresh()
    store.write(_file(A, {}, 1))
    store.flush()
    first = fs.read_text_snapshot("s1", f"link/{A}.json").version
    store.write(_file(A, {B: _entry(B)}, 2))
    store.flush()
    assert ("s1", f"id:s1:link/{A}.json", first) in fs.write_versions


# -- ST5 -----------------------------------------------------------------


def test_st5_stale_write_rereads_and_rewrites_then_logs_conflict() -> None:
    log = LogRecorder()
    fs = FakeOrgFs()
    fs.add_space("s1")
    store, _, _ = build_store(fs=fs, log=log)
    store.refresh()
    store.write(_file(A, {}, 1))
    assert set(store.flush()) == {A}
    # Somebody else wrote the file after we recorded its version.
    write_reference(fs, A, {}, space="s1", revision=1)
    store.write(_file(A, {B: _entry(B)}, 2))
    committed = store.flush()
    assert set(committed) == {A}
    assert log.count("link.file_conflict") == 1
    assert A in store.index().files
    assert B in store.index().files[A].entries


def test_st5_intent_dropped_when_revision_was_surpassed() -> None:
    log = LogRecorder()
    fs = FakeOrgFs()
    fs.add_space("s1")
    store, _, _ = build_store(fs=fs, log=log)
    store.refresh()
    store.write(_file(A, {}, 1))
    assert set(store.flush()) == {A}
    # Somebody else's file already moved past the revision this intent carries.
    write_reference(fs, A, {}, space="s1", revision=5)
    store.write(_file(A, {B: _entry(B)}, 2))
    store.flush()
    assert log.count("link.intent_dropped") == 1
    assert A not in store.intents()
    assert B not in store.index().files[A].entries


# -- ST6 -----------------------------------------------------------------


def test_st6_only_this_nodes_actors_may_be_written() -> None:
    store, fs, _ = build_store()
    fs.add_space("s1")
    store.refresh()
    before = len(fs.calls)
    with pytest.raises(ValueError):
        store.write(_file(XS, {A: _entry(A)}, 1))
    with pytest.raises(ValueError):
        store.write(_file(OWNER, {A: _entry(A)}, 1))
    assert len(fs.calls) == before
    assert store.intents() == {}


# -- ST7 -----------------------------------------------------------------


def test_st7_watch_callback_only_kicks() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    store, _, changes = build_store(fs=fs)
    store.refresh()
    fs.fire_change("s1", f"link/{A}.json")
    assert changes == [1]
    assert fs.watch_reads == 0


# -- ST8 -----------------------------------------------------------------


def test_st8_leaving_space_marks_absent_and_keeps_revisions() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    write_reference(fs, A, {B: _entry(B)}, space="s1", revision=4)
    store, _, _ = build_store(fs=fs)
    store.refresh()
    assert A in store.index().files
    fs.drop_space("s1")
    store.refresh()
    assert A in store.index().absent
    assert store.index().known(A)
    assert store.index().revisions[A] == 4
    assert A not in store.index().files


# -- ST9 -----------------------------------------------------------------


def test_st9_all_mirrors_failing_keeps_the_intent() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    store, _, _ = build_store(fs=fs)
    store.refresh()
    store.write(_file(A, {B: _entry(B)}, 1))
    fs.fail_writes.add("s1")
    assert store.flush() == ()
    assert A in store.intents()
    assert A not in store.index().files
    fs.fail_writes.discard("s1")
    assert set(store.flush()) == {A}
    assert A in store.index().files


def test_st9_read_only_space_is_not_writable() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1", writable=False)
    store, _, _ = build_store(fs=fs)
    index = store.refresh()
    assert index.writable == frozenset()
    assert index.org_available is False


def test_st9_conflicting_link_directories_remain_available() -> None:
    fs = ConflictedLinkOrgFs()
    fs.put_document("dir-z", _file(A, {B: _entry(B)}, 1))
    fs.put_document("dir-a", _file(B, {A: _entry(A)}, 1))
    store, _, _ = build_store(fs=fs)

    index = store.refresh()

    assert index.org_available is True
    assert set(index.files) == {A, B}
    store.write(_file(C, {A: _entry(A)}, 1))
    assert store.flush() == (C,)
    assert fs.writes == [("dir-a", f"{C}.json")]


def test_st9_late_canonical_directory_can_receive_existing_actor() -> None:
    fs = ConflictedLinkOrgFs()
    fs.directories.pop("dir-a")
    fs.put_document("dir-z", _file(A, {B: _entry(B)}, 1))
    store, _, _ = build_store(fs=fs)
    assert store.refresh().files[A].revision == 1

    fs.directories["dir-a"] = {}
    store.refresh()
    store.write(_file(A, {C: _entry(C)}, 2))

    assert store.flush() == (A,)
    assert fs.writes == [("dir-a", f"{A}.json")]


def test_st9_unwritable_space_is_reprobed_on_refresh() -> None:
    log = LogRecorder()
    fs = FakeOrgFs()
    fs.add_space("s1", writable=False)
    store, _, _ = build_store(fs=fs, log=log)
    assert store.refresh().org_available is False
    assert log.count("link.space_unwritable") == 1

    fs.set_writable("s1", True)

    assert store.refresh().org_available is True
    store.write(_file(A, {}, 1))
    assert store.flush() == (A,)

    fs.set_writable("s1", False)
    assert store.refresh().org_available is False
    assert log.count("link.space_unwritable") == 2


def test_st9_partial_success_does_not_commit() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    fs.add_space("s2")
    store, _, _ = build_store(fs=fs)
    store.refresh()
    store.write(_file(A, {B: _entry(B)}, 1))
    fs.fail_writes.add("s2")
    assert store.flush() == ()
    assert A not in store.index().files
    assert A in store.intents()


# -- ST10 ----------------------------------------------------------------


def test_st10_absence_then_monotonic_revisions() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    write_reference(fs, A, {B: _entry(B)}, space="s1", revision=5)
    store, _, _ = build_store(fs=fs)
    store.refresh()
    assert store.index().files[A].revision == 5
    fs.remove("s1", f"link/{A}.json")
    store.refresh()
    assert A in store.index().absent
    assert A not in store.index().files
    write_reference(fs, A, {}, space="s1", revision=3)
    store.refresh()
    assert A in store.index().absent
    write_reference(fs, A, {}, space="s1", revision=6)
    store.refresh()
    assert A in store.index().files
    assert store.index().files[A].revision == 6


# -- ST11 ----------------------------------------------------------------


def test_st11_real_local_orgfs_round_trip(tmp_path: Path) -> None:
    from hyprial.daemon.impl.orgfs.docs import LocalOrgFs

    log = LogRecorder()
    org = LocalOrgFs(author=OWNER)
    space = org.create_space("team")
    changes: list[int] = []
    prefix = "agent:o:n1:"
    store = LinkStore(
        org,
        spaces=lambda: (space.space_id,),
        author_of=lambda uri: model.owner_of(uri),
        is_local_agent=lambda uri: uri.startswith(prefix)
        and uri[len(prefix) :] in {"a", "b"},
        on_change=lambda: changes.append(1),
        logger=log,
    )
    store.refresh()
    assert store.index().writable == {space.space_id}
    store.write(_file(A, {B: _entry(B)}, 1))
    store.flush()
    assert store.index().files[A].entries[B].peer == B
    store.refresh()
    assert A in store.index().files
    assert "link/" not in str(tmp_path)


# -- S-B1 / S-B2 ---------------------------------------------------------

_ALLOWED_STORE = {
    "hyprial.kernel",
    "hyprial.daemon.impl.links.ports",
    "hyprial.daemon.impl.links.model",
    "hyprial.daemon.impl.orgfs.api",
}
_ALLOWED_SERVICE = {
    "hyprial.kernel",
    "hyprial.daemon.impl.links.ports",
    "hyprial.daemon.impl.links.model",
    "hyprial.daemon.impl.links.controller",
    "hyprial.daemon.impl.links.policy",
    "hyprial.daemon.impl.links.store",
}


def _links_dir() -> Path:
    import hyprial.daemon.impl.links as links

    return Path(links.__file__).parent


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


def test_s_b1_links_imports_stay_within_the_allowed_set() -> None:
    links_dir = _links_dir()
    store_imports = {
        name for name in _imports(links_dir / "store.py") if name.startswith("hyprial")
    }
    assert store_imports <= _ALLOWED_STORE, store_imports - _ALLOWED_STORE
    # The engine is a package since spec §9.5; every submodule of it is held to
    # the service allow-list (relative imports inside the package are exempt).
    service_files = sorted((links_dir / "service").glob("*.py"))
    assert service_files, "the service package must exist"
    for path in service_files:
        service_imports = {
            name for name in _imports(path) if name.startswith("hyprial")
        }
        assert service_imports <= _ALLOWED_SERVICE, (
            path.name,
            service_imports - _ALLOWED_SERVICE,
        )
    # links/__init__.py still only imports ports (WP0 guard, ruling 32).
    init_imports = {
        name
        for name in _imports(links_dir / "__init__.py")
        if name.startswith("hyprial")
    }
    assert init_imports == {"hyprial.daemon.impl.links.ports"}


def test_s_b1_inbox_never_imports_the_links_implementation() -> None:
    import hyprial.daemon.impl.inbox as inbox

    root = Path(inbox.__file__).parent
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        for name in _imports(path):
            if name in (
                "hyprial.daemon.impl.links.service",
                "hyprial.daemon.impl.links.store",
            ):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_s_b2_links_holds_exactly_seven_modules() -> None:
    links_dir = _links_dir()
    modules = sorted(path.name for path in links_dir.glob("*.py"))
    # §9.5: the engine moved into ``service/``; the top level keeps the WP0/WP1
    # faces plus ``store``.  The package is the seventh module.
    assert modules == [
        "__init__.py",
        "controller.py",
        "model.py",
        "policy.py",
        "ports.py",
        "store.py",
    ], modules
    service_dir = links_dir / "service"
    assert service_dir.is_dir(), "links/service must be a package directory"
    assert (service_dir / "__init__.py").is_file()
    assert len(sorted(service_dir.glob("*.py"))) <= 7, sorted(
        path.name for path in service_dir.glob("*.py")
    )



def test_bookkeeping_off_drops_intents_and_writes_nothing() -> None:
    store, fs, _ = build_store(bookkeeping=False)
    fs.add_space("s1")
    store.refresh()
    fs.calls.clear()
    fs.write_versions.clear()

    store.write(_file(A, {B: _entry(B, revision=2)}, 1))
    assert store.intents() == {}, "write() must drop the intent before flush"

    assert store.flush() == ()

    assert store.intents() == {}
    assert fs.write_versions == []
    assert [c for c in fs.calls if c[0] in ("mkdir", "write_text", "move", "remove")] == []
    assert fs.index().files == {} if hasattr(fs, "index") else True


def test_flush_without_a_writable_space_logs_once_per_transition() -> None:
    logs = LogRecorder()
    store, fs, _ = build_store(log=logs)
    store.write(_file(A, {B: _entry(B, revision=2)}, 1))

    assert store.flush() == ()
    assert store.flush() == ()
    skipped = logs.fields_for("link.flush_skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "no-writable-space"
    assert skipped[0]["intents"] == 1


def test_flush_with_a_writable_space_does_not_log_the_skip() -> None:
    logs = LogRecorder()
    store, fs, _ = build_store(log=logs)
    fs.add_space("s1")
    store.refresh()
    store.write(_file(A, {B: _entry(B, revision=2)}, 1))

    assert store.flush() == (A,)
    assert logs.fields_for("link.flush_skipped") == []


def test_ambiguous_link_roots_log_once_per_space() -> None:
    class _Roots:
        def mkdir(self, *_args, **_kwargs):
            raise OrgFsError("invalid-argument", {"message": "exists"})

        def listdir(self, _space, node):
            assert node == ""
            from types import SimpleNamespace

            return (
                SimpleNamespace(kind="dir", name="link", node_id="id-a"),
                SimpleNamespace(kind="dir", name="link", node_id="id-b"),
            )

    logs = LogRecorder()
    store = LinkStore(
        _Roots(),
        spaces=lambda: (),
        author_of=lambda uri: model.owner_of(uri),
        is_local_agent=lambda uri: uri in LOCAL,
        on_change=lambda: None,
        logger=logs,
    )
    store._probe("s1")
    store._probe("s1")

    events = logs.fields_for("link.space_ambiguous")
    assert len(events) == 1
    assert events[0]["space"] == "s1"
    assert events[0]["candidates"] == ["id-a", "id-b"]
    assert events[0]["canonical"] == "id-a"


def test_refresh_logs_attach_summary_when_live_set_changes() -> None:
    logs = LogRecorder()
    store, fs, _ = build_store(log=logs)
    fs.add_space("s1")
    store.refresh()
    first = logs.fields_for("link.attach.summary")
    assert len(first) == 1
    assert first[0]["attached"] == ["s1"]
    assert first[0]["skipped"] == []
    assert first[0]["failed"] == []
    assert first[0]["bookkeeping"] is True
    store.refresh()
    assert len(logs.fields_for("link.attach.summary")) == 1



def test_a_single_link_root_does_not_log_ambiguity() -> None:
    class _OneRoot:
        def mkdir(self, *_args, **_kwargs):
            raise OrgFsError("invalid-argument", {"message": "exists"})

        def listdir(self, _space, node):
            assert node == ""
            from types import SimpleNamespace

            return (
                SimpleNamespace(kind="dir", name="link", node_id="id-only"),
            )

    logs = LogRecorder()
    store = LinkStore(
        _OneRoot(),
        spaces=lambda: (),
        author_of=lambda uri: model.owner_of(uri),
        is_local_agent=lambda uri: uri in LOCAL,
        on_change=lambda: None,
        logger=logs,
    )
    store._probe("s1")

    assert logs.fields_for("link.space_ambiguous") == []
