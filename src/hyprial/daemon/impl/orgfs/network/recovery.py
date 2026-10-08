from __future__ import annotations


from collections.abc import Callable, Mapping

from concurrent.futures import Future



import json

from threading import Event

import time

from typing import TYPE_CHECKING









from hyprial.daemon.impl.orgfs.storage.store  import (
    ORGFS_ENVELOPE_BYTES,
    ImportResult,
    StoreError)


if TYPE_CHECKING:
    pass


from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_BLOB_FETCH_QUEUE_LIMIT, ORGFS_LOG_RANGE_LIMIT, ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET, ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS, ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS, ORGFS_SYNC_QUEUE_LIMIT, ORGFS_SYNC_RETRY_BACKOFF_SECONDS, _decode_update_header

class MeshRecovery:
    """Responsibility methods on the sole OrgFsMesh state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _initialize_projection_frontiers(self) -> None:
        if self._on_applied is None:
            return
        for doc_id in self._document_ids():
            for writer, seq in self._writer_seq_watermarks(doc_id).items():
                key = (doc_id, writer)
                self._projected_frontiers[key] = seq
                self._projection_scanned[key] = seq


    def _projection_key(self, envelope: bytes) -> tuple[str, str, int]:
        value = _decode_update_header(envelope, space_id=self.store.space_id)
        origin = value["origin"]
        return str(value["docId"]), str(origin["writer"]), int(value["seq"])


    def _record_projected(self, envelope: bytes) -> None:
        if self._on_applied is None:
            return
        doc_id, writer, seq = self._projection_key(envelope)
        key = (doc_id, writer)
        with self._projection_lock:
            self._projection_scanned[key] = max(
                seq, self._projection_scanned.get(key, -1)
            )
            failures = self._projection_failures.get(key)
            if failures is not None:
                failures.pop(seq, None)
                if not failures:
                    self._projection_failures.pop(key, None)
            self._projection_failed.discard((*key, seq))
            if key not in self._projection_failures:
                self._projected_frontiers[key] = max(
                    self._projection_scanned[key],
                    self._projected_frontiers.get(key, -1),
                )
            dirty = self._projection_dirty.get(key)
            if dirty is not None and dirty[0] <= self._projected_frontiers[key]:
                self._projection_dirty.pop(key, None)


    def _queue_projection(self, envelope: bytes, *, supplier: str) -> None:
        doc_id, writer, seq = self._projection_key(envelope)
        key = (doc_id, writer)
        with self._projection_lock:
            if self._projected_frontiers.get(key, -1) >= seq:
                return
            failure_state = self._projection_failures.get(key, {}).get(seq)
            if self._projection_scanned.get(key, -1) >= seq:
                if failure_state is None:
                    return
                if failure_state[0] >= ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET:
                    return
            dirty = self._projection_dirty.get(key)
            if dirty is None or dirty[0] < seq:
                self._projection_dirty[key] = (seq, supplier)


    def _retain_projection_failure(
        self, key: tuple[str, str], seq: int
    ) -> tuple[int, bool, bool]:
        with self._projection_lock:
            failures = self._projection_failures.setdefault(key, {})
            previous, _attempted_at, reset_used = failures.get(
                seq, (0, 0.0, False)
            )
            attempts = min(
                previous + 1, ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET
            )
            failures[seq] = (attempts, self._projection_clock(), reset_used)
        abandoned = (
            previous < ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET
            and attempts == ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET
        )
        return attempts, abandoned, abandoned and reset_used


    def _projection_retry_due(self, attempts: int, attempted_at: float) -> bool:
        if attempts <= 0:
            return True
        if attempts >= ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET:
            return False
        retry_delay = ORGFS_SYNC_RETRY_BACKOFF_SECONDS[attempts - 1]
        return self._projection_clock() - attempted_at >= retry_delay


    def _drop_missing_projection_failure(
        self, key: tuple[str, str], seq: int
    ) -> int:
        with self._projection_lock:
            failures = self._projection_failures.get(key)
            previous = (
                0 if failures is None else failures.get(seq, (0, 0.0, False))[0]
            )
            attempts = min(
                previous + 1, ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET
            )
            if failures is not None:
                failures.pop(seq, None)
                if not failures:
                    self._projection_failures.pop(key, None)
        return attempts


    def _projection_succeeded(
        self, key: tuple[str, str], seq: int
    ) -> tuple[int, ...]:
        with self._projection_lock:
            failures = self._projection_failures.get(key)
            if failures is None:
                reset: tuple[int, ...] = ()
            else:
                failures.pop(seq, None)
                self._projection_failed.discard((*key, seq))
                reset = tuple(
                    failed_seq
                    for failed_seq, (
                        attempts,
                        _attempted_at,
                        reset_used,
                    ) in sorted(
                        failures.items()
                    )
                    if failed_seq < seq
                    and attempts >= ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET
                    and not reset_used
                )
                reset_at = self._projection_clock()
                for failed_seq in reset:
                    failures[failed_seq] = (0, reset_at, True)
                    self._projection_failed.discard((*key, failed_seq))
                if not failures:
                    self._projection_failures.pop(key, None)
            # A predecessor gets one reset epoch in this daemon generation.
            # Once that epoch reaches the attempt budget, later successes
            # leave it finally abandoned; restart rebuilds from the journal.
        return reset


    def _apply_projection_envelope(
        self,
        key: tuple[str, str],
        seq: int,
        envelope: bytes,
        *,
        supplier: str,
    ) -> tuple[Exception | None, tuple[int, ...]]:
        assert self._on_applied is not None
        doc_id, writer = key
        try:
            referenced = self._on_applied(envelope)
        except Exception as exc:  # noqa: BLE001 - isolate one record
            attempt_count, abandoned, final = self._retain_projection_failure(
                key, seq
            )
            if abandoned:
                self._log_projection_abandoned(
                    doc_id, writer, seq, attempt_count, final=final
                )
            return exc, ()
        reset = self._projection_succeeded(key, seq)
        for digest in referenced or ():
            self._defer_replica_blob(str(digest), supplier=supplier)
        return None, reset


    def _retry_failed_projection(
        self,
        key: tuple[str, str],
        seq: int,
        *,
        supplier: str,
        attempted_sequences: set[int],
        force: bool = False,
    ) -> tuple[Exception | None, tuple[int, ...]]:
        doc_id, writer = key
        with self._projection_lock:
            state = self._projection_failures.get(key, {}).get(seq)
        if state is None:
            return None, ()
        attempts, attempted_at, _reset_used = state
        if not force and not self._projection_retry_due(attempts, attempted_at):
            return None, ()
        attempted_sequences.add(seq)
        try:
            failed_envelope = self._failed_projection_record(doc_id, writer, seq)
        except StoreError as exc:
            if exc.code != "projection-behind":
                attempt_count, abandoned, final = self._retain_projection_failure(
                    key, seq
                )
                if abandoned:
                    self._log_projection_abandoned(
                        doc_id, writer, seq, attempt_count, final=final
                    )
                return exc, ()
            attempt_count = self._drop_missing_projection_failure(key, seq)
            self._log_projection_abandoned(
                doc_id, writer, seq, attempt_count, final=True
            )
            return exc, ()
        except Exception as exc:  # noqa: BLE001 - isolate a failed durable read
            attempt_count, abandoned, final = self._retain_projection_failure(
                key, seq
            )
            if abandoned:
                self._log_projection_abandoned(
                    doc_id, writer, seq, attempt_count, final=final
                )
            return exc, ()
        return self._apply_projection_envelope(
            key, seq, failed_envelope, supplier=supplier
        )


    def _failed_projection_record(
        self, doc_id: str, writer: str, seq: int
    ) -> bytes:
        records = self._log_range(
            doc_id,
            writer,
            after=None if seq == 0 else seq - 1,
            limit=1,
        )
        if not records or records[0].seq != seq:
            raise StoreError(
                "projection-behind",
                "durable orgfs log is missing an unprojected envelope",
            )
        return records[0].envelope_bytes


    def _log_projection_abandoned(
        self,
        doc_id: str,
        writer: str,
        seq: int,
        attempts: int,
        *,
        final: bool = False,
    ) -> None:
        # The durable journal remains authoritative.  An abandoned record stays
        # unprojected for this daemon generation; restart rebuilds the facade
        # from the journal before projection frontiers are initialized.
        try:
            fields: dict[str, object] = {
                "docId": doc_id,
                "writer": writer,
                "seq": seq,
                "attempts": attempts,
            }
            if final:
                fields["final"] = True
            self._log(
                "warn",
                "orgfs.projection.abandoned",
                **fields,
            )
        except Exception:
            pass


    def _repair_projections(
        self,
        *,
        attempted: dict[tuple[str, str], tuple[int, set[int]]] | None = None,
    ) -> None:
        if self._on_applied is None:
            return
        attempted = attempted if attempted is not None else {}
        while True:
            with self._projection_lock:
                candidate = next(
                    (
                        (key, dirty)
                        for key, dirty in self._projection_dirty.items()
                        if key not in self._projection_active
                        and (
                            key not in attempted
                            or attempted[key][0] < dirty[0]
                        )
                    ),
                    None,
                )
                if candidate is None:
                    return
                key, (target_seq, supplier) = candidate
                attempted_sequences = attempted.get(key, (target_seq, set()))[1]
                attempted[key] = (target_seq, attempted_sequences)
                doc_id, writer = key
                projected_seq = self._projected_frontiers.get(key, -1)
                if projected_seq >= target_seq:
                    self._projection_dirty.pop(key, None)
                    continue
                self._projection_active.add(key)
            failure: tuple[int, Exception] | None = None
            try:
                with self._projection_lock:
                    failed_records = tuple(
                        sorted(
                            self._projection_failures.get(key, {}).items()
                        )
                    )
                for seq, _state in failed_records:
                    if seq in attempted_sequences:
                        continue
                    record_failure, reset = self._retry_failed_projection(
                        key,
                        seq,
                        supplier=supplier,
                        attempted_sequences=attempted_sequences,
                    )
                    if record_failure is not None:
                        failure = failure or (seq, record_failure)
                    pending_reset = list(reset)
                    while pending_reset:
                        reset_seq = pending_reset.pop(0)
                        reset_failure, nested_reset = self._retry_failed_projection(
                            key,
                            reset_seq,
                            supplier=supplier,
                            attempted_sequences=attempted_sequences,
                            force=True,
                        )
                        if reset_failure is not None:
                            failure = failure or (reset_seq, reset_failure)
                        pending_reset.extend(nested_reset)
                while True:
                    with self._projection_lock:
                        scanned_seq = self._projection_scanned.get(key, -1)
                    if scanned_seq >= target_seq:
                        break
                    records = self._log_range(
                        doc_id,
                        writer,
                        after=None if scanned_seq < 0 else scanned_seq,
                        limit=ORGFS_LOG_RANGE_LIMIT,
                    )
                    if not records:
                        raise StoreError(
                            "projection-behind",
                            "durable orgfs log is missing an unprojected envelope",
                        )
                    advanced = False
                    for record in records:
                        if record.seq <= scanned_seq:
                            continue
                        attempted_sequences.add(record.seq)
                        record_failure, reset = self._apply_projection_envelope(
                            key,
                            record.seq,
                            record.envelope_bytes,
                            supplier=supplier,
                        )
                        if record_failure is not None:
                            failure = failure or (record.seq, record_failure)
                        pending_reset = list(reset)
                        while pending_reset:
                            reset_seq = pending_reset.pop(0)
                            (
                                reset_failure,
                                nested_reset,
                            ) = self._retry_failed_projection(
                                key,
                                reset_seq,
                                supplier=supplier,
                                attempted_sequences=attempted_sequences,
                                force=True,
                            )
                            if reset_failure is not None:
                                failure = failure or (reset_seq, reset_failure)
                            pending_reset.extend(nested_reset)
                        with self._projection_lock:
                            self._projection_scanned[key] = max(
                                record.seq,
                                self._projection_scanned.get(key, -1),
                            )
                        advanced = True
                    if not advanced:
                        raise StoreError(
                            "projection-behind",
                            "durable orgfs log did not advance projection repair",
                        )
            except Exception as exc:  # noqa: BLE001 - isolate each dirty key
                failure = failure or (target_seq, exc)
            except BaseException:
                with self._projection_lock:
                    self._projection_active.discard(key)
                raise
            should_log = False
            with self._projection_lock:
                self._projection_active.discard(key)
                has_failures = bool(self._projection_failures.get(key))
                has_retryable_failures = any(
                    attempts < ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET
                    for attempts, _attempted_at, _reset_used in (
                        self._projection_failures.get(key, {}).values()
                    )
                )
                if failure is not None and has_failures:
                    failure_seq, failure_exc = failure
                    failed_record = (*key, failure_seq)
                    if (
                        failure_seq in self._projection_failures.get(key, {})
                        and failed_record not in self._projection_failed
                    ):
                        self._projection_failed.add(failed_record)
                        should_log = True
                dirty = self._projection_dirty.get(key)
                if (
                    dirty is not None
                    and dirty[0] <= self._projection_scanned.get(key, -1)
                    and not has_retryable_failures
                ):
                    if not has_failures:
                        self._projected_frontiers[key] = max(
                            self._projection_scanned.get(key, -1),
                            self._projected_frontiers.get(key, -1),
                        )
                    self._projection_dirty.pop(key, None)
            if should_log:
                try:
                    self._log(
                        "warn",
                        "orgfs.projection.failed",
                        docId=doc_id,
                        writer=writer,
                        exceptionType=type(failure_exc).__name__,
                    )
                except Exception:
                    pass

    def receive(self, envelope: bytes, *, supplier: str) -> ImportResult:
        """Apply all three inbound gates without turning rejection into a reply."""

        return self._receive(
            envelope,
            supplier=supplier,
            defer_blob_fetch=False,
            projection_attempted=None,
        )


    def _receive(
        self,
        envelope: bytes,
        *,
        supplier: str,
        defer_blob_fetch: bool,
        projection_attempted: (
            dict[tuple[str, str], tuple[int, set[int]]] | None
        ),
    ) -> ImportResult:
        if len(envelope) > ORGFS_ENVELOPE_BYTES:
            return ImportResult("rejected", "too-large")
        try:
            _decode_update_header(envelope, space_id=self.store.space_id)
        except StoreError as exc:
            self._log(
                "warn", "orgfs.update.rejected", reason=exc.code, supplier=supplier
            )
            return ImportResult("rejected", exc.code)
        if not self._supplier_allowed(supplier):
            self._log(
                "warn",
                "orgfs.update.rejected",
                reason="supplier-offline",
                supplier=supplier,
            )
            return ImportResult("rejected", "supplier-offline")
        try:
            result = self._import_envelope(envelope, supplier=supplier)
        except StoreError as exc:
            self._log(
                "warn", "orgfs.update.rejected", reason=exc.code, supplier=supplier
            )
            return ImportResult("rejected", exc.code)
        details = getattr(result, "details", None)
        if (
            result.status == "rejected"
            and result.code == "unknown-blob"
            and isinstance(details, Mapping)
            and isinstance(details.get("digest"), str)
            and self.blob_store is not None
        ):
            digest = str(details["digest"])
            if defer_blob_fetch:
                self._defer_blob_recovery(envelope, supplier=supplier, digest=digest)
                return result
            result = self._recover_blob(
                envelope, supplier=supplier, digest=digest, initial=result
            )
        self._finish_receive(
            envelope,
            supplier=supplier,
            result=result,
            projection_attempted=projection_attempted,
        )
        return result


    def _recover_blob(
        self,
        envelope: bytes,
        *,
        supplier: str,
        digest: str,
        initial: ImportResult,
    ) -> ImportResult:
        assert self.blob_store is not None
        try:
            self._fetch_blob_into_store(supplier, digest)
            # Exactly one retry: a repeated rejection is final.
            return self._import_envelope(envelope, supplier=supplier)
        except Exception as exc:  # noqa: BLE001 - inbound fetch must not kill the worker
            self._log_blob_fetch_failed(
                "orgfs.update.blob-fetch-failed", exc, supplier=supplier, digest=digest
            )
            return initial


    def _fetch_blob_into_store(self, supplier: str, digest: str) -> None:
        assert self.blob_store is not None
        blob = self.fetch_blob(supplier, digest)
        stored_digest = self.blob_store.put(self.store.space_id, blob, reason="replica")
        if stored_digest != digest:
            raise StoreError(
                "blob-unavailable", "fetched blob did not match requested digest"
            )


    def _defer_blob_recovery(
        self, envelope: bytes, *, supplier: str, digest: str
    ) -> None:
        submit = False
        dropped_count: int | None = None
        queue_depth = 0
        with self._worker_lock:
            if self._closed:
                return
            batch = self._blob_fetch_pending.get(digest)
            if batch is None:
                if (
                    self._blob_fetch_pending_count >= ORGFS_BLOB_FETCH_QUEUE_LIMIT
                    or len(self._blob_fetch_pending) >= ORGFS_BLOB_FETCH_QUEUE_LIMIT
                ):
                    self._blob_fetch_dropped += 1
                    dropped_count = self._blob_fetch_dropped
                    queue_depth = self._blob_fetch_pending_count
                else:
                    batch = []
                    self._blob_fetch_pending[digest] = batch
                    submit = True
            elif (
                self._blob_fetch_pending_count >= ORGFS_BLOB_FETCH_QUEUE_LIMIT
                or len(batch) >= ORGFS_BLOB_FETCH_QUEUE_LIMIT
            ):
                self._blob_fetch_dropped += 1
                dropped_count = self._blob_fetch_dropped
                queue_depth = self._blob_fetch_pending_count
            if batch is not None and dropped_count is None:
                batch.append((envelope, supplier))
                self._blob_fetch_pending_count += 1
        if dropped_count is not None:
            self._log(
                "warn",
                "orgfs.update.blob-fetch-dropped",
                reason="queue-full",
                supplier=supplier,
                digest=digest,
                droppedCount=dropped_count,
                queueDepth=queue_depth,
                queueLimit=ORGFS_BLOB_FETCH_QUEUE_LIMIT,
            )
            return
        if not submit:
            return
        if not self._submit_worker(self._run_blob_recovery, digest):
            with self._worker_lock:
                batch = self._blob_fetch_pending.pop(digest, [])
                self._blob_fetch_pending_count -= len(batch)
                waiters = self._blob_fetch_waiters.pop(digest, [])
            for waiter in waiters:
                waiter.set()


    def request_blob(self, digest: str) -> Event:
        """Join one bounded, coalesced fetch; the event is not proof of arrival."""

        completed = Event()
        if self.blob_store is None or self.blob_store.contains(digest):
            completed.set()
            return completed
        future: Future[object] | None = None
        rejected: str | None = None
        with self._worker_lock:
            if self._closed:
                completed.set()
                return completed
            existing = self._blob_fetch_waiters.get(digest)
            if existing:
                return existing[0]
            if (
                len(self._blob_fetch_waiters) >= ORGFS_BLOB_FETCH_QUEUE_LIMIT
                or (
                    digest not in self._blob_fetch_pending
                    and len(self._blob_fetch_pending) >= ORGFS_BLOB_FETCH_QUEUE_LIMIT
                )
            ):
                rejected = "queue-full"
            else:
                self._blob_fetch_waiters[digest] = [completed]
                if digest not in self._blob_fetch_pending:
                    self._blob_fetch_pending[digest] = []
                    try:
                        future = self._worker_executor.submit(
                            self._run_blob_recovery, digest
                        )
                    except RuntimeError:
                        # Admission failed while the lock still excludes
                        # inbound envelope attachment to this new batch.
                        self._blob_fetch_pending.pop(digest, None)
                        self._blob_fetch_waiters.pop(digest, None)
                        rejected = "worker-closed"
                    else:
                        self._worker_futures.add(future)
        if future is not None:
            future.add_done_callback(self._worker_finished)
        if rejected is not None:
            completed.set()
            self._log(
                "warn", "orgfs.content.blob-fetch-dropped",
                reason=rejected, digest=digest,
                queueLimit=ORGFS_BLOB_FETCH_QUEUE_LIMIT,
            )
        return completed


    def _run_blob_recovery(self, digest: str) -> None:
        fetched = False
        attempted: set[str] = set()
        pending: list[tuple[bytes, str]] = []
        waiters: list[Event] = []
        while True:
            try:
                discovered = tuple(self._recovery_candidates())
            except Exception:
                discovered = ()
            with self._worker_lock:
                batch = self._blob_fetch_pending.get(digest, ())
                supplier = next(
                    (
                        candidate
                        for candidate in dict.fromkeys(
                            [item[1] for item in batch] + list(discovered)
                        )
                        if candidate not in attempted
                        and candidate != self.node_id
                        and self._supplier_allowed(candidate)
                    ),
                    None,
                )
                if supplier is None:
                    pending = self._blob_fetch_pending.pop(digest, [])
                    self._blob_fetch_pending_count -= len(pending)
                    waiters = self._blob_fetch_waiters.pop(digest, [])
                    break
            attempted.add(supplier)
            try:
                self._fetch_blob_into_store(supplier, digest)
                fetched = True
            except Exception as exc:  # noqa: BLE001 - keep the serial worker alive
                self._log_blob_fetch_failed(
                    "orgfs.update.blob-fetch-failed",
                    exc,
                    supplier=supplier,
                    digest=digest,
                )
                continue
            with self._worker_lock:
                pending = self._blob_fetch_pending.pop(digest, [])
                self._blob_fetch_pending_count -= len(pending)
                waiters = self._blob_fetch_waiters.pop(digest, [])
            break

        for envelope, supplier in pending:
            initial = ImportResult(
                "rejected", "unknown-blob", details={"digest": digest}
            )
            result = (
                self._import_envelope(envelope, supplier=supplier)
                if fetched
                else initial
            )
            self._finish_receive(
                envelope,
                supplier=supplier,
                result=result,
                projection_attempted=None,
            )
        for waiter in waiters:
            waiter.set()


    def schedule_sync_from(self, supplier: str) -> bool:
        """F6: coalesce one bounded anti-entropy job per live peer."""

        dropped_count: int | None = None
        with self._worker_lock:
            if self._closed:
                return False
            if supplier in self._sync_pending or supplier in self._sync_active:
                rerun_added = supplier not in self._sync_rerun
                self._sync_rerun.add(supplier)
                return rerun_added
            if len(self._sync_pending) >= ORGFS_SYNC_QUEUE_LIMIT:
                self._sync_dropped += 1
                dropped_count = self._sync_dropped
            else:
                self._sync_pending.add(supplier)
        if dropped_count is not None:
            self._log(
                "warn",
                "orgfs.sync.dropped",
                reason="queue-full",
                supplier=supplier,
                droppedCount=dropped_count,
                queueDepth=ORGFS_SYNC_QUEUE_LIMIT,
                queueLimit=ORGFS_SYNC_QUEUE_LIMIT,
            )
            return False
        if not self._submit_worker(self._run_scheduled_sync, supplier):
            with self._worker_lock:
                self._sync_pending.discard(supplier)
            return False
        return True


    def _submit_worker(self, operation: Callable[[str], object], key: str) -> bool:
        with self._worker_lock:
            if self._closed:
                return False
            try:
                future = self._worker_executor.submit(operation, key)
            except RuntimeError:
                return False
            self._worker_futures.add(future)
        future.add_done_callback(self._worker_finished)
        return True


    def _worker_finished(self, future: Future[object]) -> None:
        with self._worker_lock:
            self._worker_futures.discard(future)


    def _run_scheduled_sync(self, supplier: str) -> None:
        def run_once() -> None:
            deadline = time.monotonic() + ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS
            delays = (0.0, *ORGFS_SYNC_RETRY_BACKOFF_SECONDS)
            for attempt, delay in enumerate(delays, start=1):
                if delay:
                    time.sleep(delay)
                with self._worker_lock:
                    if self._closed:
                        return
                if not self._supplier_allowed(supplier):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    self.sync_from(
                        supplier,
                        timeout=min(ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS, remaining),
                        deadline_monotonic=deadline,
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - best-effort lane
                    retry_scheduled = (
                        attempt < len(delays)
                        and self._supplier_allowed(supplier)
                        and time.monotonic() + delays[attempt] < deadline
                    )
                    self._log(
                        "warn",
                        "orgfs.sync.failed",
                        reason=getattr(exc, "code", "sync-failed"),
                        supplier=supplier,
                        attempt=attempt,
                        retryScheduled=retry_scheduled,
                    )
                    if not retry_scheduled:
                        return

        resubmit = False
        try:
            for run in range(2):
                run_once()
                with self._worker_lock:
                    if run == 0 and supplier in self._sync_rerun:
                        self._sync_rerun.discard(supplier)
                        continue
                break
        finally:
            with self._worker_lock:
                self._sync_pending.discard(supplier)
                resubmit = supplier in self._sync_rerun and not self._closed
                self._sync_rerun.discard(supplier)
        if resubmit:
            self.schedule_sync_from(supplier)


    def _defer_replica_blob(self, digest: str, *, supplier: str) -> None:
        """Coalesce one bounded worker job per retained blob digest."""

        replica = self.replica_store
        if replica is None or self.blob_store is None:
            return
        if replica.pinned_reason(digest) is not None:
            return
        dropped_count: int | None = None
        with self._worker_lock:
            if self._closed or digest in self._replica_blob_pending:
                return
            if len(self._replica_blob_pending) >= ORGFS_BLOB_FETCH_QUEUE_LIMIT:
                self._replica_blob_dropped += 1
                dropped_count = self._replica_blob_dropped
            else:
                self._replica_blob_pending[digest] = supplier
        if dropped_count is not None:
            self._log(
                "warn",
                "orgfs.replica.blob-reconcile-dropped",
                reason="queue-full",
                supplier=supplier,
                digest=digest,
                droppedCount=dropped_count,
                queueDepth=ORGFS_BLOB_FETCH_QUEUE_LIMIT,
                queueLimit=ORGFS_BLOB_FETCH_QUEUE_LIMIT,
            )
            return
        if not self._submit_worker(self._run_replica_blob_reconcile, digest):
            with self._worker_lock:
                self._replica_blob_pending.pop(digest, None)


    def _run_replica_blob_reconcile(self, digest: str) -> None:
        with self._worker_lock:
            supplier = self._replica_blob_pending.get(digest)
        try:
            if supplier is None or self.blob_store is None:
                return
            try:
                content = self.blob_store.get(self.store.space_id, digest)
            except Exception:
                content = self.fetch_blob(supplier, digest)
                stored = self.blob_store.put(
                    self.store.space_id, content, reason="replica"
                )
                if stored != digest:
                    raise StoreError(
                        "blob-unavailable",
                        "fetched blob did not match requested digest",
                    )
            replica = self.replica_store
            if replica is not None:
                replica.store_blob(digest, content)
                self.blob_store.pin(self.store.space_id, digest, "replica")
        except Exception as exc:  # noqa: BLE001 - bounded repair is retried by sync
            self._log_blob_fetch_failed(
                "orgfs.replica.blob-reconcile-failed",
                exc,
                supplier=supplier or "",
                digest=digest,
            )
        finally:
            with self._worker_lock:
                self._replica_blob_pending.pop(digest, None)


    def _finish_receive(
        self,
        envelope: bytes,
        *,
        supplier: str,
        result: ImportResult,
        projection_attempted: (
            dict[tuple[str, str], tuple[int, set[int]]] | None
        ) = None,
    ) -> None:
        if result.status == "applied":
            self._store_replica_envelope(envelope)
            drained_for_replica = (result.details or {}).get("drained", ())
            if isinstance(drained_for_replica, (tuple, list)):
                for pending_envelope in drained_for_replica:
                    if isinstance(pending_envelope, bytes):
                        self._store_replica_envelope(pending_envelope)
        if result.status == "applied" and self._on_applied is not None:
            self._queue_projection(envelope, supplier=supplier)
            drained = (result.details or {}).get("drained", ())
            if isinstance(drained, (tuple, list)):
                for pending_envelope in drained:
                    if isinstance(pending_envelope, bytes):
                        self._queue_projection(
                            pending_envelope, supplier=supplier
                        )
            self._repair_projections(attempted=projection_attempted)
        if result.status == "applied" and result.code != "duplicate":
            try:
                value = json.loads(envelope)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = {}
            if value.get("docId") == "meta" and self._pending_replacements():
                self.schedule_sync_from(supplier)
        if result.status == "rejected":
            fields: dict[str, object] = {
                "reason": result.code or "rejected",
                "supplier": supplier,
            }
            try:
                decoded = json.loads(envelope)
                origin = decoded.get("origin", {})
                fields.update(
                    writer=origin.get("writer"),
                    seq=decoded.get("seq"),
                    metaFrontier=origin.get("metaFrontier"),
                )
            except Exception:
                pass
            self._log("warn", "orgfs.update.rejected", **fields)
