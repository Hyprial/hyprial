"""One pull round's frames cost what changed, not what exists (design §4.3).

2026-11-25 (C6): the frames come from ``frames.py``, a model of §4.3 that does
not use C3's code.  With nothing changed, request and reply carry only the
``meta`` and ``tree`` vectors, so their byte counts must be identical for
N = 100 / 1000 / 10000 content documents and for a tree with 1 or 2000 edits
of history.  A round that has one changed document costs that document's
update.  A requester that is behind on P documents asks for at most 128 per
round, so request size is bounded and P pending documents take ceil(P / 128)
rounds with strictly falling pending counts.
"""

from __future__ import annotations

import base64
from pathlib import Path

from pycrdt import Doc

from ..conftest import space_path, StoreFactory, build_space
from ..reference_store import Batch
from ..synthetic import decode_state_vector, edit_tree_row, synthetic_tree_doc
from .frames import PAGE_BYTES, PENDING_LIMIT, build_reply, build_request, doc_from_update, frame_bytes


VARINT_SLACK = 16


def _spread(observed) -> int:
    """Largest byte difference between rounds; clocks are varints, so it may grow by a byte or two."""

    return max(max(column) - min(column) for column in zip(*observed))


def _vectors(store, names=("meta", "tree")) -> dict[str, bytes]:
    return {name: doc_from_update(store.load(name)).get_state() for name in names}


def _round(supplier, requester, pending=()) -> tuple[dict, dict]:
    request = build_request("space", _vectors(requester), list(pending))
    return request, build_reply(request, supplier.load)


def _cloned(factory: StoreFactory, source: Path, target: Path):
    build_space(factory, target, 0)
    store = factory(target)
    for name in ("meta", "tree"):
        store.commit(Batch(appends=((name, factory(source).load(name)),)))
    return store


def test_a_round_with_no_change_costs_the_same_bytes_at_every_size(
    state_store_factory, sizes, tmp_path
) -> None:
    observed = []
    for count in sizes:
        supplier_dir = space_path(tmp_path, f"supplier{count}")
        build_space(state_store_factory, supplier_dir, count, tree_rows=count)
        supplier = state_store_factory(supplier_dir)
        requester = _cloned(state_store_factory, supplier_dir, space_path(tmp_path, f"requester{count}"))
        request, reply = _round(supplier, requester)
        assert reply["updates"] == [] and reply["omitted"] == []
        assert sorted(request["vectors"]) == ["meta", "tree"]
        observed.append((frame_bytes(request), frame_bytes(reply)))
    assert _spread(observed) <= VARINT_SLACK, observed
    assert max(max(pair) for pair in observed) < 2048, observed


def test_a_round_with_no_change_costs_the_same_bytes_whatever_the_history(
    state_store_factory, tmp_path
) -> None:
    observed = []
    for edits in (1, 2_000):
        supplier_dir = space_path(tmp_path, f"supplier{edits}")
        build_space(state_store_factory, supplier_dir, 10)
        supplier = state_store_factory(supplier_dir)
        tree = doc_from_update(supplier.load("tree"))
        for index in range(edits):
            supplier.commit(Batch(appends=(("tree", edit_tree_row(tree, "node-00000001", f"name-{index}.md")),)))
        supplier.compact("tree", tree.get_update())
        requester = _cloned(state_store_factory, supplier_dir, space_path(tmp_path, f"requester{edits}"))
        request, reply = _round(supplier, requester)
        assert reply["updates"] == []
        observed.append((frame_bytes(request), frame_bytes(reply)))
    assert _spread(observed) <= VARINT_SLACK, observed


def test_a_round_with_one_changed_document_costs_that_documents_update(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        supplier_dir = space_path(tmp_path, f"supplier{count}")
        build_space(state_store_factory, supplier_dir, count)
        supplier = state_store_factory(supplier_dir)
        requester = _cloned(state_store_factory, supplier_dir, space_path(tmp_path, f"requester{count}"))
        tree = doc_from_update(supplier.load("tree"))
        update = edit_tree_row(tree, "node-00000001", "changed.md")
        supplier.commit(Batch(appends=(("tree", update),)))
        request, reply = _round(supplier, requester)
        assert [entry["docId"] for entry in reply["updates"]] == ["tree"]
        costs.append((frame_bytes(request), frame_bytes(reply), len(update)))
    assert _spread([cost[:2] for cost in costs]) <= VARINT_SLACK, costs
    assert costs[0][1] < costs[0][2] * 2 + 2048, costs


def test_a_requester_far_behind_asks_for_at_most_128_documents_per_round(
    state_store_factory, tmp_path
) -> None:
    behind = 700
    supplier_dir = space_path(tmp_path, "supplier")
    ids = build_space(state_store_factory, supplier_dir, behind)
    supplier = state_store_factory(supplier_dir)
    requester = _cloned(state_store_factory, supplier_dir, space_path(tmp_path, "requester"))
    pending = list(ids)
    rounds = []
    while pending:
        request, reply = _round(supplier, requester, pending)
        asked = [name for name in request["vectors"] if name not in ("meta", "tree")]
        assert len(asked) == min(PENDING_LIMIT, len(pending))
        delivered = {entry["docId"] for entry in reply["updates"] if entry["docId"] in asked}
        assert delivered == set(asked), (len(delivered), len(asked))
        for entry in reply["updates"]:
            if "update" in entry:
                requester.commit(Batch(appends=((entry["docId"], base64.b64decode(entry["update"])),)))
        rounds.append((frame_bytes(request), frame_bytes(reply), len(pending)))
        pending = [name for name in pending if name not in delivered]
        assert frame_bytes(reply) <= PAGE_BYTES
    assert len(rounds) == -(-behind // PENDING_LIMIT), rounds
    assert [r[2] for r in rounds] == sorted((r[2] for r in rounds), reverse=True)
    assert max(r[0] for r in rounds) < 128 * 120 + 4096, rounds


def test_a_page_budget_smaller_than_the_answers_lists_the_rest_as_omitted(
    state_store_factory, tmp_path
) -> None:
    supplier_dir = space_path(tmp_path, "supplier")
    ids = build_space(state_store_factory, supplier_dir, 60)
    supplier = state_store_factory(supplier_dir)
    requester = _cloned(state_store_factory, supplier_dir, space_path(tmp_path, "requester"))
    request = build_request("space", _vectors(requester), ids)
    request["maxBytes"] = 4_000

    reply = build_reply(request, supplier.load)

    assert reply["omitted"], reply["omitted"]
    assert frame_bytes({"updates": reply["updates"]}) <= 4_000
    delivered = {entry["docId"] for entry in reply["updates"]}
    assert delivered.isdisjoint(reply["omitted"])
    assert delivered | set(reply["omitted"]) == set(ids)


def test_state_vector_bytes_grow_with_clients_not_with_documents_or_history(record_property) -> None:
    tree = synthetic_tree_doc(2_000, client_id=21)
    one_client = len(tree.get_state())
    for index in range(1, 41):
        other = Doc(client_id=21 + index)
        other.apply_update(tree.get_update())
        before = other.get_state()
        edit_tree_row(other, "node-00000001", f"by-client-{index}")
        tree.apply_update(other.get_update(before))
    forty_one_clients = len(tree.get_state())
    record_property("state_vector_bytes_1_client_41_clients", (one_client, forty_one_clients))
    print("tree state vector bytes: 1 client", one_client, "41 clients", forty_one_clients)
    assert len(decode_state_vector(tree.get_state())) == 41
    assert one_client < 16 and forty_one_clients < 41 * 16
