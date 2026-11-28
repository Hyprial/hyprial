# Decision record (phase D, package D2, 2026-11-25):
# - Design §1.1 / §9.1 §6-7: ``status`` returns the facade's
#   ``SpaceStatus`` with ``holders_online`` replaced by the runtime's
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
# - Package F5 (2026-11-25, O report §3-1): ``writer_attributions`` is gone.
#   It only ever answered ``unavailable`` here; ``org.fetch`` resolves node
#   sources from the org directory's device records and has no other caller.
# - Package F6 (real-node finding R3-3): ``holders_online`` lists only nodes
#   that pass the space's node gate (``_node_gate``, the same rule the sync
#   applies to suppliers), so a removed member's node drops out at once even
#   while it stays online and its old pull observation is still held.
# - Package F7 (ruling 5): ``await_content`` on content this node does not
#   hold raises ``not-a-member`` at once while its author is not a current
#   member (a text read raises it from the facade, a blob is checked before
#   any request), instead of waiting for a ``request_blob`` that returns at
#   once and ending in ``content-pending``.  Held content still answers True.
# - Package F10 (R6-1 and F9 review item 7, 2026-11-26): ``await_text`` reads
#   first and only waits via ``await_content`` when the error is
#   ``content-pending`` and ``timeout > 0`` (non-pending errors and non-positive
#   timeouts re-raise immediately without ``stat`` or waiting).  ``await_content``
#   checks runtime closing/closed and space sync detachment before and during
#   waits (sleeping on ``_closed_event`` without holding OrgFS locks) and raises
#   ``unavailable`` with ``orgfs-closing`` or ``sync-detached`` immediately.
"""Status, content provisioning and blob fetch of the state runtime."""

from __future__ import annotations

import time

from hyprial.kernel import ORGFS_CONTENT_PENDING

from hyprial.daemon.impl.orgfs.api import OrgFsError, SpaceStatus
from hyprial.daemon.impl.orgfs.composition.vocabulary import ORGFS_AWAIT_RETRY_SECONDS
from hyprial.daemon.impl.orgfs.sync import SyncError


class StateContent:
    """Status and content provisioning of ``OrgFsRuntime``."""

    def status(self, space_id: str) -> SpaceStatus:
        base = self.facade.status(space_id)
        with self._pending_announces_lock:
            announced = set(self._holders.get(space_id, ()))
            lively = set(self._lively_peers)
        observes_liveliness = self._liveliness_registration is not None
        gate = self._node_gate(space_id)
        online = {
            node
            for node in announced | set(base.holders_online)
            if node != self.node_id
            and (node in lively if observes_liveliness else self._supplier_online(node))
            and gate(node)
        }
        return SpaceStatus(
            base.space_id,
            base.synced,
            tuple(sorted(online)),
            base.content_pending,
            base.rejected_updates,
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

    def _check_await_available(
        self, space_id: str, *, require_facade_sync: bool = False
    ) -> None:
        with self._resource_lock:
            closing = self._closing or self._closed
        if closing:
            raise OrgFsError(
                "unavailable",
                {
                    "spaceId": space_id,
                    "reason": "orgfs-closing",
                    "message": "orgfs is closing",
                },
            )
        if require_facade_sync and not self.facade.has_sync(space_id):
            raise OrgFsError(
                "unavailable",
                {
                    "spaceId": space_id,
                    "reason": "sync-detached",
                    "message": "space sync was detached",
                },
            )

    def _wait_retry_interval(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._closed_event.wait(seconds)

    def await_text(
        self, space_id: str, ref: str, *, timeout: float
    ) -> tuple[str, str]:
        """Read a text document, waiting up to ``timeout`` seconds when pending."""

        try:
            return self.facade.read_text(space_id, ref)
        except OrgFsError as exc:
            if exc.code != ORGFS_CONTENT_PENDING or timeout <= 0:
                raise
            info = self.facade.stat(space_id, ref)
            if info.kind != "doc":
                raise
            deadline = time.monotonic() + timeout
            if not self.await_content(space_id, info.node_id, deadline_monotonic=deadline):
                raise
        return self.facade.read_text(space_id, ref)

    def await_content(self, space_id: str, node_id: str, *, deadline_monotonic: float) -> bool:
        """Provision one node's content through the space's sync until the deadline."""

        self._check_await_available(space_id)
        if deadline_monotonic - time.monotonic() <= 0:
            return False
        info = self.facade.stat(space_id, f"id:{node_id}")
        if info.kind == "blob":
            if info.content_state == "arrived":
                return True
            if not info.blob_hash:
                return False
            self._require_local_member(space_id, node_id)
            sync = self._mesh(space_id)
            if sync is None:
                self._check_await_available(space_id)
                return False
            had_facade_sync = self.facade.has_sync(space_id)
            while True:
                self._check_await_available(space_id, require_facade_sync=had_facade_sync)
                event = sync.request_blob(info.blob_hash)
                while not event.is_set():
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        break
                    if event.wait(min(ORGFS_AWAIT_RETRY_SECONDS, remaining)):
                        break
                    self._check_await_available(space_id, require_facade_sync=had_facade_sync)
                if self.blobs.contains(info.blob_hash):
                    self._report_blob(space_id, info.blob_hash)
                    return True
                self._check_await_available(space_id, require_facade_sync=had_facade_sync)
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    return False
                self._wait_retry_interval(min(ORGFS_AWAIT_RETRY_SECONDS, remaining))
        if info.kind != "doc":
            return True
        sync = self._mesh(space_id)
        if sync is None:
            self._check_await_available(space_id)
        require_sync = sync is not None
        while True:
            try:
                self.facade.read_text(space_id, f"id:{node_id}")
                return True
            except OrgFsError as exc:
                if exc.code != ORGFS_CONTENT_PENDING:
                    raise
            self._check_await_available(space_id, require_facade_sync=require_sync)
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                return False
            self._wait_retry_interval(min(ORGFS_AWAIT_RETRY_SECONDS, remaining))
            self._check_await_available(space_id, require_facade_sync=require_sync)

    def _require_local_member(self, space_id: str, node_id: str) -> None:
        """Refuse provisioning content for a space this node's author left (F7 ruling 5)."""

        if not self.facade.sync_host(space_id).local_member():
            raise OrgFsError("not-a-member", {
                "spaceId": space_id,
                "node": f"id:{node_id}",
                "message": "this node's author is not a current member; content it does not hold is not served",
            })

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
