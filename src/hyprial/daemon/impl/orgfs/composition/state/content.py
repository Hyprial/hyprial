# Decision record (phase D, package D2, 2026-11-25):
# - Design §1.1 / §9.1 §6-7: ``status`` returns the facade's
#   ``StateSpaceStatus`` with ``holders_online`` replaced by the runtime's
#   view, as the legacy runtime did: announced holders that are online now
#   (liveliness when this runtime observes it, else the presence predicate),
#   plus the holders whose pull observation is current.  This node is never
#   listed (there is no resident serving role any more).
# - Design §4.2 / §4.7, plan §1.4: content arrives only through the space's
#   ``SpaceSync``.  ``await_content`` on a text document reads it: a read of
#   pending content raises ``content-pending`` and asks the sync for the
#   document (read first), so the wait polls reads until the deadline.  On a
#   blob it drives ``SpaceSync.request_blob`` until the blob store has the
#   digest or the deadline passes, as the legacy loop did with the mesh.
# - ``fetch_blob`` and ``refresh_space`` catch ``SyncError`` where the legacy
#   runtime caught ``StoreError`` (design §8).  ``known_holders`` orders the
#   online announced holders before the merely known ones; per-document
#   holder frontiers are gone (Q3).
# - F4 (real-node finding R2-10): once ``await_content`` sees a blob stored or
#   ``fetch_blob`` stores one, the runtime calls ``facade.blob_arrived`` so
#   watchers get one ``modified`` event when the bytes become readable; a
#   failure to report is logged and does not fail the read.
# - ``writer_attributions`` read the journal; until the device-record source
#   of stage E exists the state runtime answers ``unavailable``.
"""Status, content provisioning and blob fetch of the state runtime."""

from __future__ import annotations

import time

from hyprial.kernel import ORGFS_CONTENT_PENDING

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.composition.vocabulary import ORGFS_AWAIT_RETRY_SECONDS
from hyprial.daemon.impl.orgfs.document.coordinator import StateSpaceStatus
from hyprial.daemon.impl.orgfs.sync import SyncError


class StateContent:
    """Status and content provisioning of ``OrgFsRuntime``."""

    def status(self, space_id: str) -> StateSpaceStatus:
        base = self.facade.status(space_id)
        with self._pending_announces_lock:
            announced = set(self._holders.get(space_id, ()))
            lively = set(self._lively_peers)
        observes_liveliness = self._liveliness_registration is not None
        online = {
            node
            for node in announced | set(base.holders_online)
            if node != self.node_id
            and (node in lively if observes_liveliness else self._supplier_online(node))
        }
        return StateSpaceStatus(
            base.space_id,
            base.synced,
            tuple(sorted(online)),
            base.content_pending,
            base.rejected_updates,
        )

    def writer_attributions(self, space_id: str) -> dict[str, tuple[str, ...]]:
        raise OrgFsError(
            "unavailable",
            {"spaceId": space_id, "message": "writer attributions need the journal"},
        )

    def _online_holders(self, space_id: str) -> tuple[str, ...]:
        return tuple(
            node
            for node in self._holder_snapshot(space_id)
            if node != self.node_id and self._supplier_online(node)
        )

    def refresh_space(self, space_id: str, *, timeout: float) -> bool:
        """Try every online holder once without exceeding the caller's bound."""

        if self._session is None:
            return False
        holders = self._online_holders(space_id)
        if not holders:
            return True
        sync = self._mesh(space_id)
        if sync is None:
            return False
        deadline = time.monotonic() + timeout
        refreshed = True
        for holder in holders:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                sync.sync_from(holder, timeout=remaining, deadline_monotonic=deadline)
            except (SyncError, OrgFsError):
                refreshed = False
            if time.monotonic() > deadline:
                return False
        return refreshed

    def known_holders(
        self,
        space_id: str,
        *,
        doc_id: str | None = None,
        required_frontier: bytes | None = None,
    ) -> tuple[str, ...]:
        online = set(self._online_holders(space_id))
        known = {node for node in self._known_holder_snapshot(space_id) if node != self.node_id}
        return tuple(sorted(online | known, key=lambda node: (node not in online, node)))

    def await_content(self, space_id: str, node_id: str, *, deadline_monotonic: float) -> bool:
        """Provision one node's content through the space's sync until the deadline."""

        if deadline_monotonic - time.monotonic() <= 0:
            return False
        info = self.facade.stat(space_id, f"id:{node_id}")
        if info.kind == "blob":
            if info.content_state == "arrived":
                return True
            if not info.blob_hash:
                return False
            sync = self._mesh(space_id)
            if sync is None:
                return False
            while True:
                event = sync.request_blob(info.blob_hash)
                event.wait(max(0.0, deadline_monotonic - time.monotonic()))
                if self.blobs.contains(info.blob_hash):
                    self._report_blob(space_id, info.blob_hash)
                    return True
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(ORGFS_AWAIT_RETRY_SECONDS, remaining))
        if info.kind != "doc":
            return True
        self._mesh(space_id)
        while True:
            try:
                self.facade.read_text(space_id, f"id:{node_id}")
                return True
            except OrgFsError as exc:
                if exc.code != ORGFS_CONTENT_PENDING:
                    raise
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(ORGFS_AWAIT_RETRY_SECONDS, remaining))

    def _report_blob(self, space_id: str, digest: str) -> None:
        """Let the facade announce a fetched blob's nodes (F4, R2-10)."""

        try:
            self.facade.blob_arrived(space_id, digest)
        except OrgFsError as exc:
            self._log("warn", "orgfs.content.blob-arrival-unreported", spaceId=space_id,
                      digest=digest, code=exc.code)

    def fetch_blob(self, space_id: str, digest: str) -> bytes:
        """Fetch a facade-referenced blob from any currently online holder."""

        candidates = sorted(
            {
                *(node for node in self._holder_snapshot(space_id) if node != self.node_id),
                *(node for node in self._holder_candidates() if node != self.node_id),
            }
        )
        sync = self._mesh(space_id)
        last_error: Exception | None = None
        if sync is not None:
            for holder in candidates:
                if not self._supplier_online(holder):
                    continue
                try:
                    payload = sync.fetch_blob(holder, digest)
                    stored = self.blobs.put(space_id, payload, reason="replica")
                    if stored != digest:
                        raise SyncError(
                            "blob-unavailable", "fetched blob did not match requested digest"
                        )
                    self._report_blob(space_id, digest)
                    return payload
                except SyncError as exc:
                    last_error = exc
        self._log(
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


__all__ = ["StateContent"]
