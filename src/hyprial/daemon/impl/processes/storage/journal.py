"""Idempotent fsynced deltas with sequence-fenced snapshot compaction.

The snapshot remains v1-readable. Recovery applies only journal sequences newer
than the snapshot, so a crash between snapshot replace and journal truncation
cannot resurrect retired entries. An incomplete final append is never accepted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from hyprial.kernel import ProcessLivenessProbeError

SCHEMA = "hyprial.orphan-processes/v1"


class CustodyJournal:
    def __init__(self, path: Path | None, snapshot_writer):
        self.path = path
        self.log_path = path.with_suffix(path.suffix + ".jsonl") if path else None
        self._snapshot_writer = snapshot_writer
        self.rows: dict[str, dict] = {}
        self.sequence = 0
        self._operations = 0
        self._bytes = 0
        self.stats = dict(appendWrites=0, appendBytes=0, snapshotWrites=0,
                          snapshotBytes=0, unchangedSaves=0)
        self._snapshot_exists = False
        self._snapshot_bytes = 0
        self._failed = False
        self._load()

    def _load(self):
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
                raise ValueError("invalid schema")
            if not isinstance(raw.get("orphans"), list):
                raise ValueError("rows are invalid")
            self.rows = {row["orphan_id"]: row for row in raw["orphans"]}
            self.sequence = int(raw.get("journal_seq", 0))
            self._snapshot_exists = True
            self._snapshot_bytes = self.path.stat().st_size
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError) as error:
            raise ProcessLivenessProbeError(f"orphan process registry {error}") from error
        assert self.log_path is not None
        try:
            data = self.log_path.read_bytes()
        except FileNotFoundError:
            return
        complete = data.rfind(b"\n") + 1
        for line in data[:complete].splitlines():
            try:
                delta = json.loads(line)
                seq = delta["seq"]
                if seq <= self.sequence:
                    continue
                if seq != self.sequence + 1:
                    raise ValueError("journal sequence gap")
                for row in delta["upsert"]:
                    self.rows[row["orphan_id"]] = row
                for key in delta["delete"]:
                    self.rows.pop(key, None)
                self.sequence = seq
                self._operations += 1
            except (ValueError, KeyError, TypeError) as error:
                raise ProcessLivenessProbeError(f"orphan process journal invalid: {error}") from error
        # The uncommitted suffix cannot be allowed to prefix the next append.
        if complete != len(data):
            with self.log_path.open("r+b") as stream:
                stream.truncate(complete)
                stream.flush()
                os.fsync(stream.fileno())
        self._bytes = complete

    def commit(self, updates: dict[str, dict | None]):
        if self._failed:
            raise ProcessLivenessProbeError("orphan journal write failed; reopen required")
        changed = {key: value for key, value in updates.items()
                   if self.rows.get(key) != value}
        if not changed:
            self.stats["unchangedSaves"] += 1
            return
        if self.path is None:
            self._apply(changed)
            return
        if not self._snapshot_exists:
            # Keep memory unchanged if the initial snapshot write fails.
            rows = dict(self.rows)
            for key, value in changed.items():
                if value is None:
                    rows.pop(key, None)
                else:
                    rows[key] = value
            self._snapshot(rows)
            self.rows = rows
            return
        delta = {"seq": self.sequence + 1,
                 "upsert": [v for v in changed.values() if v is not None],
                 "delete": [k for k, v in changed.items() if v is None]}
        data = (json.dumps(delta, separators=(",", ":")) + "\n").encode()
        assert self.log_path is not None
        descriptor = os.open(self.log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # The journal name itself must survive a crash, as well as its bytes.
            directory = os.open(self.log_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            self._failed = True
            raise
        self.sequence += 1
        self._apply(changed)
        self.stats["appendWrites"] += 1
        self.stats["appendBytes"] += len(data)
        self._operations += 1
        self._bytes += len(data)
        # Compact proportional to retained state, not every fixed N events:
        # a growing fleet then costs O(total deltas), rather than O(N squared).
        if self._bytes >= max(256 * 1024, self._snapshot_bytes):
            self.compact()

    def _apply(self, changed):
        for key, value in changed.items():
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value

    def _snapshot(self, rows):
        value = {"schema": SCHEMA, "journal_seq": self.sequence,
                 "orphans": list(rows.values())}
        self._snapshot_writer(self.path, value)
        self._snapshot_exists = True
        self._snapshot_bytes = self.path.stat().st_size
        self.stats["snapshotWrites"] += 1
        self.stats["snapshotBytes"] += self._snapshot_bytes

    def compact(self):
        if self.path is None:
            return
        self._snapshot(self.rows)
        assert self.log_path is not None
        with self.log_path.open("wb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        self._operations = self._bytes = 0
