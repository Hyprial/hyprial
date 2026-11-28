"""Shared helpers for the OrgFS state store tests (design section 2)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pycrdt import Decoder, Doc, Map, Text

from hyprial.daemon.impl.orgfs.state import FileStateStore, MemoryStateStore, StateStore
from hyprial.identity import protected_directory_doc_id

SPACE_ID = "a2b5fb64-de25-4897-aaf1-0e25e2d83e93"
OWNER = "user:bob.smith@example.com"
INVITE_PATH = "directory/invites/" + "0" * 64 + ".json"


def invite_doc_id() -> str:
    """The 253-character protected invite id measured on hq (2026-10-08)."""

    doc_id = protected_directory_doc_id(SPACE_ID, INVITE_PATH, space_owner=OWNER)
    assert doc_id is not None and len(doc_id) == 253
    return doc_id


def content_doc_id() -> str:
    return f"doc-{uuid.uuid4()}"


class Writer:
    """A single-client pycrdt document that hands out incremental updates."""

    def __init__(self, client_id: int) -> None:
        self.doc = Doc(client_id=client_id)
        self.map = self.doc.get("m", type=Map)
        self.text = self.doc.get("t", type=Text)

    def edit(self, mutate: Callable[["Writer"], None]) -> bytes:
        before = self.doc.get_state()
        mutate(self)
        return self.doc.get_update(before)

    def put(self, key: str, value: Any) -> bytes:
        return self.edit(lambda w: w.map.__setitem__(key, value))

    def drop(self, key: str) -> bytes:
        return self.edit(lambda w: w.map.__delitem__(key))

    def replace_text(self, value: str) -> bytes:
        def mutate(w: Writer) -> None:
            w.text.clear()
            w.text += value

        return self.edit(mutate)

    def full(self) -> bytes:
        return self.doc.get_update()


def materialize(update: bytes | None) -> dict[str, Any] | None:
    """Readable value of an update applied to an empty document."""

    if update is None:
        return None
    doc = Doc()
    doc.apply_update(update)
    return {"m": doc.get("m", type=Map).to_py(), "t": str(doc.get("t", type=Text))}


def decode_vector(state: bytes) -> dict[int, int]:
    decoder = Decoder(state)
    count = decoder.read_var_uint()
    vector: dict[int, int] = {}
    for _ in range(count):
        client = decoder.read_var_uint()
        vector[client] = decoder.read_var_uint()
    return vector


def vector_of(update: bytes | None) -> dict[int, int]:
    if update is None:
        return {}
    doc = Doc()
    doc.apply_update(update)
    return decode_vector(doc.get_state())


@dataclass
class StoreFactory:
    """Opens and reopens one space for either implementation."""

    kind: str
    root: Path
    backing: Any = None
    opened: list[StateStore] = field(default_factory=list)

    @property
    def space_dir(self) -> Path:
        return self.root / "spaces" / SPACE_ID

    def open(self, **kwargs: Any) -> StateStore:
        if self.kind == "file":
            store: StateStore = FileStateStore.open(self.space_dir, create=True, **kwargs)
        else:
            if self.backing is None:
                store = MemoryStateStore(SPACE_ID, **kwargs)
                self.backing = store.backing
            else:
                store = MemoryStateStore(SPACE_ID, backing=self.backing, **kwargs)
        self.opened.append(store)
        return store

    def reopen(self, store: StateStore | None = None, **kwargs: Any) -> StateStore:
        if store is not None:
            store.close()
        return self.open(**kwargs)


@pytest.fixture(params=["file", "memory"])
def factory(request: pytest.FixtureRequest, tmp_path: Path) -> StoreFactory:
    return StoreFactory(request.param, tmp_path)


@pytest.fixture
def file_factory(tmp_path: Path) -> StoreFactory:
    return StoreFactory("file", tmp_path)
