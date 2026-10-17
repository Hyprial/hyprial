"""Fresh SQLite opens and an independent, epoch-labelled health heartbeat.

This is a read probe, never a second database writer or repair engine. A missing
database is an error: mode=rw prevents a false healthy empty replacement.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

from hyprial.kernel import atomic_json_write
from .write_rate import WriteRateWindow


class DatabaseHealth:
    def __init__(self, state_dir: Path, *, epoch: str, databases: tuple[str, ...], log,
                 diagnostics=None, write_alarm=None):
        self._root = state_dir
        self._epoch = epoch
        self._databases = databases
        self._log = log
        self._diagnostics = diagnostics
        self._write_alarm = write_alarm
        self._write_rate = WriteRateWindow()
        self._guard = threading.Lock()
        self._checking = threading.Lock()
        self._report: dict = {"healthy": None, "epoch": epoch, "checkedAtMs": None}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def status(self) -> dict:
        with self._guard:
            return json.loads(json.dumps(self._report))

    def check(self) -> dict:
        with self._checking:
            return self._check()

    def _check(self) -> dict:
        failures = []
        started = time.monotonic()
        for name in self._databases:
            try:
                # A new connection tests the path and WAL readability. Already
                # open handles can stay usable after their path disappears.
                connection = sqlite3.connect(
                    (self._root / name).resolve().as_uri() + "?mode=rw", uri=True, timeout=0.2,
                )
                try:
                    connection.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
                finally:
                    connection.close()
            except (sqlite3.Error, OSError) as error:
                failures.append({
                    "database": name,
                    "errorType": type(error).__name__,
                    "sqliteErrorCode": getattr(error, "sqlite_errorcode", None),
                    "sqliteErrorName": getattr(error, "sqlite_errorname", None),
                    "errno": getattr(error, "errno", None),
                })
        prior = self.status()
        report = {
            "schemaVersion": 1, "epoch": self._epoch, "pid": os.getpid(),
            "healthy": not failures, "checkedAtMs": time.time_ns() // 1_000_000,
            "durationMs": int((time.monotonic() - started) * 1000),
            "databases": list(self._databases), "failures": failures,
            "threadCount": threading.active_count(),
            "threadCountScope": "python-managed",
        }
        try:
            diagnostics = self._diagnostics() if self._diagnostics is not None else {}
        except Exception as error:
            diagnostics = {"diagnosticsError": type(error).__name__}
        report["runtime"] = diagnostics
        total = diagnostics.get("orphanWriteBytes")
        rate_event = self._write_rate.observe(total, time.monotonic()) if isinstance(total, int) else None
        report["writeRate"] = {**self._write_rate.status(), "available": isinstance(total, int)}
        try:
            report["openFds"] = len(os.listdir("/dev/fd")) - 1
        except OSError:
            report["openFds"] = None
        with self._guard:
            self._report = report
        # This single writer has its own file. Never race daemon.json's owner.
        try:
            atomic_json_write(self._root / "daemon-health.json", report)
        except OSError as error:
            report = {**report, "healthy": False, "heartbeatError": type(error).__name__}
            with self._guard:
                self._report = report
        event = (
            "daemon.storage.unavailable" if not report["healthy"]
            else "daemon.storage.recovered" if prior.get("healthy") is False
            else None
        )
        if event is not None and (prior.get("healthy") != report["healthy"]):
            # Author-generated codes only: this fallback works even when the
            # main logger is stalled, without emitting exception payloads.
            try:
                print(json.dumps({"event": event, "epoch": self._epoch,
                                  "failures": failures}), file=sys.stderr, flush=True)
            except Exception:
                pass
            try:
                self._log("error" if failures else "info", event, **report)
            except Exception:
                pass
        if rate_event is not None:
            try:
                self._log("error" if rate_event == "high" else "info",
                          "daemon.orphan.write_rate." + rate_event, **report["writeRate"])
                if rate_event == "high" and self._write_alarm is not None:
                    self._write_alarm(report["writeRate"])
            except Exception:
                pass
        return self.status()

    def require_healthy(self):
        if not self.check()["healthy"]:
            raise RuntimeError("daemon database health check failed")

    def start(self, stop_event: threading.Event, *, interval: float = 30.0):
        if self._thread is not None:
            return

        def watch():
            while not self._stop.wait(interval):
                if stop_event.is_set():
                    return
                self.check()

        self._thread = threading.Thread(target=watch, name="hyprial-storage-health", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        return self._thread is None or not self._thread.is_alive()
