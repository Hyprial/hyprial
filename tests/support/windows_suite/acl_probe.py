"""ACL probe: record where Windows test runs create directories.

Enabled by ``HYPRIAL_TEST_ACL_PROBE=1`` together with the ledger on Windows.
An audit hook appends one line per ``os.mkdir`` and ``tempfile.mkdtemp`` event
to ``acl-probe.jsonl`` in the ledger directory, with ``event``, ``path``,
``mode`` (``os.mkdir`` only), ``mono``, ``pid`` and the current ``nodeid``.

Decision (2026-11-26): the probe records creation points only.  Failure-side
diagnosis (full-path SDDL and the first non-stdlib stack when a failure message
matches ``unsafe|TAILNET_STORAGE_UNSAFE|unsafe-acl``) is left to a later change
if the recorded creation points do not explain a failure.

Decision (same date): ``sys.addaudithook`` cannot be removed and sees only the
process that installed it.  Directories created by daemon, CLI or other child
processes never appear here; read their absence as "creator outside the pytest
process", not as "not created".  An exception raised by an audit hook aborts
the audited operation, so the hook drops any failure of its own instead of
failing the test's ``mkdir``, and a per-thread guard keeps its own file writes
from re-entering.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROBE_FILE = "acl-probe.jsonl"
EVENTS = frozenset({"os.mkdir", "tempfile.mkdtemp"})

_INSTALLED = False


class AuditRecorder:
    """Audit hook that appends directory-creation events to one JSONL file."""

    def __init__(self, sink: Path, current_nodeid: Callable[[], str | None]) -> None:
        self.sink = sink
        self._current_nodeid = current_nodeid
        self._lock = threading.Lock()
        self._local = threading.local()

    def __call__(self, event: str, args: tuple[Any, ...]) -> None:
        if event not in EVENTS or getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            self._append(event, args)
        except Exception:
            return
        finally:
            self._local.busy = False

    def _append(self, event: str, args: tuple[Any, ...]) -> None:
        target = args[0] if args else None
        try:
            path = os.fsdecode(target)
        except TypeError:
            path = repr(target)
        record: dict[str, Any] = {
            "event": event,
            "path": path,
            "mono": time.monotonic(),
            "pid": os.getpid(),
            "nodeid": self._current_nodeid(),
        }
        if event == "os.mkdir" and len(args) > 1:
            record["mode"] = args[1]
        line = (json.dumps(record, default=str) + "\n").encode("utf-8")
        with self._lock, open(self.sink, "ab") as stream:
            stream.write(line)


def install_acl_probe(ledger_dir: Path, current_nodeid: Callable[[], str | None]) -> bool:
    """Install the audit hook on Windows; return whether a hook is active."""

    global _INSTALLED
    if os.name != "nt":
        return False
    if not _INSTALLED:
        sys.addaudithook(AuditRecorder(Path(ledger_dir) / PROBE_FILE, current_nodeid))
        _INSTALLED = True
    return True
