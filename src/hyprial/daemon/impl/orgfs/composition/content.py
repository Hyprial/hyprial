from __future__ import annotations







import time








from hyprial.daemon.impl.orgfs.api  import OrgFsError, SpaceStatus







from hyprial.daemon.impl.orgfs.storage.space_authority  import _ReadStore

from hyprial.daemon.impl.orgfs.storage.store  import StoreError, state_covers


from hyprial.daemon.impl.orgfs.composition.vocabulary import ORGFS_AWAIT_RETRY_SECONDS

class RuntimeContent:
    """Responsibility methods on the sole OrgFsRuntime state host.

    This class never constructs, copies, or persists an independent host.
    """

    def status(self, space_id: str) -> SpaceStatus:
        """Live holder view: durable probe history plus current presence.

        ``holders_online`` is the sorted, de-duplicated union of this node
        when it currently serves the space and the announced holders that
        are online now per the runtime's liveliness and presence view.
        ``durable_holders_seen`` keeps its probe-recorded meaning.
        """

        base = self.facade.status(space_id)
        with self._pending_announces_lock:
            announced = set(self._holders.get(space_id, ()))
            lively = set(self._lively_peers)
        # When this runtime observes host liveliness, presence is exactly that
        # view: an announce applied after a peer's liveliness delete must not
        # make it look online again.  The supplier predicate is only the
        # fallback for sessions without a liveliness view.
        observes_liveliness = self._liveliness_registration is not None
        online = {
            node
            for node in announced
            if node != self.node_id
            and (
                node in lively
                if observes_liveliness
                else self._supplier_online(node)
            )
        }
        with self._resource_lock:
            serving_locally = space_id in self._replicas
        if serving_locally:
            online.add(self.node_id)
        return SpaceStatus(
            base.space_id,
            base.unconfirmed_commits,
            base.durable_holders_seen,
            tuple(sorted(online)),
            base.parked_commits,
        )


    def writer_attributions(self, space_id: str) -> dict[str, tuple[str, ...]]:
        """Return the space's replicated node -> author attribution table."""

        if space_id not in {info.space_id for info in self.facade.spaces()}:
            raise OrgFsError("unknown-space", {"spaceId": space_id})
        store = self.stores.get(space_id)
        authority = self.facade.space_authority(space_id, store)
        if authority is None:
            raise OrgFsError("unavailable", {"spaceId": space_id})
        return dict(authority.read(_ReadStore("writer_attributions")))


    def refresh_space(self, space_id: str, *, timeout: float) -> bool:
        """Try every online holder once without exceeding the caller's bound."""

        if self._session is None:
            return False
        holders = tuple(
            node
            for node in self._holder_snapshot(space_id)
            if node != self.node_id and self._supplier_online(node)
        )
        if not holders:
            return True
        mesh = self._mesh(space_id)
        if mesh is None:
            return False
        deadline = time.monotonic() + timeout
        refreshed = True
        for holder in holders:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                mesh.sync_from(
                    holder,
                    timeout=remaining,
                    deadline_monotonic=deadline,
                )
            except (StoreError, OrgFsError):
                refreshed = False
            if time.monotonic() > deadline:
                refreshed = False
                break
        return refreshed


    def known_holders(
        self,
        space_id: str,
        *,
        doc_id: str | None = None,
        required_frontier: bytes | None = None,
    ) -> tuple[str, ...]:
        """Return in-memory holder hints, preferring proven doc coverage."""

        online = {
            node
            for node in self._holder_snapshot(space_id)
            if node != self.node_id and self._supplier_online(node)
        }
        known = {
            node
            for node in self._known_holder_snapshot(space_id)
            if node != self.node_id
        }
        candidates = online | known
        store = self.stores.get(space_id)
        authority = self.facade.space_authority(space_id, store)
        assert authority is not None
        frontiers = {
            holder: dict(docs)
            for holder, docs in authority.read(_ReadStore("holder_frontiers"))
        }

        def key(node: str) -> tuple[bool, bool, str]:
            covers = False
            if doc_id is not None and required_frontier is not None:
                actual = frontiers.get(node, {}).get(doc_id)
                covers = actual is not None and state_covers(
                    actual, required_frontier
                )
            return (not covers, node not in online, node)

        return tuple(sorted(candidates, key=key))


    def await_content(
        self, space_id: str, node_id: str, *, deadline_monotonic: float
    ) -> bool:
        """Actively provision one node's content without holding the facade lock."""

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return False
        info = self.facade.stat(space_id, f"id:{node_id}")
        if info.kind == "blob":
            if info.content_state == "arrived":
                return True
            if not info.blob_hash:
                return False
            mesh = self._mesh(space_id)
            if mesh is None:
                return False
            # request_blob's event only means "this attempt ended"; a holder
            # that is not routable yet makes an attempt end without the blob.
            # Keep provisioning until the caller's deadline, with a short
            # backoff so an already-set event cannot spin.
            while True:
                event = mesh.request_blob(info.blob_hash)
                event.wait(max(0.0, deadline_monotonic - time.monotonic()))
                if self.blobs.contains(info.blob_hash):
                    return True
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(ORGFS_AWAIT_RETRY_SECONDS, remaining))
        if info.kind == "doc":
            if info.content_state in {"arrived", "unverifiable"}:
                return True
            self.refresh_space(
                space_id,
                timeout=max(0.0, deadline_monotonic - time.monotonic()),
            )
            refreshed = self.facade.stat(space_id, f"id:{node_id}")
            if refreshed.doc_id:
                store = self.stores.get(space_id)
                authority = self.facade.space_authority(space_id, store)
                assert authority is not None
                snapshot = authority.read(
                    _ReadStore("snapshot", doc_id=refreshed.doc_id)
                )
                self.facade.hydrate_content_snapshot(
                    space_id, f"id:{node_id}", snapshot.snapshot_bytes,
                    expected_doc_id=snapshot.doc_id,
                )
            return (
                self.facade.stat(space_id, f"id:{node_id}").content_state
                == "arrived"
            )
        return True


    def fetch_blob(self, space_id: str, digest: str) -> bytes:
        """Fetch a facade-referenced blob from any currently online holder."""

        candidates = sorted(
            {
                *(
                    node
                    for node in self._holder_snapshot(space_id)
                    if node != self.node_id
                ),
                *(node for node in self._holder_candidates() if node != self.node_id),
            }
        )
        mesh = self._mesh(space_id)
        last_error: Exception | None = None
        if mesh is not None:
            for holder in candidates:
                if not self._supplier_online(holder):
                    continue
                try:
                    payload = mesh.fetch_blob(holder, digest)
                    stored = self.blobs.put(space_id, payload, reason="replica")
                    if stored != digest:
                        raise StoreError(
                            "blob-unavailable",
                            "fetched blob did not match requested digest",
                        )
                    with self._resource_lock:
                        replica = self._replicas.get(space_id)
                    if replica is not None:
                        replica.store_blob(digest, payload)
                        self.blobs.pin(space_id, digest, "replica")
                    return payload
                except StoreError as exc:
                    last_error = exc
        if self.logger is not None:
            self.logger(
                "warn",
                "orgfs.content.blob-fetch-failed",
                reason=getattr(last_error, "code", "no-holder-online"),
                spaceId=space_id,
                digest=digest,
                candidates=candidates,
                detail=str(last_error) if last_error is not None else "",
            )
        details: dict[str, object] = {
            "digest": digest,
            "lastKnownHolders": list(self._known_holder_snapshot(space_id)),
        }
        if last_error is not None:
            details["message"] = str(last_error)
        raise OrgFsError("blob-unavailable", details)
