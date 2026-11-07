"""WP1 model tests: link identity, files, derivation and the codec.

Cases M1-M12 of docs/design/send-links-impl/wp1.md §3.1.
"""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.links.model import (
    LINK_FILE_SCHEMA,
    LinkEntry,
    LinkFile,
    derive_link,
    effective,
    from_document,
    is_owner_pair,
    link_file_path,
    link_key,
    links_of,
    owner_of,
    task_bound_peers,
    to_document,
    touch,
    with_reference,
    without_reference,
)
from hyprial.daemon.impl.links.ports import LINK_DIR

A = "agent:o:m:a"
B = "agent:o:m:b"
C = "agent:o:m:c"
OWNER = "user:o"
X = "agent:p:m:x"
U = "user:p"


def _entry(
    peer: str,
    *,
    minted_at_ms: int = 0,
    revision: int = 1,
    lifecycle: str = "regular",
    task_id: str | None = None,
    last_used_ms: int = 0,
) -> LinkEntry:
    return LinkEntry(
        peer=peer,
        minted_at_ms=minted_at_ms,
        revision=revision,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        task_id=task_id,
        last_used_ms=last_used_ms,
    )


def _file(actor: str, *entries: LinkEntry, revision: int = 1) -> LinkFile:
    return LinkFile(
        actor=actor, revision=revision, entries={entry.peer: entry for entry in entries}
    )


def test_m1_link_key_is_the_unordered_pair() -> None:
    assert link_key(B, A) == link_key(A, B) == (A, B)
    for first, second in ((A, A), (A, "bad"), (A, "route:x:y")):
        with pytest.raises(ValueError):
            link_key(first, second)


def test_m2_owner_edges_are_agent_to_its_own_owner() -> None:
    assert is_owner_pair(A, OWNER)
    assert is_owner_pair(OWNER, A)
    assert not is_owner_pair(A, U)
    assert not is_owner_pair(X, OWNER)
    assert not is_owner_pair(A, B)
    assert owner_of(A) == OWNER
    assert owner_of(OWNER) is None
    assert owner_of("route:x:y") is None


def test_m3_two_mutual_references_make_an_effective_link() -> None:
    mine = _file(A, _entry(B))
    theirs = _file(B, _entry(A))
    both = derive_link(A, B, mine, theirs)
    assert both is not None
    assert both.state == "established" and effective(both)
    assert both.mine and both.theirs and both.entry is not None

    only_mine = derive_link(A, B, mine, None)
    assert only_mine is not None and only_mine.state == "pending"
    assert only_mine.mine and not only_mine.theirs

    only_theirs = derive_link(A, B, None, _file(B, _entry(A)))
    assert only_theirs is not None and only_theirs.state == "pending"
    assert not only_theirs.mine and only_theirs.theirs and only_theirs.entry is None

    assert derive_link(A, B, None, None) is None

    reclaimed = derive_link(A, B, mine, theirs, reclaimed=True)
    assert reclaimed is not None and reclaimed.state == "reclaimed"
    assert not effective(reclaimed)


def test_m4_a_missing_peer_copy_never_becomes_established_or_lost() -> None:
    mine = _file(A, _entry(B))
    link = derive_link(A, B, mine, None, reclaimed=False)
    assert link is not None and link.state == "pending"
    assert not effective(link)
    # The peer's file exists but has not synced my reference yet: same result.
    unsynced = derive_link(A, B, mine, _file(B))
    assert unsynced is not None and unsynced.state == "pending"


def test_m5_derive_link_rejects_a_file_for_the_wrong_actor() -> None:
    with pytest.raises(ValueError):
        derive_link(A, B, _file(B, _entry(A)), None)
    with pytest.raises(ValueError):
        derive_link(A, B, None, _file(C, _entry(A)))


def test_m6_lifecycle_is_coupled_to_task_binding() -> None:
    with pytest.raises(ValueError):
        _entry(B, lifecycle="pac_task", task_id=None)
    with pytest.raises(ValueError):
        _entry(B, lifecycle="regular", task_id="t1")
    with pytest.raises(ValueError):
        _entry(B, lifecycle="unknown")
    file = _file(
        A,
        _entry(B, lifecycle="pac_task", task_id="t1"),
        _entry(C, lifecycle="pac_task", task_id="t1"),
        _entry(X, lifecycle="pac_task", task_id="t2"),
        _entry(U),
    )
    assert task_bound_peers(file, "t1") == (B, C)
    assert task_bound_peers(file, "t2") == (X,)
    assert task_bound_peers(file, "nope") == ()


def test_m7_writes_bump_the_revision_and_stamp_the_entry() -> None:
    file = LinkFile(actor=A, revision=5, entries={})
    first = with_reference(file, _entry(B, revision=1))
    assert first.revision == 6
    assert first.entries[B].revision == 6
    second = with_reference(first, _entry(B, minted_at_ms=99, revision=1))
    assert second.revision == 7
    assert len(second.entries) == 1 and second.entries[B].minted_at_ms == 99
    assert without_reference(second, C) == second
    removed = without_reference(second, B)
    assert removed.revision == 8 and removed.entries == {}
    assert file.revision == 5 and file.entries == {}


def test_m8_touch_only_moves_last_used_and_keeps_the_revision() -> None:
    file = _file(A, _entry(B, last_used_ms=5), revision=3)
    old = touch(file, B, 3)
    assert old.entries[B].last_used_ms == 5 and old.revision == 3
    new = touch(file, B, 9)
    assert new.entries[B].last_used_ms == 9 and new.revision == 3
    assert file.entries[B].last_used_ms == 5
    assert touch(file, C, 9) == file


def test_m9_owner_edges_are_never_written_to_orgfs() -> None:
    file = _file(A, _entry(B))
    with pytest.raises(ValueError):
        with_reference(file, _entry(OWNER, revision=1))
    with pytest.raises(ValueError):
        LinkFile(actor=A, revision=1, entries={OWNER: _entry(OWNER)})


def test_m10_document_roundtrip_is_strict() -> None:
    file = _file(
        A,
        _entry(B, minted_at_ms=10, last_used_ms=20),
        _entry(C, lifecycle="pac_task", task_id="t1"),
        revision=4,
    )
    document = to_document(file)
    assert document["schema"] == LINK_FILE_SCHEMA
    assert [peer["peer"] for peer in document["peers"]] == [B, C]
    assert from_document(document) == file
    serialized = json.dumps(document, sort_keys=True)
    swapped = _file(
        A,
        _entry(C, lifecycle="pac_task", task_id="t1"),
        _entry(B, minted_at_ms=10, last_used_ms=20),
        revision=4,
    )
    assert json.dumps(to_document(swapped), sort_keys=True) == serialized

    missing_key = {key: value for key, value in document.items() if key != "schema"}
    unknown_schema = {**document, "schema": 2}
    duplicate = {**document, "peers": document["peers"] + document["peers"][:1]}
    illegal_uri = {**document, "peers": [{**document["peers"][0], "peer": "bad"}]}
    string_revision = {**document, "revision": "1"}
    bool_revision = {**document, "revision": True}
    negative_revision = {**document, "revision": -1}
    unknown_lifecycle = {
        **document,
        "peers": [{**document["peers"][0], "lifecycle": "eternal"}],
    }
    self_peer = {**document, "peers": [{**document["peers"][0], "peer": A}]}
    owner_peer = {**document, "peers": [{**document["peers"][0], "peer": OWNER}]}
    extra_key = {**document, "extra": 1}
    for bad in (
        missing_key,
        unknown_schema,
        duplicate,
        illegal_uri,
        string_revision,
        bool_revision,
        negative_revision,
        unknown_lifecycle,
        self_peer,
        owner_peer,
        extra_key,
        "not a mapping",
    ):
        with pytest.raises(ValueError):
            from_document(bad)  # type: ignore[arg-type]


def test_m11_link_file_path_is_under_the_ports_link_dir() -> None:
    assert link_file_path(A) == "link/agent:o:m:a.json"
    assert link_file_path(A) == f"{LINK_DIR}/{A}.json"
    with pytest.raises(ValueError):
        link_file_path("bad")
    with pytest.raises(ValueError):
        link_file_path("route:x:y")


def test_m12_links_of_collects_every_edge_touching_the_actor() -> None:
    mine = _file(A, _entry(B), _entry(C))
    files = {
        B: _file(B, _entry(A)),
        C: _file(C, _entry(X)),
        X: _file(X, _entry(A)),
    }
    links = links_of(A, mine, files, reclaimed={B})
    assert set(links) == {B, C, X}
    assert links[B].state == "reclaimed"
    assert links[C].state == "pending" and links[C].mine and not links[C].theirs
    assert links[X].state == "pending" and not links[X].mine and links[X].theirs
    with pytest.raises(ValueError):
        links_of(A, _file(B, _entry(A)), files)
    with pytest.raises(ValueError):
        links_of(A, mine, {B: _file(C, _entry(A))})
