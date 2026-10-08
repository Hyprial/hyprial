from __future__ import annotations

import base64

from dataclasses import replace






import hashlib


import json




import uuid


from typing import Any, Mapping, Sequence

from pycrdt import Map

from hyprial.identity import (
    parse_protected_directory_doc_id,
    protected_directory_doc_id,
)






from hyprial.daemon.impl.orgfs.api  import (
    NodeInfo,
    OrgFsError,
    require_space_owner)


from hyprial.daemon.impl.orgfs.projection.purge  import (
    PurgeBlob,
    PurgeDocument,
    PurgePlan,
    PurgeResult,
    PurgeSnapshot,
    PurgeStatus,
    parse_iso,
    utc_now)

from hyprial.daemon.impl.orgfs.storage.replica  import encode_snapshot_frontier

from hyprial.daemon.impl.orgfs.storage.space_authority  import (
    OrgSpaceAuthority,
    _ReadStore)





from hyprial.daemon.impl.orgfs.document.model import TreeDocument, _BroadcastPending, _ContentDocument, _Space, _facade_locked, _now

_PurgeReplacements = dict[str, tuple[str, bytes, str]]
_PurgeContentMetadata = dict[str, tuple[bytes, int, str]]

class FacadePurge:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _require_purge_owner(self, space: _Space) -> None:
        require_space_owner(self.author, space.info)


    def _export_behind_writer_work(
        self, space: _Space, retired_doc_id: str, plan_id: str
    ) -> NodeInfo | None:
        """Export unpublished retired-doc text as a normal root file for review."""

        store = self._store(space.info.space_id)
        if (
            store is None
            or not hasattr(store, "has_unbroadcast_local_doc")
            or not store.has_unbroadcast_local_doc(retired_doc_id)
            or retired_doc_id not in space.contents
        ):
            return None
        content = space.contents[retired_doc_id].value()
        name = f"purge-conflict-{plan_id[:12] or 'unknown'}.md"
        try:
            existing = self.resolve(space.info.space_id, name)
        except OrgFsError as exc:
            if exc.code != "unknown-doc":
                raise
            existing = ()
        if existing:
            return existing[0]
        return self.write_text(space.info.space_id, name, content)


    def _normalize_purge_targets(
        self, space: _Space, targets: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, str], ...]:
        normalized: list[dict[str, str]] = []
        for target in targets:
            kind = target.get("kind") if isinstance(target, Mapping) else None
            if kind == "blob":
                sha = target.get("sha")
                if not isinstance(sha, str) or len(sha) != 64:
                    raise OrgFsError(
                        "invalid-argument", {"message": "invalid blob sha"}
                    )
                normalized.append({"kind": "blob", "sha": sha})
            elif kind == "tree-range":
                start = target.get("from")
                end = target.get("to")
                if not isinstance(start, str) or not isinstance(end, str):
                    raise OrgFsError(
                        "invalid-argument", {"message": "tree range requires from/to"}
                    )
                normalized.append(
                    {
                        "kind": "tree-range",
                        "docId": space.tree_doc_id,
                        "from": start,
                        "to": end,
                    }
                )
            elif kind == "doc-range":
                node_ref = target.get("node")
                start = target.get("from")
                end = target.get("to")
                if not all(isinstance(value, str) for value in (node_ref, start, end)):
                    raise OrgFsError(
                        "invalid-argument",
                        {"message": "doc range requires node/from/to"},
                    )
                node = self._one(space, str(node_ref), include_deleted=True)
                if node.kind != "doc" or not node.doc_id:
                    raise OrgFsError(
                        "invalid-argument", {"message": "target is not a document"}
                    )
                path = self._path(space, node.node_id)
                try:
                    protected_doc = (
                        parse_protected_directory_doc_id(node.doc_id) is not None
                    )
                except ValueError:
                    protected_doc = node.doc_id.startswith("doc-orgdir-")
                if protected_doc or (
                    protected_directory_doc_id(
                        space.info.space_id,
                        path,
                        space_owner=space.info.owner,
                    )
                    is not None
                ):
                    raise OrgFsError(
                        "protected-path",
                        {
                            "message": f"protected path cannot be purged: {path}",
                            "path": path,
                        },
                    )
                normalized.append(
                    {
                        "kind": "doc-range",
                        "docId": node.doc_id,
                        "nodeId": node.node_id,
                        "from": str(start),
                        "to": str(end),
                    }
                )
            else:
                raise OrgFsError(
                    "invalid-argument", {"message": "unknown purge target"}
                )
        if not normalized:
            raise OrgFsError("invalid-argument", {"message": "purge targets are empty"})
        return tuple(normalized)


    def _build_purge_plan(
        self,
        space: _Space,
        targets: tuple[dict[str, str], ...],
        *,
        expires_at: str | None = None,
    ) -> PurgePlan:
        store = self._store(space.info.space_id)
        if store is None or not hasattr(store, "purge_inventory"):
            raise OrgFsError(
                "invalid-argument", {"message": "purge requires a durable store"}
            )
        authority = self.space_authority(space.info.space_id, store)
        if authority is None:
            raise OrgFsError(
                "invalid-argument", {"message": "purge requires a durable authority"}
            )
        documents: list[PurgeDocument] = []
        snapshots: list[PurgeSnapshot] = []
        writers: set[str] = set()
        authors: dict[str, int] = {}
        blob_hashes = {target["sha"] for target in targets if target["kind"] == "blob"}
        for target in targets:
            if target["kind"] == "blob":
                continue
            inventory = self._thaw_space_value(
                authority.read(_ReadStore("purge_inventory", doc_id=target["docId"]))
            )
            if not isinstance(inventory, dict):
                raise OrgFsError(
                    "invalid-argument", {"message": "invalid purge inventory"}
                )
            documents.append(
                PurgeDocument(
                    target["docId"],
                    target["from"],
                    target["to"],
                    tuple(inventory["updateBlobs"]),
                    tuple(inventory["logKeys"]),
                )
            )
            blob_hashes.update(inventory["updateBlobs"])
            snapshots.extend(
                PurgeSnapshot(item["snapshotId"], item["docId"])
                for item in inventory["snapshots"]
            )
            writers.update(inventory["writersAffected"])
            for author, count in inventory["authorsBefore"].items():
                authors[author] = authors.get(author, 0) + int(count)
        blobs: list[PurgeBlob] = []
        for sha in sorted(blob_hashes):
            elsewhere: tuple[str, ...] = ()
            if self.blobs is not None and hasattr(self.blobs, "referenced_elsewhere"):
                elsewhere = tuple(
                    self.blobs.referenced_elsewhere(space.info.space_id, sha)
                )
            blobs.append(PurgeBlob(sha, elsewhere))
        return PurgePlan.create(
            space_id=space.info.space_id,
            meta_frontier=base64.b64encode(
                authority.read(_ReadStore("frontier", doc_id="meta"))
            ).decode("ascii"),
            docs=sorted(documents, key=lambda item: item.doc_id),
            blobs=blobs,
            snapshots=sorted(
                snapshots, key=lambda item: (item.doc_id, item.snapshot_id)
            ),
            writers_affected=sorted(writers),
            authors_before=authors,
            expires_at=expires_at,
        )


    @_facade_locked
    def purge_plan(
        self, space_id: str, targets: Sequence[Mapping[str, Any]]
    ) -> PurgePlan:
        space = self._space(space_id)
        self._require_purge_owner(space)
        normalized = self._normalize_purge_targets(space, targets)
        plan = self._build_purge_plan(space, normalized)
        store = self._store(space_id)
        authority = self.space_authority(space_id, store)
        if authority is None:
            raise OrgFsError(
                "invalid-argument", {"message": "purge requires a durable authority"}
            )
        with self._lock:
            self._purge_plans[plan.plan_id] = (plan, normalized)
        authority.save_purge_plan(plan.plan_id, plan.storage_dict(), normalized)
        return plan


    def _purge_participants(self, space: _Space) -> tuple[str, ...]:
        roots = space.meta.get("writers", type=Map)
        raw_writers = roots.to_py() if roots is not None else {}
        nodes_by_user: dict[str, set[str]] = {}
        for value in raw_writers.values():
            if not isinstance(value, dict) or not value.get("node"):
                continue
            author = value.get("author")
            if isinstance(author, str):
                nodes_by_user.setdefault(author, set()).add(str(value["node"]))
        if self.author in space.members:
            nodes_by_user.setdefault(self.author, set()).add(self.node_id)
        participants: set[str] = set()
        for user in space.members:
            bound = nodes_by_user.get(user)
            # A user URI in ``pending`` is the no-node-binding reason marker.
            participants.update(bound if bound else (user,))
        return tuple(sorted(participants))


    @_facade_locked
    def purge(self, space_id: str, plan_id: str) -> PurgeResult:
        space = self._space(space_id)
        self._require_purge_owner(space)
        store = self._store(space_id)
        authority = self.space_authority(space_id, store)
        if authority is None:
            raise OrgFsError(
                "invalid-argument", {"message": "purge requires a durable authority"}
            )
        with self._lock:
            saved = self._purge_plans.get(plan_id)
        if saved is None:
            loaded = self._thaw_space_value(
                authority.read(_ReadStore("load_purge_plan", plan_id=plan_id))
            )
            if loaded is not None:
                try:
                    plan = PurgePlan.from_storage_dict(loaded[0])
                except (KeyError, TypeError, ValueError) as exc:
                    raise OrgFsError("stale-plan", {"planId": plan_id}) from exc
                saved = (plan, loaded[1])
                with self._lock:
                    self._purge_plans[plan_id] = saved
        if saved is None:
            raise OrgFsError("stale-plan", {"planId": plan_id})
        plan, targets = saved
        if utc_now() >= parse_iso(plan.expires_at):
            raise OrgFsError("stale-plan", {"planId": plan_id, "reason": "expired"})
        try:
            current = self._build_purge_plan(space, targets, expires_at=plan.expires_at)
        except Exception as exc:
            if isinstance(exc, OrgFsError):
                raise
            raise OrgFsError("stale-plan", {"planId": plan_id}) from exc
        if current.plan_id != plan.plan_id:
            raise OrgFsError("stale-plan", {"planId": plan_id})

        replacements, content_nodes, replacement_metadata = self._purge_phase_prepare_replacements(space, targets)
        self._purge_phase_commit_retirements(space, store, plan, plan_id, replacements)
        self._purge_phase_install_replacements(space, authority, replacements, content_nodes, replacement_metadata)
        space_id = self._purge_phase_publish_frontiers(space, authority, plan_id, replacements, space_id)
        self._purge_phase_delete_retired_objects(authority, replacements, plan, space_id)
        return self._purge_phase_acknowledge(space, store, space_id, plan_id)

    def _purge_phase_prepare_replacements(self, space: _Space, targets: Sequence[Mapping[str, str]]) -> tuple[_PurgeReplacements, dict[str, str], _PurgeContentMetadata]:
        replacements: dict[str, tuple[str, bytes, str]] = {}
        content_nodes: dict[str, str] = {}
        replacement_ids: dict[str, str] = {}
        for target in targets:
            if target["kind"] == "blob":
                continue
            old_doc_id = target["docId"]
            if target["kind"] == "tree-range":
                replacement_ids[old_doc_id] = f"tree-{uuid.uuid4()}"
            else:
                new_doc_id = f"doc-{uuid.uuid4()}"
                replacement_ids[old_doc_id] = new_doc_id
                content_nodes[target["nodeId"]] = new_doc_id
        for target in targets:
            if target["kind"] == "blob":
                continue
            old_doc_id = target["docId"]
            new_doc_id = replacement_ids[old_doc_id]
            if target["kind"] == "tree-range":
                rebuilt = TreeDocument(client_id=space.tree.doc.client_id)
                for node in space.nodes.values():
                    if node.deleted:
                        continue
                    rebuilt.record(
                        replace(
                            node, doc_id=content_nodes.get(node.node_id, node.doc_id)
                        )
                    )
                snapshot_bytes = rebuilt.get_update()
            else:
                rebuilt_content = _ContentDocument(
                    client_id=space.contents[old_doc_id].doc.client_id
                )
                rebuilt_content.set(space.contents[old_doc_id].value())
                snapshot_bytes = rebuilt_content.export()
            replacements[old_doc_id] = (
                new_doc_id,
                snapshot_bytes,
                hashlib.sha256(snapshot_bytes).hexdigest(),
            )

        replacement_metadata: dict[str, tuple[bytes, int, str]] = {}
        for old_doc_id, (new_doc_id, snapshot_bytes, _snapshot_id) in replacements.items():
            if old_doc_id == space.tree_doc_id:
                continue
            document = _ContentDocument(update=snapshot_bytes)
            raw = document.value().encode()
            replacement_metadata[new_doc_id] = (
                document.doc.get_state(),
                len(raw),
                hashlib.sha256(raw).hexdigest(),
            )
        if old_tree_replacement := replacements.get(space.tree_doc_id):
            new_tree_id, _old_snapshot, _old_snapshot_id = old_tree_replacement
            rebuilt_tree = TreeDocument(client_id=space.tree.doc.client_id)
            for node in space.nodes.values():
                if node.deleted:
                    continue
                new_doc_id = content_nodes.get(node.node_id, node.doc_id)
                rebuilt_node = replace(node, doc_id=new_doc_id)
                if new_doc_id in replacement_metadata:
                    frontier, ref_size, ref_sha256 = replacement_metadata[new_doc_id]
                    rebuilt_node = replace(
                        rebuilt_node,
                        required_content_frontier=frontier,
                        ref_size=ref_size,
                        ref_sha256=ref_sha256,
                    )
                rebuilt_tree.record(rebuilt_node)
            tree_snapshot = rebuilt_tree.get_update()
            replacements[space.tree_doc_id] = (
                new_tree_id,
                tree_snapshot,
                hashlib.sha256(tree_snapshot).hexdigest(),
            )
        return replacements, content_nodes, replacement_metadata

    def _purge_phase_commit_retirements(self, space: _Space, store: Any, plan: PurgePlan, plan_id: str, replacements: _PurgeReplacements) -> None:
        retired_at = _now()

        def commit_retirements() -> None:
            plans = space.meta.get("purgePlans", type=Map)
            plans[plan_id] = Map(
                {"expiresAt": plan.expires_at, "createdAt": retired_at}
            )
            retirements = space.meta.get("retirements", type=Map)
            snapshot_points = space.meta.get("snapshotPoints", type=Map)
            docs = space.meta.get("docs", type=Map)
            for old_doc_id, (
                new_doc_id,
                _snapshot,
                snapshot_id,
            ) in replacements.items():
                retirements[old_doc_id] = Map(
                    {
                        "replacementDocId": new_doc_id,
                        "planId": plan_id,
                        "snapshotId": snapshot_id,
                        "retiredAt": retired_at,
                    }
                )
                snapshot_points[new_doc_id] = Map(
                    {
                        "frontier": base64.b64encode(
                            encode_snapshot_frontier({})
                        ).decode("ascii"),
                        "snapshotId": snapshot_id,
                        "planId": plan_id,
                    }
                )
                if old_doc_id == space.tree_doc_id:
                    docs["tree"] = Map(
                        {
                            "active": new_doc_id,
                            "activatedAt": retired_at,
                            "planId": plan_id,
                        }
                    )
            purge_list = space.meta.get("purgeList", type=Map)
            for blob in plan.blobs:
                purge_list[blob.sha] = Map(
                    {"planId": plan_id, "bannedAt": retired_at, "unbannedAt": None}
                )
            audits = space.meta.get("purgeAudit", type=Map)
            audits[plan_id] = Map(
                {
                    "at": retired_at,
                    "nodeId": self.node_id,
                    "docIds": json.dumps(sorted(replacements)),
                    "blobHashes": json.dumps(sorted(blob.sha for blob in plan.blobs)),
                }
            )

        # G2 step 3: make the retirement durable locally, but do not publish it
        # until every replacement snapshot is queryable by a remote member.
        self._commit(space, "meta", commit_retirements, broadcast=False)
        if hasattr(store, "_fault"):
            store._fault("retirement_committed")

    def _purge_phase_install_replacements(self, space: _Space, authority: OrgSpaceAuthority, replacements: _PurgeReplacements, content_nodes: dict[str, str], replacement_metadata: _PurgeContentMetadata) -> None:
        old_tree_id = space.tree_doc_id
        for old_doc_id, (
            new_doc_id,
            snapshot_bytes,
            _snapshot_id,
        ) in replacements.items():
            if authority is None:
                raise OrgFsError(
                    "invalid-argument", {"message": "purge requires a durable authority"}
                )
            authority.install_replacement(
                old_doc_id,
                new_doc_id,
                snapshot_bytes,
                author=self.author,
                actor=self.actor,
            )
            if old_doc_id == old_tree_id:
                space.tree_doc_id = new_doc_id
                space.tree = TreeDocument(update=snapshot_bytes)
                space.nodes = {
                    node_id: node
                    for node_id, node in space.nodes.items()
                    if not node.deleted
                }
            else:
                space.contents[new_doc_id] = _ContentDocument(update=snapshot_bytes)
                space.contents.pop(old_doc_id, None)
        if content_nodes and old_tree_id not in replacements:
            affected = tuple(content_nodes)

            def rewrite_pointers() -> None:
                for node_id, new_doc_id in content_nodes.items():
                    node = space.nodes[node_id]
                    node.doc_id = new_doc_id
                    (
                        node.required_content_frontier,
                        node.ref_size,
                        node.ref_sha256,
                    ) = replacement_metadata[new_doc_id]

            self._write_tree(
                space,
                rewrite_pointers,
                affected,
                "content",
                broadcast=False,
            )
        else:
            for node_id, new_doc_id in content_nodes.items():
                node = space.nodes[node_id]
                node.doc_id = new_doc_id
                (
                    node.required_content_frontier,
                    node.ref_size,
                    node.ref_sha256,
                ) = replacement_metadata[new_doc_id]

    def _purge_phase_publish_frontiers(self, space: _Space, authority: OrgSpaceAuthority, plan_id: str, replacements: _PurgeReplacements, space_id: str) -> str:
        def publish_snapshot_frontiers() -> None:
            snapshot_points = space.meta.get("snapshotPoints", type=Map)
            for _old_doc_id, (
                new_doc_id,
                _snapshot_bytes,
                snapshot_id,
            ) in replacements.items():
                watermarks = (
                    dict(
                        authority.read(
                            _ReadStore("writer_seq_watermarks", doc_id=new_doc_id)
                        )
                    )
                    if authority is not None
                    else {}
                )
                snapshot_points[new_doc_id] = Map(
                    {
                        "frontier": base64.b64encode(
                            encode_snapshot_frontier(watermarks)
                        ).decode("ascii"),
                        "snapshotId": snapshot_id,
                        "planId": plan_id,
                    }
                )

        self._commit(space, "meta", publish_snapshot_frontiers, broadcast=False)
        if self._broadcast_is_enabled():
            space_id = space.info.space_id
            pending = (
                tuple(authority.read(_ReadStore("unbroadcast")))
                if authority is not None
                else ()
            )
            records = self._publication_records(space_id, pending, broadcast=True)
            if records:
                self._defer_effect(_BroadcastPending(space_id, records))
        return space_id

    def _purge_phase_delete_retired_objects(self, authority: OrgSpaceAuthority, replacements: _PurgeReplacements, plan: PurgePlan, space_id: str) -> None:
        for old_doc_id in replacements:
            if authority is None:
                raise OrgFsError(
                    "invalid-argument", {"message": "purge requires a durable authority"}
                )
            authority.delete_retired_objects(old_doc_id)
            if authority.read(_ReadStore("retired_residue", doc_id=old_doc_id)):
                raise OrgFsError(
                    "invalid-argument",
                    {
                        "message": "retired document residue remains",
                        "docId": old_doc_id,
                    },
                )
        if self.blobs is not None:
            for blob in plan.blobs:
                if hasattr(self.blobs, "release"):
                    self.blobs.release(space_id, blob.sha)
                if hasattr(self.blobs, "delete_if_unreferenced"):
                    self.blobs.delete_if_unreferenced(blob.sha)

    def _purge_phase_acknowledge(self, space: _Space, store: Any, space_id: str, plan_id: str) -> PurgeResult:
        if hasattr(store, "_fault"):
            store._fault("before_purge_ack")
        self._forget_history(space)

        def acknowledge() -> None:
            acks = space.meta.get("purgeAcks", type=Map)
            current = (acks.to_py() or {}).get(self.node_id)
            values = {
                key: Map(value)
                for key, value in (current.items() if isinstance(current, dict) else ())
                if isinstance(value, dict)
            }
            values[plan_id] = Map({"ackedAt": _now()})
            acks[self.node_id] = Map(values)

        self._commit(space, "meta", acknowledge)
        status = self.purge_status(space_id, plan_id)
        return PurgeResult(plan_id, True, status.acknowledged, status.pending)


    @_facade_locked
    def purge_status(self, space_id: str, plan_id: str) -> PurgeStatus:
        space = self._space(space_id)
        plans = space.meta.get("purgePlans", type=Map)
        durable_plans = plans.to_py() if plans is not None else {}
        with self._lock:
            has_plan = plan_id in self._purge_plans
        if not has_plan and plan_id not in durable_plans:
            store = self._store(space_id)
            authority = self.space_authority(space_id, store)
            loaded = (
                self._thaw_space_value(
                    authority.read(_ReadStore("load_purge_plan", plan_id=plan_id))
                )
                if authority is not None
                else None
            )
            if loaded is None:
                raise OrgFsError("stale-plan", {"planId": plan_id})
            try:
                plan = PurgePlan.from_storage_dict(loaded[0])
            except (KeyError, TypeError, ValueError) as exc:
                raise OrgFsError("stale-plan", {"planId": plan_id}) from exc
            if plan.plan_id != plan_id:
                raise OrgFsError("stale-plan", {"planId": plan_id})
            with self._lock:
                self._purge_plans[plan_id] = (plan, loaded[1])
        acks = space.meta.get("purgeAcks", type=Map)
        raw = acks.to_py() if acks is not None else {}
        acknowledged = tuple(
            sorted(
                node
                for node, plans in raw.items()
                if isinstance(plans, dict) and plan_id in plans
            )
        )
        pending = tuple(
            node for node in self._purge_participants(space) if node not in acknowledged
        )
        return PurgeStatus(plan_id, acknowledged, pending)


    @_facade_locked
    def unban(self, space_id: str, sha: str) -> None:
        space = self._space(space_id)
        self._require_purge_owner(space)
        if len(sha) != 64 or any(
            character not in "0123456789abcdef" for character in sha
        ):
            raise OrgFsError("invalid-argument", {"sha": sha})

        def operation() -> None:
            purge_list = space.meta.get("purgeList", type=Map)
            current = (purge_list.to_py() or {}).get(sha)
            if not isinstance(current, dict):
                raise OrgFsError("invalid-argument", {"sha": sha})
            purge_list[sha] = Map({**current, "unbannedAt": _now()})

        self._commit(space, "meta", operation)
