"""Offline analysis and guarded compaction of an OrgFS journal.

``--dry-run`` opens the journal with SQLite ``mode=ro`` and reads the
replicated ``.loro`` document files; it never writes.  ``--apply`` is only
allowed while the daemon is stopped, backs up the complete space directory,
creates a full snapshot per document, publishes ``snapshotPoints`` in meta,
deletes old broadcast rows, and verifies every materialized document hash is
unchanged.  A failure restores the backup byte-for-byte.  Older peers need the
0.5.5.2 baseline-snapshot sync extension before they can install the result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    _decode_state_vector,
    _doc_roots,
    _encode_state_vector,
    _unb64,
    state_covers,
)


def _load_documents(space_dir: Path) -> dict[str, Doc]:
    docs: dict[str, Doc] = {}
    meta_path = space_dir / "meta.loro"
    if meta_path.is_file():
        meta = Doc()
        meta.apply_update(meta_path.read_bytes())
        docs["meta"] = meta
        values = _doc_roots(meta).get("docs")
        tree = values.get("tree") if isinstance(values, dict) else None
        active = tree.get("active") if isinstance(tree, dict) else None
        if isinstance(active, str):
            tree_path = space_dir / "tree.loro"
            if tree_path.is_file():
                tree_doc = Doc()
                tree_doc.apply_update(tree_path.read_bytes())
                docs[active] = tree_doc
    for path in sorted((space_dir / "docs").glob("*.loro")):
        try:
            doc_id, update = LocalSpaceStore._read_document_file(path)
        except Exception:
            continue
        doc = Doc()
        doc.apply_update(update)
        docs[doc_id] = doc
    return docs


def analyze(space_dir: Path) -> dict[str, Any]:
    journal = space_dir / "journal.sqlite3"
    if not journal.is_file():
        raise FileNotFoundError(journal)
    db = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        docs = _load_documents(space_dir)
        rows = db.execute(
            "SELECT doc_id, writer, seq, length(envelope_bytes) AS bytes, "
            "version, broadcast_at FROM commits ORDER BY rowid"
        ).fetchall()
    finally:
        db.close()

    per_doc: dict[str, dict[str, Any]] = {
        doc_id: {
            "docId": doc_id,
            "materializedHash": hashlib.sha256(doc.get_update()).hexdigest(),
            "materializedBytes": len(doc.get_update()),
            "reclaimRows": 0,
            "reclaimBytes": 0,
            "retainedRows": 0,
            "missingMaterialized": False,
        }
        for doc_id, doc in docs.items()
    }
    for row in rows:
        doc_id = str(row["doc_id"])
        entry = per_doc.setdefault(
            doc_id,
            {
                "docId": doc_id,
                "materializedHash": None,
                "materializedBytes": 0,
                "reclaimRows": 0,
                "reclaimBytes": 0,
                "retainedRows": 0,
                "missingMaterialized": True,
            },
        )
        doc = docs.get(doc_id)
        # Unbroadcast rows are never compaction candidates.  A row is old only
        # when the materialized document's CRDT state already covers its version.
        reclaim = (
            doc is not None
            and row["broadcast_at"] is not None
            and (
                not row["version"]
                or state_covers(doc.get_state(), bytes(row["version"]))
            )
        )
        key = "reclaim" if reclaim else "retained"
        entry[f"{key}Rows"] += 1
        if reclaim:
            entry["reclaimBytes"] += int(row["bytes"])

    total_rows = sum(item["reclaimRows"] for item in per_doc.values())
    total_bytes = sum(item["reclaimBytes"] for item in per_doc.values())
    return {
        "ok": True,
        "dryRun": True,
        "journalBytes": journal.stat().st_size,
        "commitRows": len(rows),
        "reclaimRows": total_rows,
        "reclaimBytes": total_bytes,
        "protocol": {
            "baselineSnapshotInstallable": False,
            "reason": (
                "0.5.5.1 MeshSync._install_sync_snapshot requires a retirement "
                "record; snapshotPoints alone returns snapshot-barrier"
            ),
        },
        "documents": sorted(per_doc.values(), key=lambda item: item["docId"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("space_dir", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--accept-lagging-peers", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        report = apply_compaction(
            args.space_dir,
            accept_lagging_peers=args.accept_lagging_peers,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            print(
                "WARNING: baseline snapshots are not installable by old peers; "
                "offline nodes cannot catch up and rejected members need re-invite"
            )
            print(
                f"applied: deleted {report['deletedRows']} rows / "
                f"{report['deletedBytes']} bytes; backup={report['backup']}"
            )
            for item in report["documents"]:
                print(f"{item['docId']}: {item['beforeHash']} == {item['afterHash']}")
        return 0
    report = analyze(args.space_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"dry-run: {report['reclaimRows']} rows / {report['reclaimBytes']} bytes "
            f"reclaimable of {report['commitRows']} rows "
            f"({report['journalBytes']} journal bytes)"
        )
        for item in report["documents"]:
            print(
                f"{item['docId']}: {item['reclaimRows']} rows / "
                f"{item['reclaimBytes']} bytes, "
                f"materialized={item['materializedBytes']} bytes, "
                f"missing={item['missingMaterialized']}"
            )
        print("NOT APPLIED: baseline snapshot install is not supported for old peers")
    return 0


def _backup(space_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_root = space_dir.parents[2] / "orgfs" / "compaction-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{space_dir.name}-{stamp}"
    shutil.copytree(space_dir, backup)
    return backup


def _restore(space_dir: Path, backup: Path) -> None:
    if space_dir.exists():
        shutil.rmtree(space_dir)
    shutil.copytree(backup, space_dir)


def apply_compaction(
    space_dir: Path, *, accept_lagging_peers: bool = False
) -> dict[str, Any]:
    """Back up, create baseline snapshots, publish points, delete old rows."""

    if not accept_lagging_peers:
        raise RuntimeError(
            "refusing --apply: pass --accept-lagging-peers after accepting that "
            "offline old peers cannot catch up and wrongly rejected members must "
            "be re-invited"
        )
    state_dir = space_dir.parents[2]
    socket = state_dir / "daemon.sock"
    if socket.exists():
        raise RuntimeError(f"daemon socket exists; stop the daemon first: {socket}")
    before = analyze(space_dir)
    backup = _backup(space_dir)
    store = None
    try:
        store = LocalSpaceStore(state_dir, space_dir.name, node_id="compaction")
        owner = store._owner()
        if owner is None:
            raise RuntimeError("space has no owner; refusing compaction")
        doc_ids = tuple(
            doc_id for doc_id in store.document_ids() if doc_id != "meta"
        )
        manifests = {}
        for doc_id in doc_ids:
            manifests[doc_id] = store.snapshot(doc_id, shallow_since=None)
        plan_id = f"journal-compaction-{int(time.time())}"
        frontier_by_doc = {
            doc_id: _encode_state_vector(
                _decode_state_vector(_unb64(manifest.frontier))
            )
            for doc_id, manifest in manifests.items()
        }

        def publish_points(meta: Doc) -> None:
            points = meta.get("snapshotPoints", type=Map)
            for doc_id, manifest in manifests.items():
                points[doc_id] = Map(
                    {
                        "frontier": manifest.frontier,
                        "snapshotId": manifest.snapshot_id,
                        "planId": plan_id,
                    }
                )

        store.commit("meta", publish_points, author=owner, actor=None)
        deleted = 0
        deleted_bytes = 0
        with store._db:
            for row in store._db.execute(
                "SELECT rowid, doc_id, version, envelope_bytes, broadcast_at "
                "FROM commits ORDER BY rowid"
            ).fetchall():
                doc_id = str(row["doc_id"])
                frontier = frontier_by_doc.get(doc_id)
                if (
                    not frontier
                    or row["broadcast_at"] is None
                    or (
                        row["version"]
                        and not state_covers(frontier, bytes(row["version"]))
                    )
                ):
                    continue
                store._db.execute("DELETE FROM commits WHERE rowid = ?", (row["rowid"],))
                deleted += 1
                deleted_bytes += len(bytes(row["envelope_bytes"]))
        store._persist_all_documents()
        store._db.execute("VACUUM")
        after_hashes = {
            doc_id: hashlib.sha256(store._doc(doc_id).get_update()).hexdigest()
            for doc_id in doc_ids
        }
        before_hashes = {
            item["docId"]: item["materializedHash"]
            for item in before["documents"]
            if item["docId"] in after_hashes
        }
        if before_hashes != after_hashes:
            raise RuntimeError("materialized content changed during compaction")
        store.close()
        store = None
        checked = LocalSpaceStore(state_dir, space_dir.name, node_id="compaction")
        try:
            checked_hashes = {
                doc_id: hashlib.sha256(checked._doc(doc_id).get_update()).hexdigest()
                for doc_id in doc_ids
            }
        finally:
            checked.close()
        if checked_hashes != after_hashes:
            raise RuntimeError("self-check could not reopen the compacted space")
        result = {
            "ok": True,
            "dryRun": False,
            "risk": "lagging-old-peers-cannot-catch-up",
            "backup": str(backup),
            "deletedRows": deleted,
            "deletedBytes": deleted_bytes,
            "documents": [
                {
                    "docId": doc_id,
                    "beforeHash": before_hashes[doc_id],
                    "afterHash": after_hashes[doc_id],
                }
                for doc_id in sorted(after_hashes)
            ],
        }
        return result
    except BaseException:
        try:
            if store is not None:
                store.close()
        finally:
            store = None
            _restore(space_dir, backup)
        raise
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
