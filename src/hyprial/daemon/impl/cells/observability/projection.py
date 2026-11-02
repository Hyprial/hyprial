"""Bounded, non-durable diagnostic history hosted by existing owners."""
from collections import OrderedDict
from hashlib import sha256
from uuid import uuid4

from .model import RunFacts
from .reducer import derive_run_state


class RunLedger:
    """Validated receipt projection; eviction permanently loses completeness.

    Owned under the existing session-turn lock. A new fence starts unknown.
    An authenticated current-state refresh (``session.turn.refresh``) restores
    completeness and replaces the open set; heartbeats and progress events
    cannot. The refresh is observation only: it publishes no turn event and
    never writes, acks, replies to or reads an inbox row.
    """

    def __init__(self, capacity=1024, producer_capacity=1024):
        self.capacity = capacity
        self.producer_capacity = producer_capacity
        self.retired_producers = set()
        self.producer_history_exhausted = False
        self.refresh_rejection_reason = None
        self.rows = OrderedDict()
        self.refreshed = {}
        self.capabilities = None
        self.producer_epoch = None
        self.producer_seq = -1
        self.authoritative = False
        self.gap = False
        self.epoch = None

    def advance_mount(self, epoch):
        """Advance only to the current identity-owned mount, never reset stats.

        The application validates and audits this transition under its receipt
        lock. Repeating the same mount is a no-op, including after saturation.
        """

        if epoch == self.epoch:
            return
        self.epoch = epoch
        self.retired_producers.clear()
        self.producer_history_exhausted = False
        self.refresh_rejection_reason = None
        self.rows.clear()
        self.refreshed = {}
        self.capabilities = None
        self.producer_epoch = None
        self.producer_seq = -1
        self.authoritative = False
        self.gap = False

    def record(self, epoch, report, phase, *, at_ms, monotonic_ms):
        if epoch != self.epoch:
            self.advance_mount(epoch)
        before = self.rows.get(report)
        if before is not None and (before[0] == "end" or phase == "start"):
            return
        self.rows[report] = (phase, at_ms, monotonic_ms)
        if phase == "end":
            self.refreshed.pop(report, None)
        if len(self.rows) > self.capacity:
            self.rows.popitem(last=False)
            self.gap = True

    def refresh(self, epoch, *, producer_epoch, seq, runs, capabilities, at_ms, monotonic_ms):
        """Apply one authoritative current open-run set.

        Returns ``(applied, resyncCleared)``. A duplicate or older producer
        sequence is an idempotent no-op: a retried report can never resurrect
        state a newer report already replaced. An ended report id is never
        reopened. Retired epochs are never readmitted within the mount fence.
        History saturation latches unknown until a new mount fence; no retired
        identity is evicted to make room. The rejection reason is read under
        the owner's lock along with this result. Receipt rows the carrier no
        longer lists as open are dropped without publishing a turn transition.
        """

        if epoch != self.epoch:
            self.advance_mount(epoch)
        self.refresh_rejection_reason = None
        if producer_epoch in self.retired_producers:
            self.refresh_rejection_reason = "retired_producer"
            return False, False
        if self.producer_history_exhausted:
            self.refresh_rejection_reason = "producer_history_exhausted"
            return False, False
        if producer_epoch == self.producer_epoch and seq <= self.producer_seq:
            self.refresh_rejection_reason = "stale_sequence"
            return False, False
        if self.producer_epoch is not None and producer_epoch != self.producer_epoch:
            if len(self.retired_producers) >= self.producer_capacity:
                self.producer_history_exhausted = True
                self.gap = True
                self.refresh_rejection_reason = "producer_history_exhausted"
                return False, False
            self.retired_producers.add(self.producer_epoch)
        self.producer_epoch = producer_epoch
        self.producer_seq = seq
        self.capabilities = dict(capabilities)
        current = {}
        for report_id, phase, elapsed_ms in runs:
            row = self.rows.get(report_id)
            if row is not None and row[0] == "end":
                continue
            previous = self.refreshed.get(report_id)
            if previous is not None and previous[2] is not None:
                current[report_id] = previous
            elif elapsed_ms is None:
                current[report_id] = (phase, None, None)
            else:
                current[report_id] = (
                    phase,
                    max(0, at_ms - elapsed_ms),
                    max(0, monotonic_ms - elapsed_ms),
                )
        self.refreshed = current
        for report_id in [
            report
            for report, row in self.rows.items()
            if row[0] == "start" and report not in current
        ]:
            del self.rows[report_id]
        cleared = self.gap
        self.gap = False
        self.authoritative = True
        return True, cleared

    def snapshot(self, epoch):
        if epoch != self.epoch:
            return RunFacts(source_epoch=str(epoch))
        opens = {
            report: (row[1], row[2])
            for report, row in self.rows.items()
            if row[0] == "start"
        }
        for report, (_phase, at_ms, monotonic_ms) in self.refreshed.items():
            row = self.rows.get(report)
            if row is not None and row[0] == "end":
                continue
            known = opens.get(report)
            if known is None:
                opens[report] = (at_ms, monotonic_ms)
            elif known[1] is None:
                opens[report] = (at_ms, monotonic_ms) if monotonic_ms is not None else known
            elif monotonic_ms is not None:
                opens[report] = (min(known[0], at_ms), min(known[1], monotonic_ms))
        if not self.rows and not self.authoritative:
            return RunFacts(source_epoch=str(epoch))
        timed = [row for row in opens.values() if row[1] is not None]
        oldest = min(timed, key=lambda row: row[1]) if timed else None
        unknown_age = len(timed) != len(opens)
        return RunFacts(source_epoch=str(epoch), known=not self.gap, running=True,
                        resync_required=self.gap, open_count=len(opens),
                        since_ms=oldest[0] if oldest and not unknown_age else None,
                        since_monotonic_ms=oldest[1] if oldest and not unknown_age else None)


class RunProjection:
    """One immutable request cut, not an independently running actor."""

    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.epoch = uuid4().hex
        self.revision = 0
        self.previous = OrderedDict()

    def publish(self, inputs, *, now_ms, monotonic_ms):
        self.revision += 1
        result = {}
        for actor, (cell, facts, policy) in inputs.items():
            runtime = sha256(f"{cell}:{facts.source_epoch}".encode()).hexdigest()[:32]
            row = derive_run_state(facts, policy, now_ms=now_ms, monotonic_ms=monotonic_ms)
            previous = self.previous.get(actor)
            row["lastKnown"] = None
            if row["state"] == "unknown" and previous is not None:
                if previous["state"] != "unknown":
                    row["lastKnown"] = {key: previous[key] for key in (
                        "state", "baseState", "sinceMs", "observedAtMs", "runtimeIncarnation",
                    )}
                elif previous.get("lastKnown") is not None:
                    row["lastKnown"] = dict(previous["lastKnown"])
            row.update(runtimeIncarnation=runtime, epoch=self.epoch, revision=self.revision)
            result[actor] = row
            self.previous[actor] = dict(row)
            self.previous.move_to_end(actor)
        while len(self.previous) > self.capacity:
            self.previous.popitem(last=False)
        return result
