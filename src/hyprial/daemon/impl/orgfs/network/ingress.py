from __future__ import annotations


from collections.abc import Iterable, Mapping




import json



from typing import TYPE_CHECKING, Any

import uuid


from hyprial.kernel import AdmissionResult

from hyprial.kernel import EffectCompleted, EffectRequest


from hyprial.daemon.impl.transport import (
    TransportSample)



from hyprial.daemon.impl.orgfs.storage.store  import (
    ORGFS_ENVELOPE_BYTES,
    ImportResult,
    CommitRecord,
    StoreError)

from hyprial.daemon.impl.orgfs.storage.space_authority  import _ReadStore

if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.storage.replica  import ReplicaStore


from hyprial.daemon.impl.orgfs.network.protocol import _LogIngress, _READ_DIRECT, _decode_update_header, _json_bytes

class MeshIngress:
    """Responsibility methods on the sole OrgFsMesh state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _on_log(self, sample: TransportSample) -> None:
        self._process_log_sample(sample)


    @property
    def ingress_dropped(self) -> int:
        return self._ingress_dropped


    @property
    def ingress_pending(self) -> int:
        with self._ingress_guard:
            return len(self._ingress_pending)


    def _release_ingress_credit(self, operation_id: str, generation: int) -> None:
        with self._ingress_guard:
            self._ingress_pending.discard((operation_id, generation))


    def _admit_log_sample(self, sample: TransportSample) -> None:
        with self._ingress_guard:
            if (self._closed or self._ingress_closing
                or len(self._ingress_pending) >= self._ingress_capacity):
                self._ingress_dropped += 1
                return
            command = _LogIngress(
                uuid.uuid4().hex, self._ingress_generation, bytes(sample.payload)
            )
            token = (command.operation_id, command.generation)
            self._ingress_pending.add(token)
            admission = self._ingress_runtime.tell(self._ingress_actor, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._ingress_pending.discard(token)
                self._ingress_dropped += 1


    def _on_ingress_command(self, command: object) -> None:
        if isinstance(command, EffectCompleted):
            if self._ingress_effects.acknowledge(
                command.operation_id, command.generation
            ):
                self._release_ingress_credit(command.operation_id, command.generation)
            self._pump_ingress()
            if command.error is not None:
                self._ingress_dropped += 1
            return
        if not isinstance(command, _LogIngress):
            raise TypeError("orgfs space ingress received an invalid command")
        if command.generation != self._ingress_generation:
            self._ingress_dropped += 1
            self._release_ingress_credit(command.operation_id, command.generation)
            return
        request = EffectRequest(command.operation_id, command.generation, command)
        if not any(item.operation_id == request.operation_id for item in self._ingress_deferred):
            self._ingress_deferred.append(request)
        self._pump_ingress()


    def _pump_ingress(self) -> None:
        while self._ingress_deferred:
            request = self._ingress_deferred[0]
            admission = self._ingress_effects.submit(request)
            if admission is AdmissionResult.OVERLOADED:
                return
            self._ingress_deferred.pop(0)
            if admission is AdmissionResult.CLOSED:
                self._ingress_dropped += 1
                self._release_ingress_credit(request.operation_id, request.generation)


    def _process_log_ingress(self, command: _LogIngress) -> None:
        self._process_log_sample(TransportSample("", command.envelope))


    def _process_log_sample(self, sample: TransportSample) -> None:
        try:
            value = _decode_update_header(sample.payload, space_id=self.store.space_id)
        except StoreError as exc:
            self._log("warn", "orgfs.update.rejected", reason=exc.code, supplier="")
            return
        # TransportSample exposes no Zenoh source identity.  The trusted-tailnet
        # fallback is origin.node, gated by the daemon's same online-host presence
        # judgment used by org.fetch.  Announcements discover holders only.
        supplier = str(value["origin"]["node"])
        if not self._supplier_allowed(supplier):
            self._log(
                "warn",
                "orgfs.update.rejected",
                reason="supplier-offline",
                supplier=supplier,
            )
            return
        self._receive(
            sample.payload,
            supplier=supplier,
            defer_blob_fetch=True,
            projection_attempted=None,
        )


    def broadcast_pending(self, space_id: str | None = None) -> int:
        if space_id is not None and space_id != self.store.space_id:
            return 0
        records = self._read(_ReadStore("unbroadcast"))
        if records is _READ_DIRECT:
            records = self.store.unbroadcast()
        return self.broadcast_records(tuple(records))


    def broadcast_records(self, records: Iterable[CommitRecord]) -> int:
        """Publish the immutable journal records captured at local commit."""

        count = 0
        for record in records:
            value = json.loads(record.envelope_bytes)
            self._record_projected(record.envelope_bytes)
            self._store_replica_envelope(record.envelope_bytes)
            key = self._keys.orgfs_log(
                self.store.space_id,
                str(value["docId"]),
                record.writer,
                record.seq,
            )
            self.session.put(key, record.envelope_bytes)
            if self.space_authority is not None:
                self.space_authority.mark_broadcast(
                    record.writer,
                    record.seq,
                    doc_id=str(value["docId"]),
                )
            else:
                self.store.mark_broadcast(
                    record.writer,
                    record.seq,
                    doc_id=str(value["docId"]),
                )
            count += 1
        return count


    def announce(
        self, *, mode: str = "rw", roles: tuple[str, ...] = ("member",)
    ) -> None:
        spaces = (
            list(self._announcement_source())
            if self._announcement_source is not None
            else [
                {
                    "spaceId": self.store.space_id,
                    "roles": list(roles),
                    "mode": mode,
                }
            ]
        )
        payload = _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-announce",
                "node": self.node_id,
                "durable": self.durable,
                "spaces": spaces,
            }
        )
        if len(payload) > ORGFS_ENVELOPE_BYTES:
            payload = _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-announce",
                    "node": self.node_id,
                    "durable": self.durable,
                    "spaces": [
                        {"spaceId": str(space["spaceId"])}
                        for space in spaces
                        if isinstance(space.get("spaceId"), str)
                    ],
                }
            )
        self.session.put(self._keys.orgfs_announce(self.node_id), payload)


    def attach_replica(self, replica_store: ReplicaStore) -> None:
        """Attach and prime the resident backend from the admitted journal."""

        if replica_store.space_id != self.store.space_id:
            raise ValueError("replica store belongs to another space")
        replica_store.prime_from(self.store, self.blob_store)
        self.replica_store = replica_store
        self.durable = replica_store.durable()
        if self.blob_store is not None:
            for digest in replica_store.pinned_blobs():
                self.blob_store.pin(self.store.space_id, digest, "replica")
        self.announce()


    def _store_replica_envelope(self, envelope: bytes) -> None:
        if self.replica_store is None:
            return
        try:
            value = _decode_update_header(envelope, space_id=self.store.space_id)
            origin = value["origin"]
            key = (
                f"log/{self.store.space_id}/{value['docId']}/"
                f"{origin['writer']}/{value['seq']}"
            )
            self.replica_store.store_envelope(key, envelope)
            digest = value.get("updateBlob")
            if isinstance(digest, str) and self.blob_store is not None:
                self.replica_store.store_blob(
                    digest, self.blob_store.get(self.store.space_id, digest)
                )
                self.blob_store.pin(self.store.space_id, digest, "replica")
        except Exception as exc:
            self._log(
                "error",
                "orgfs.replica.store-failed",
                reason=getattr(exc, "code", "replica-store-failed"),
                detail=str(exc),
            )

            # Replica conflicts are integrity alarms, not subscriber failures.
            # ReplicaStore has already preserved the original bytes and emitted
            # both callbacks.  The admitted member journal remains authoritative,
            # so the subscriber and local broadcast flush must continue.

    def _import_envelope(self, envelope: bytes, *, supplier: str) -> ImportResult:
        if self.space_authority is not None:
            return self.space_authority.import_envelope(envelope, supplier=supplier)
        return self.store.import_envelope(envelope, supplier=supplier)


    def _requester_allowed(self, value: Mapping[str, Any]) -> bool:
        requester = value.get("requester")
        return (
            isinstance(requester, dict)
            and isinstance(requester.get("author"), str)
            and self._member_mode(str(requester["author"])) is not None
        )
