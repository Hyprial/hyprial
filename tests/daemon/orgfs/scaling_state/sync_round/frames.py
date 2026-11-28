"""A model of the §4.3 pull request / reply frames, computed from the design alone.

Decisions (C6, 2026-11-25):

* C3's ``SpaceSync`` is not imported.  ``build_request`` and ``build_reply``
  follow the JSON shapes of design §4.3 field by field: ``vectors`` always
  carries ``meta`` and ``tree`` plus at most ``PENDING_LIMIT`` (128) content
  documents; a document whose current vector the requester already covers is
  not answered; documents that do not fit ``maxBytes`` are listed in
  ``omitted``; an update bigger than half the page budget is announced as a
  blob (digest and size) instead of inlined.
* Both sides use the ``StateStore`` (any factory) plus a throwaway ``Doc`` per
  document, as the design says a supplier would.
* The model is only precise enough to measure byte counts; it is not a
  protocol implementation and makes no claim about C3's code.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Mapping, Sequence

from pycrdt import Doc

from ..synthetic import decode_state_vector

PENDING_LIMIT = 128
PAGE_BYTES = 1_048_576


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def frame_bytes(frame: Mapping[str, object]) -> int:
    return len(json.dumps(frame, separators=(",", ":")).encode("utf-8"))


def covers(have: bytes, other: bytes) -> bool:
    mine, theirs = decode_state_vector(have), decode_state_vector(other)
    return all(mine.get(client, 0) >= clock for client, clock in theirs.items())


def doc_from_update(update: bytes | None) -> Doc:
    doc = Doc()
    if update:
        doc.apply_update(update)
    return doc


def build_request(
    space_id: str,
    vectors: Mapping[str, bytes],
    pending: Sequence[str],
    *,
    round_id: str = "00000000-0000-4000-8000-000000000000",
) -> dict[str, object]:
    wanted = {"meta": vectors["meta"], "tree": vectors["tree"]}
    for doc_id in pending[:PENDING_LIMIT]:
        wanted[doc_id] = vectors.get(doc_id, b"\x00")
    return {
        "schemaVersion": 3,
        "type": "sync",
        "spaceId": space_id,
        "round": round_id,
        "requester": {"node": "node-requester", "author": "user:scale"},
        "vectors": {doc_id: b64(vector) for doc_id, vector in wanted.items()},
        "maxBytes": PAGE_BYTES,
    }


def build_reply(
    request: Mapping[str, object],
    supplier_load,
    *,
    supplier: str = "node-supplier",
) -> dict[str, object]:
    """``supplier_load(doc_id) -> bytes | None`` gives the supplier's merged update."""

    requested: Mapping[str, str] = request["vectors"]  # type: ignore[assignment]
    budget = int(request["maxBytes"])  # type: ignore[arg-type]
    updates: list[dict[str, object]] = []
    omitted: list[str] = []
    used = 0
    for doc_id, vector_b64 in requested.items():
        since = base64.b64decode(vector_b64)
        loaded = supplier_load(doc_id)
        if loaded is None:
            continue
        doc = doc_from_update(loaded)
        current = doc.get_state()
        if covers(since, current):
            continue
        update = doc.get_update(since)
        if len(update) > budget // 2:
            digest = hashlib.sha256(update).hexdigest()
            entry = {"docId": doc_id, "blob": digest, "size": len(update), "vector": b64(current)}
        else:
            entry = {"docId": doc_id, "update": b64(update), "vector": b64(current)}
        size = frame_bytes(entry)
        if used + size > budget:
            omitted.append(doc_id)
            continue
        used += size
        updates.append(entry)
    return {
        "schemaVersion": 3,
        "type": "sync-reply",
        "spaceId": request["spaceId"],
        "round": request["round"],
        "supplier": supplier,
        "updates": updates,
        "vectors": {
            doc_id: b64(doc_from_update(supplier_load(doc_id)).get_state())
            for doc_id in ("meta", "tree")
        },
        "omitted": omitted,
    }
