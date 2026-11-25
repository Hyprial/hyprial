"""The parked-update queue (``pending``) of one OrgFS space.

Extracted from :mod:`hyprial.daemon.impl.orgfs.storage.store.admission` so the
admission decision and the durable hold/drain machinery can change
independently: the protected-organization paths keep growing in ``admission``
while this module owns the hold bounds, the age re-validation and the drain
contract that keeps an aged row from being dropped silently.

The methods are responsibility methods on the single ``LocalSpaceStore``
state host -- they read its database, lock, pending limits and counters and
never construct, copy or persist a second host.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from hyprial.identity import org_from_space_name

from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    StoreError,
    _LOG,
    _MembershipBudgetExceeded,
    _doc_roots,
    _now,
    _unb64,
)


class StorePending:
    """Pending-hold responsibility methods on the store state host."""

    def _pending_org(self) -> str:
        meta = self._docs.get("meta")
        if meta is None:
            meta = self._doc("meta")
        space = _doc_roots(meta).get("space")
        name = space.get("name") if isinstance(space, dict) else None
        if not isinstance(name, str):
            return "-"
        return org_from_space_name(name) or "-"

    def _expired_pending_page(
        self, cutoff: str, *, after_rowid: int
    ) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT rowid AS pending_rowid, * FROM pending "
            "WHERE (held_at IS NULL OR held_at < ?) AND rowid > ? "
            "ORDER BY rowid LIMIT ?",
            (cutoff, after_rowid, self._pending_drain_page_size),
        ).fetchall()

    def _expire_pending_locked(self, applied: list[bytes], deadline: float | None) -> int:
        age = timedelta(seconds=self._pending_import_max_age_seconds)
        cutoff = (datetime.now(UTC) - age).isoformat()
        summary = self._db.execute(
            "SELECT COUNT(*), MIN(held_at), "
            "SUM(CASE WHEN held_at IS NULL THEN 1 ELSE 0 END) FROM pending "
            "WHERE held_at IS NULL OR held_at < ?",
            (cutoff,),
        ).fetchone()
        count = int(summary[0])
        if not count:
            return 0
        oldest = "missing" if int(summary[2]) else str(summary[1])
        applied_before = len(applied)
        while True:
            if self._drain_budget_exceeded(deadline):
                return count
            progressed = False
            after_rowid = 0
            while True:
                rows = self._expired_pending_page(cutoff, after_rowid=after_rowid)
                if not rows:
                    break
                for row in rows:
                    if self._drain_budget_exceeded(deadline):
                        return count
                    after_rowid = int(row["pending_rowid"])
                    progressed = self._drain_pending_row(row, applied) or progressed
            if not progressed:
                break
        unresolved = 0
        while True:
            rows = self._expired_pending_page(cutoff, after_rowid=0)
            if not rows:
                break
            for row in rows:
                self._db.execute(
                    "DELETE FROM pending WHERE writer = ? AND seq = ? AND doc_id = ?",
                    (str(row["writer"]), int(row["seq"]), str(row["doc_id"])),
                )
                unresolved += 1
        self._db.commit()
        admitted = len(applied) - applied_before
        _LOG.warning(
            "orgfs.pending_expired org=%s space=%s count=%s oldestHeldAt=%s "
            "reason=age-after-revalidation admittedCount=%s discardedCount=%s "
            "unresolvedCount=%s",
            self._pending_org(),
            self.space_id,
            count,
            oldest,
            admitted,
            count - admitted,
            unresolved,
        )
        return count

    def expire_pending(self, *, deadline: float | None = None) -> int:
        """Re-evaluate and resolve aged parked envelopes during idle maintenance."""
        if deadline is None:
            deadline = self._monotonic() + self._pending_drain_budget_seconds
        with self._lock:
            before = int(self._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
            drained = self._drain_pending(deadline)
            self._last_drained += drained
            after = int(self._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
            return before - after

    def _warn_pending_drop(self, reason: str, *, count: int = 1) -> None:
        """Rate-limit capacity diagnostics while retaining a cumulative count."""

        self._pending_drop_count += count
        now = time.monotonic()
        if (
            now - self._pending_drop_warning_at
            < self._pending_drop_warning_interval_seconds
        ):
            return
        self._pending_drop_warning_at = now
        _LOG.warning(
            "orgfs dropped pending dependency: space=%s reason=%s droppedCount=%s",
            self.space_id,
            reason,
            self._pending_drop_count,
        )

    def _pending(self, envelope: bytes, supplier: str) -> bool:
        value = self._decode_envelope(envelope)
        writer = str(value["origin"]["writer"])
        author = str(value["origin"]["author"])
        meta_frontier = _unb64(str(value["origin"]["metaFrontier"]))
        seq = int(value["seq"])
        doc_id = str(value["docId"])
        with self._db:
            existing = self._db.execute(
                "SELECT 1 FROM pending WHERE writer = ? AND seq = ? AND doc_id = ?",
                (writer, seq, doc_id),
            ).fetchone()
            if existing is None:
                count = int(self._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
                supplier_count = int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM pending WHERE supplier = ?", (supplier,)
                    ).fetchone()[0]
                )
                if (
                    count >= self._pending_import_limit
                    or supplier_count >= self._pending_import_supplier_limit
                ):
                    self._warn_pending_drop("capacity")
                    return False
            self._db.execute(
                "INSERT INTO pending(writer, seq, doc_id, author, meta_frontier, "
                "envelope_bytes, supplier, held_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(writer, seq, doc_id) DO UPDATE SET "
                "author=excluded.author, meta_frontier=excluded.meta_frontier, "
                "envelope_bytes=excluded.envelope_bytes, supplier=excluded.supplier",
                (
                    writer,
                    seq,
                    doc_id,
                    author,
                    meta_frontier,
                    envelope,
                    supplier,
                    _now(),
                ),
            )
        return True

    def _drain_budget_exceeded(self, deadline: float | None) -> bool:
        if deadline is None or self._monotonic() < deadline:
            return False
        now = self._monotonic()
        if now - self._drain_budget_warning_at >= self._pending_drop_warning_interval_seconds:
            self._drain_budget_warning_at = now
            _LOG.warning("orgfs pending drain deferred by budget: space=%s", self.space_id)
        return True

    def _drain_pending(self, deadline: float | None = None) -> tuple[bytes, ...]:
        applied: list[bytes] = []
        previous = self._membership_deadline
        self._membership_deadline = deadline
        try:
            self._drain_pending_until(applied, deadline)
        except _MembershipBudgetExceeded:
            pass
        finally:
            self._membership_deadline = previous
        return tuple(applied)

    def _drain_pending_until(self, applied: list[bytes], deadline: float | None) -> None:
        while True:
            if self._drain_budget_exceeded(deadline):
                return
            self._expire_pending_locked(applied, deadline)
            progressed = False
            after_rowid = 0
            while True:
                rows = self._db.execute(
                    "SELECT rowid AS pending_rowid, * FROM pending "
                    "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (after_rowid, self._pending_drain_page_size),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    if self._drain_budget_exceeded(deadline):
                        return
                    after_rowid = int(row["pending_rowid"])
                    progressed = self._drain_pending_row(row, applied) or progressed
            if not progressed:
                return

    def _decode_pending_envelope(self, envelope: bytes) -> dict[str, Any]:
        """Decode one parked body after its stored wait token is covered."""

        return self._decode_envelope(envelope)

    def _drain_pending_row(self, row: sqlite3.Row, applied: list[bytes]) -> bool:
        wait_token = row["meta_frontier"]
        if wait_token is not None:
            token = bytes(wait_token)
            if token and self._membership_at(token) is None:
                return False
        envelope = bytes(row["envelope_bytes"])
        value: dict[str, Any] | None = None
        try:
            try:
                value = self._decode_pending_envelope(envelope)
                self._update_bytes(value)
                decision = self.admission(envelope)
            except StoreError as exc:
                if exc.code == "unknown-blob":
                    return False
                decision = (
                    "snapshot-barrier"
                    if exc.code == "snapshot-barrier"
                    else "not-a-member"
                )
            if decision in {"pending-meta", "pending-dependency"}:
                return False
            if (
                decision == "not-a-member"
                and value is not None
                and self._drain_keeps_not_a_member(value, self._update_bytes(value))
            ):
                return False
            if decision == "ok":
                result = self._apply_import(value, envelope, str(row["supplier"]))
                if result.status == "applied" and result.code != "duplicate":
                    applied.append(envelope)
                if result.status not in {"applied", "rejected"}:
                    return False
            elif decision == "snapshot-barrier":
                _LOG.warning(
                    "orgfs discarded pending retired document update: "
                    "space=%s doc=%s code=snapshot-barrier",
                    self.space_id, row["doc_id"],
                )
            self._db.execute(
                "DELETE FROM pending WHERE writer = ? AND seq = ? AND doc_id = ?",
                (str(row["writer"]), int(row["seq"]), str(row["doc_id"])),
            )
            self._db.commit()
            return True
        except _MembershipBudgetExceeded:
            raise
        except Exception:
            self._db.rollback()
            _LOG.exception("orgfs pending drain deferred after durable-trigger failure")
            return False

    def take_drained(self) -> tuple[bytes, ...]:
        """Return envelopes applied while the latest local meta commit drained pending."""

        with self._lock:
            drained = self._last_drained
            self._last_drained = ()
            return drained
