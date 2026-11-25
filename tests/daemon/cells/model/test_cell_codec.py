"""Cell document codec and the reader-side verification rule."""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.cells.codec import (
    CELL_SCHEMA,
    MAX_DOCUMENT_BYTES,
    decode,
    encode,
    verify as verify_with,
)
from hyprial.daemon.impl.cells.model import (
    Cell,
    CellBinding,
    CellError,
    CellMember,
    CellScope,
)
from hyprial.daemon.impl.cells.permissions import CellPermissions
from hyprial.kernel import keyed_cell_id, mint_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import ALICE

WRITERS = {ALICE: "user:alice"}


def verify(cell: Cell, **fields: object) -> str | None:
    """``codec.verify`` with the local binding ALICE -> user:alice."""

    return verify_with(cell, writer_of=WRITERS.get, **fields)  # type: ignore[arg-type]


def _cell(**overrides: object) -> Cell:
    fields: dict[str, object] = {
        "cell_id": keyed_cell_id("agent", ALICE, "eng"),
        "kind": "agent",
        "authority": ALICE,
        "scope": CellScope.owner(),
        "binding": CellBinding.keyed("eng"),
        "name": "chat",
        "members": (CellMember("agent:alice:mac:a", "writer"),),
        "permissions": CellPermissions.of(read=("@members",)),
        "params": {"delivery.bodyRetentionMs": 3_600_000, "nested": {"l": [1, 2]}},
        "revision": 3,
        "updated_at_ms": 42,
    }
    fields.update(overrides)
    return Cell(**fields)  # type: ignore[arg-type]


def test_round_trip_is_exact_and_canonical() -> None:
    cell = _cell()
    text = encode(cell)
    assert decode(text) == cell
    assert encode(decode(text)) == text
    document = json.loads(text)
    assert document["schema"] == CELL_SCHEMA == "hyprial-cell/v1"
    assert document["params"] == {
        "delivery.bodyRetentionMs": 3_600_000,
        "nested": {"l": [1, 2]},
    }
    assert list(document) == sorted(document)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="hyprial-cell/v2"),
        lambda d: d.pop("cellId"),
        lambda d: d.update(kind="department"),
        lambda d: d.update(revision="3"),
        lambda d: d.update(members="agent:x"),
        lambda d: d.update(params=[1]),
        lambda d: d.update(cellId=mint_cell_id()),
    ],
)
def test_malformed_documents_are_refused(mutate) -> None:
    document = json.loads(encode(_cell()))
    mutate(document)
    with pytest.raises(CellError) as raised:
        decode(json.dumps(document))
    assert raised.value.code in {"invalid-cell", "id-mismatch"}


def test_not_json_and_oversized_documents_are_refused() -> None:
    with pytest.raises(CellError):
        decode("{not json")
    with pytest.raises(CellError):
        decode("[]")
    big = _cell(params={"blob": "x" * MAX_DOCUMENT_BYTES})
    with pytest.raises(CellError) as raised:
        encode(big)
    assert raised.value.code == "too-large"


def test_verify_accepts_only_documents_authored_by_the_authority() -> None:
    cell = _cell()
    assert verify(cell, authors=("user:alice",), space_owner="user:alice") is None
    assert verify(cell, authors=(), space_owner="user:alice") == "author-mismatch"
    assert (
        verify(cell, authors=("user:alice", "user:bob"), space_owner="user:alice")
        == "author-mismatch"
    )


def test_verify_requires_owner_scope_cells_to_live_in_the_authority_home() -> None:
    cell = _cell()
    assert verify(cell, authors=("user:alice",), space_owner="user:bob") == (
        "space-owner-mismatch"
    )
    org_cell = _cell(
        cell_id=keyed_cell_id("agent", ALICE, "eng"),
        kind="agent",
        scope=CellScope.of_org("acme"),
        binding=CellBinding.keyed("eng"),
    )
    assert verify(org_cell, authors=("user:alice",), space_owner="user:bob") is None


def test_verify_refuses_an_authority_without_a_writer_binding() -> None:
    cell = _cell()
    assert (
        verify_with(cell, authors=("user:alice",), space_owner="user:alice", writer_of=lambda _a: None)
        == "authority-unbound"
    )


def test_verify_decides_authority_unbound_before_the_author_check() -> None:
    cell = _cell()
    assert (
        verify_with(cell, authors=("user:mallory",), space_owner="user:bob", writer_of={}.get)
        == "authority-unbound"
    )


def test_verify_compares_authors_and_space_owner_with_the_writer_not_the_authority() -> None:
    cell = _cell()
    assert verify(cell, authors=(ALICE,), space_owner="user:alice") == "author-mismatch"
    assert verify(cell, authors=("user:alice",), space_owner=ALICE) == "space-owner-mismatch"
    assert verify(cell, authors=("user:alice",), space_owner="user:alice") is None


def test_verify_checks_the_path_kind_and_id() -> None:
    cell = _cell()
    assert (
        verify(cell, authors=("user:alice",), space_owner="user:alice", kind="org")
        == "path-mismatch"
    )
    assert (
        verify(
            cell,
            authors=("user:alice",),
            space_owner="user:alice",
            kind="conversation",
            cell_id=mint_cell_id(),
        )
        == "path-mismatch"
    )


def test_verify_binds_org_scope_cells_to_org_spaces_of_their_org() -> None:
    org_cell = _cell(
        cell_id=keyed_cell_id("agent", ALICE, "eng"),
        kind="agent",
        scope=CellScope.of_org("acme"),
        binding=CellBinding.keyed("eng"),
    )
    authored = {"authors": ("user:alice",), "space_owner": "user:bob"}
    assert verify(org_cell, **authored, space_role="org", space_org="acme") is None
    assert (
        verify(org_cell, **authored, space_role="org", space_org="beta")
        == "space-org-mismatch"
    )
    assert (
        verify(org_cell, **authored, space_role="owner-home", space_org=None)
        == "space-role-mismatch"
    )
    any_org = _cell(
        cell_id=keyed_cell_id("agent", ALICE, "eng"),
        kind="agent",
        scope=CellScope.of_org(),
        binding=CellBinding.keyed("eng"),
    )
    assert verify(any_org, **authored, space_role="org", space_org="beta") is None
    assert (
        verify(any_org, **authored, space_role="owner-home", space_org=None)
        == "space-role-mismatch"
    )


def test_verify_keeps_owner_scope_cells_out_of_org_spaces() -> None:
    cell = _cell()
    assert (
        verify(cell, authors=("user:alice",), space_owner="user:alice", space_role="org",
               space_org="acme")
        == "space-role-mismatch"
    )
    assert (
        verify(cell, authors=("user:alice",), space_owner="user:alice",
               space_role="owner-home")
        is None
    )
