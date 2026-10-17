"""Link identity, one link file, mutual-reference judgement, codec (WP1).

Design: docs/design/design-send-links-2026-10-14.md §3, §6; implementation
spec: docs/design/send-links-impl/wp1.md §1.1.  Pure data and pure
functions: no clock, no randomness, no I/O.  ``ports.py`` owns every name
other packages code against; this module only adds the in-memory shapes and
the ``link/<actor>.json`` codec that WP2 stores.

A link exists twice: each endpoint writes its own file.  A reference in one
file is ``pending``; only two mutual references make a link ``established``.
Owner links (agent <-> its own owner) are never written to orgfs (§3.2).
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal

from hyprial.daemon.impl.links.ports import LINK_DIR, LinkState
from hyprial.kernel import (
    canonical_user_uri,
    parse_agent_uri,
    parse_user_uri,
    uri_scheme,
)

__all__ = [
    "LINK_FILE_SCHEMA",
    "Lifecycle",
    "Link",
    "LinkEntry",
    "LinkFile",
    "derive_link",
    "effective",
    "from_document",
    "is_link_endpoint",
    "is_owner_pair",
    "link_file_path",
    "link_key",
    "links_of",
    "owner_of",
    "task_bound_peers",
    "to_document",
    "touch",
    "with_reference",
    "without_reference",
]

#: Lifecycle marker of a link (§3.1): a regular link or one bound to a PAC
#: task.  A PAC-bound link is reclaimed when its task ends.
Lifecycle = Literal["regular", "pac_task"]

#: Version of the ``link/<actor>.json`` document produced by ``to_document``.
LINK_FILE_SCHEMA = 1

_ENTRY_KEYS = frozenset(
    {"peer", "mintedAtMs", "revision", "lifecycle", "taskId", "lastUsedMs"}
)


def _require_int(value: Any, field: str) -> int:
    """Reject bools pretending to be ints: ``true`` is not a revision."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int, got {value!r}")
    return value


def _require_non_negative(value: Any, field: str) -> int:
    checked = _require_int(value, field)
    if checked < 0:
        raise ValueError(f"{field} must be non-negative, got {checked!r}")
    return checked


def is_link_endpoint(uri: str) -> bool:
    """True for a valid ``agent:`` or ``user:`` address that can hold a link."""

    scheme = uri_scheme(uri)
    if scheme == "agent":
        return parse_agent_uri(uri) is not None
    if scheme == "user":
        return parse_user_uri(uri) is not None
    return False


def owner_of(uri: str) -> str | None:
    """The canonical owner ``user:`` URI of an agent URI, else None."""

    parsed = parse_agent_uri(uri)
    if parsed is None:
        return None
    return canonical_user_uri(parsed[0])


def is_owner_pair(a: str, b: str) -> bool:
    """True when the two addresses are an agent and exactly that agent's owner."""

    owner_a = owner_of(a)
    if owner_a is not None and b == owner_a:
        return True
    owner_b = owner_of(b)
    return owner_b is not None and a == owner_b


def link_key(a: str, b: str) -> tuple[str, str]:
    """The unordered-pair identity of a link, in lexicographic order."""

    if a == b:
        raise ValueError("a link needs two distinct endpoints")
    if not is_link_endpoint(a) or not is_link_endpoint(b):
        raise ValueError(f"not a link endpoint pair: {a!r}, {b!r}")
    return (a, b) if a < b else (b, a)


def link_file_path(actor: str) -> str:
    """The orgfs path of ``actor``'s link file (§6)."""

    if not is_link_endpoint(actor):
        raise ValueError(f"not a link endpoint: {actor!r}")
    return f"{LINK_DIR}/{actor}.json"


@dataclass(frozen=True, slots=True)
class LinkEntry:
    """One reference to a peer inside a link file (§3.1).

    ``last_used_ms == 0`` means "never used since minting".  ``revision`` is
    the file revision the entry was written at; ``touch`` deliberately does
    not change it (local runtime fact, no CRDT write amplification).
    """

    peer: str
    minted_at_ms: int
    revision: int
    lifecycle: Lifecycle = "regular"
    task_id: str | None = None
    last_used_ms: int = 0

    def __post_init__(self) -> None:
        if not is_link_endpoint(self.peer):
            raise ValueError(f"link peer is not an endpoint: {self.peer!r}")
        _require_non_negative(self.minted_at_ms, "minted_at_ms")
        _require_non_negative(self.revision, "revision")
        _require_non_negative(self.last_used_ms, "last_used_ms")
        if self.lifecycle not in ("regular", "pac_task"):
            raise ValueError(f"unknown lifecycle: {self.lifecycle!r}")
        if self.task_id is not None and not isinstance(self.task_id, str):
            raise ValueError(f"task_id must be a string or None: {self.task_id!r}")
        bound = self.lifecycle == "pac_task"
        if bound != bool(self.task_id):
            raise ValueError(
                "lifecycle 'pac_task' requires a task_id and 'regular' forbids one"
            )


@dataclass(frozen=True, slots=True)
class LinkFile:
    """``link/<actor>.json`` in memory: the peers this actor references."""

    actor: str
    revision: int
    entries: Mapping[str, LinkEntry]

    def __post_init__(self) -> None:
        if not is_link_endpoint(self.actor):
            raise ValueError(f"link file actor is not an endpoint: {self.actor!r}")
        _require_non_negative(self.revision, "revision")
        if not isinstance(self.entries, Mapping):
            raise ValueError("link file entries must be a mapping")
        frozen: dict[str, LinkEntry] = {}
        for key, entry in self.entries.items():
            if not isinstance(key, str) or not isinstance(entry, LinkEntry):
                raise ValueError("link file entries must map peer -> LinkEntry")
            if key != entry.peer:
                raise ValueError(f"entry key {key!r} does not match peer {entry.peer!r}")
            if entry.peer == self.actor:
                raise ValueError("a link file must not reference itself")
            if is_owner_pair(self.actor, entry.peer):
                raise ValueError("owner links are never written to orgfs")
            frozen[key] = entry
        object.__setattr__(self, "entries", MappingProxyType(frozen))


@dataclass(frozen=True, slots=True)
class Link:
    """One edge derived from "my file + the peer's file" (local view)."""

    actor: str
    peer: str
    state: LinkState
    mine: bool
    theirs: bool
    entry: LinkEntry | None


def derive_link(
    actor: str,
    peer: str,
    mine: LinkFile | None,
    theirs: LinkFile | None,
    *,
    reclaimed: bool = False,
) -> Link | None:
    """Judge one edge from the two local file copies.

    Two mutual references -> ``established``; one side only -> ``pending``;
    neither -> None.  ``reclaimed`` is a local known fact and outranks the
    file copies (they may not have synced the removal yet) whenever at least
    one side still references the peer.
    """

    if mine is not None and mine.actor != actor:
        raise ValueError(f"file {mine.actor!r} is not {actor!r}")
    if theirs is not None and theirs.actor != peer:
        raise ValueError(f"file {theirs.actor!r} is not {peer!r}")
    mine_refs = mine is not None and peer in mine.entries
    theirs_refs = theirs is not None and actor in theirs.entries
    if not mine_refs and not theirs_refs:
        return None
    if reclaimed:
        state: LinkState = "reclaimed"
    elif mine_refs and theirs_refs:
        state = "established"
    else:
        state = "pending"
    entry = mine.entries[peer] if mine_refs and mine is not None else None
    return Link(
        actor=actor,
        peer=peer,
        state=state,
        mine=mine_refs,
        theirs=theirs_refs,
        entry=entry,
    )


def effective(link: Link | None) -> bool:
    """A link is effective only when both files reference each other."""

    return link is not None and link.state == "established"


def links_of(
    actor: str,
    mine: LinkFile | None,
    files: Mapping[str, LinkFile],
    reclaimed: AbstractSet[str] = frozenset(),
) -> Mapping[str, Link]:
    """Every edge touching ``actor`` derivable from the local copies.

    Includes peers that merely reference ``actor`` in their own file (a
    one-sided reference is a real pending edge).  Owner edges are excluded:
    they are not part of the link graph.
    """

    peers: set[str] = set()
    if mine is not None:
        if mine.actor != actor:
            raise ValueError(f"file {mine.actor!r} is not {actor!r}")
        peers.update(mine.entries)
    for key, file in files.items():
        if key != file.actor:
            raise ValueError(f"file under key {key!r} belongs to {file.actor!r}")
        if actor in file.entries:
            peers.add(key)
    derived: dict[str, Link] = {}
    for peer in sorted(peers):
        if peer == actor or is_owner_pair(actor, peer):
            continue
        link = derive_link(
            actor, peer, mine, files.get(peer), reclaimed=peer in reclaimed
        )
        if link is not None:
            derived[peer] = link
    return MappingProxyType(derived)


def with_reference(file: LinkFile, entry: LinkEntry) -> LinkFile:
    """Add or overwrite one reference and bump the revision."""

    if entry.peer == file.actor:
        raise ValueError("a link file must not reference itself")
    if is_owner_pair(file.actor, entry.peer):
        raise ValueError("owner links are never written to orgfs")
    revision = file.revision + 1
    stamped = replace(entry, revision=revision)
    entries = dict(file.entries)
    entries[entry.peer] = stamped
    return LinkFile(actor=file.actor, revision=revision, entries=entries)


def without_reference(file: LinkFile, peer: str) -> LinkFile:
    """Remove one reference (bumping the revision); unknown peers are a no-op."""

    if peer not in file.entries:
        return file
    entries = dict(file.entries)
    del entries[peer]
    return LinkFile(actor=file.actor, revision=file.revision + 1, entries=entries)


def touch(file: LinkFile, peer: str, now_ms: int) -> LinkFile:
    """Advance one entry's ``last_used_ms`` to ``max(old, now_ms)``.

    The file revision deliberately does not move: the last-use moment is
    local runtime state, not an orgfs edit.
    """

    entry = file.entries.get(peer)
    if entry is None:
        return file
    updated = replace(entry, last_used_ms=max(entry.last_used_ms, now_ms))
    if updated == entry:
        return file
    entries = dict(file.entries)
    entries[peer] = updated
    return LinkFile(actor=file.actor, revision=file.revision, entries=entries)


def task_bound_peers(file: LinkFile, task_id: str) -> tuple[str, ...]:
    """The peers bound to one PAC task, lexicographically ordered."""

    return tuple(
        sorted(
            peer
            for peer, entry in file.entries.items()
            if entry.lifecycle == "pac_task" and entry.task_id == task_id
        )
    )


def to_document(file: LinkFile) -> dict[str, Any]:
    """Encode a link file as the stable, sorted JSON document (§6)."""

    return {
        "schema": LINK_FILE_SCHEMA,
        "actor": file.actor,
        "revision": file.revision,
        "peers": [
            {
                "peer": entry.peer,
                "mintedAtMs": entry.minted_at_ms,
                "revision": entry.revision,
                "lifecycle": entry.lifecycle,
                "taskId": entry.task_id,
                "lastUsedMs": entry.last_used_ms,
            }
            for entry in sorted(file.entries.values(), key=lambda item: item.peer)
        ],
    }


def from_document(doc: Mapping[str, Any]) -> LinkFile:
    """Strict inverse of :func:`to_document`; every malformed shape raises."""

    if not isinstance(doc, Mapping):
        raise ValueError("link document must be a mapping")
    if set(doc) != {"schema", "actor", "revision", "peers"}:
        raise ValueError(f"unexpected link document keys: {sorted(doc)!r}")
    schema = doc["schema"]
    if isinstance(schema, bool) or schema != LINK_FILE_SCHEMA:
        raise ValueError(f"unsupported link file schema: {schema!r}")
    actor = doc["actor"]
    if not isinstance(actor, str) or not is_link_endpoint(actor):
        raise ValueError(f"link file actor is not an endpoint: {actor!r}")
    revision = _require_non_negative(doc["revision"], "revision")
    raw_peers = doc["peers"]
    if isinstance(raw_peers, (str, bytes)) or not isinstance(raw_peers, Sequence):
        raise ValueError("link document peers must be a sequence")
    entries: dict[str, LinkEntry] = {}
    for raw in raw_peers:
        if not isinstance(raw, Mapping) or set(raw) != _ENTRY_KEYS:
            raise ValueError(f"malformed link entry: {raw!r}")
        peer = raw["peer"]
        if not isinstance(peer, str):
            raise ValueError(f"link entry peer must be a string: {peer!r}")
        if peer in entries:
            raise ValueError(f"duplicate link entry: {peer!r}")
        entries[peer] = LinkEntry(
            peer=peer,
            minted_at_ms=_require_non_negative(raw["mintedAtMs"], "mintedAtMs"),
            revision=_require_non_negative(raw["revision"], "revision"),
            lifecycle=raw["lifecycle"],
            task_id=raw["taskId"],
            last_used_ms=_require_non_negative(raw["lastUsedMs"], "lastUsedMs"),
        )
    return LinkFile(actor=actor, revision=revision, entries=entries)
