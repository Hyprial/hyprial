# Decision notes (E2E-031, 2026-11-19):
# - The runner speaks only the public CLI, the daemon's own socket and a
#   read-only open of this scenario's own inbox database; nothing reaches the
#   production home, socket or daemon.
# - Rows are counted in the durable inbox, not only in the pending view: a
#   group row id is uuid5 of (group, entry, member) and the inbox keys a row by
#   its id, so "exactly one row" is read as "exactly one row whose payload names
#   this entry for this recipient", which also catches a second row under any
#   other id.
# - Group delivery happens inside the send (append_message), so a positive
#   barrier (the woken member's row exists) is enough before a negative count;
#   no sleep stands in for "nothing arrived".
# - Every assertion is recorded with its own status; a failed step marks only
#   the steps that need its state NOT_RUN, so a product defect is reported with
#   the command, the expectation and the observation instead of aborting.
"""Fixture, CLI wrappers, durable reads and the per-assertion report for E2E-031."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
NOT_RUN = "NOT_RUN"
BLOCKED = "BLOCKED"


def evidence(step: str, **fields: Any) -> None:
    """One machine-readable evidence line, the house EVIDENCE format."""

    print(f"EVIDENCE {step} " + json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str),
          flush=True)


@dataclass
class Outcome:
    """The status of one assertion and how it was driven."""

    status: str
    driven: str
    detail: str = ""


@dataclass
class Report:
    """Per-assertion statuses, kept apart: PASS, FAIL, BLOCKED, UNKNOWN, NOT_RUN."""

    scenario: str
    outcomes: dict[str, Outcome] = field(default_factory=dict)

    def record(self, step: str, status: str, driven: str, detail: str = "") -> None:
        self.outcomes[step] = Outcome(status, driven, detail)
        print(f"RESULT {status} {self.scenario} {step} driven={driven}"
              + (f" detail={detail}" if detail else ""), flush=True)

    def passed(self, step: str) -> bool:
        outcome = self.outcomes.get(step)
        return outcome is not None and outcome.status == PASS

    def run(self, step: str, driven: str, needs: tuple[str, ...],
            body: Callable[[], Any]) -> Any:
        """Run one assertion unless a step it needs did not pass."""

        unmet = [name for name in needs if not self.passed(name)]
        if unmet:
            self.record(step, NOT_RUN, driven, f"needs {','.join(unmet)}")
            return None
        try:
            value = body()
        except Exception as error:  # noqa: BLE001 - every failure is this step's FAIL, never an abort
            notes = " ".join(getattr(error, "__notes__", []))
            kind = "" if isinstance(error, AssertionError) else f"{type(error).__name__}: "
            self.record(step, FAIL, driven, f"{kind}{error} {notes}".strip()[:6000])
            return None
        self.record(step, PASS, driven)
        return value

    def summary(self) -> tuple[str, int]:
        """The final verdict and exit code: 0 PASS, 1 FAIL, 2 BLOCKED, 4 incomplete."""

        statuses = {outcome.status for outcome in self.outcomes.values()}
        counts = " ".join(
            f"{status.lower()}={sum(1 for o in self.outcomes.values() if o.status == status)}"
            for status in (PASS, FAIL, BLOCKED, UNKNOWN, NOT_RUN)
        )
        if FAIL in statuses:
            return f"FAIL {counts}", 1
        if BLOCKED in statuses:
            return f"BLOCKED {counts}", 2
        if statuses - {PASS}:
            return f"INCOMPLETE {counts}", 4
        return f"PASS {counts}", 0


class Expected(AssertionError):
    """An assertion failure carrying the command, the expectation and the observation."""

    def __init__(self, command: str, expected: str, actual: Any) -> None:
        super().__init__(
            f"command={command!r} expected={expected!r} actual={json.dumps(actual, default=str)[:3000]}"
        )


def run_json(daemon: Any, *args: str, exits: tuple[int, ...] = (0, 1),
             env: dict[str, str] | None = None) -> dict[str, Any]:
    """One hyprial command with ``--json``; the parsed object (a failure is data)."""

    if env is None:
        return daemon.command(*args, "--json", exits=exits)
    saved = dict(daemon.env)
    daemon.env.update(env)
    try:
        return daemon.command(*args, "--json", exits=exits)
    finally:
        daemon.env.clear()
        daemon.env.update(saved)


def require_ok(command: str, result: dict[str, Any], expected: str = "ok:true") -> dict[str, Any]:
    if result.get("ok") is False:
        raise Expected(command, expected, result)
    return result


def group_row_id(group_id: str, entry_id: str, member_id: str) -> str:
    """The row id the daemon derives for one member's row of one entry."""

    return str(uuid5(NAMESPACE_URL, f"hyprial:group-row:{group_id}:{entry_id}:{member_id}"))


def durable_rows(daemon: Any, conversation_id: str) -> list[dict[str, Any]]:
    """Every inbox row of one conversation, read-only from this scenario's own database."""

    database = Path(daemon.root) / "state" / "inbox.sqlite3"
    if not database.is_file():
        raise AssertionError(f"scenario inbox database is missing: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT message_id, conversation_id, sender, recipient, intent, consumed, "
            "acknowledged_at_ms, fetched_at_ms, payload FROM inbox "
            "WHERE conversation_id = ? ORDER BY arrival_id",
            (conversation_id,),
        ).fetchall()
    finally:
        connection.close()
    result = []
    for row in rows:
        item = dict(row)
        payload = item.pop("payload")
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        try:
            item["payload"] = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            item["payload"] = {"undecodable": True}
        result.append(item)
    return result


def rows_for_entry(daemon: Any, group_id: str, entry_id: str) -> dict[str, list[dict[str, Any]]]:
    """Recipient address to the durable rows naming *entry_id* in *group_id*."""

    rows: dict[str, list[dict[str, Any]]] = {}
    for row in durable_rows(daemon, group_id):
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("groupMessageId") == entry_id:
            rows.setdefault(str(row["recipient"]), []).append(row)
    return rows


def wait_for(description: str, probe: Callable[[], Any], accept: Callable[[Any], bool],
             *, timeout: float, interval: float = 0.25) -> Any:
    """Poll *probe* until *accept* holds, or fail with the last reading."""

    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if accept(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"{description} did not happen within {timeout:g}s; last={last!r}"[:4000])


def daemon_pid(daemon: Any) -> int | None:
    """The owned daemon's pid from its own ``state/daemon.json``, when readable."""

    try:
        value = json.loads((Path(daemon.root) / "state" / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 else None


def daemon_log_count(daemon: Any, event: str) -> int:
    """How many times *event* appears in this scenario's own daemon log."""

    log = Path(daemon.root) / "state" / "logs" / "daemon.jsonl"
    if not log.is_file():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
               if f'"{event}"' in line)


@dataclass
class Group:
    """What the scenario learned about its group."""

    group_id: str = ""
    owner_member: str = ""
    members: dict[str, str] = field(default_factory=dict)
    entries: list[str] = field(default_factory=list)
    rows: dict[str, str] = field(default_factory=dict)


def member_ids(show: dict[str, Any]) -> dict[str, str]:
    """Display name (or the owner's id) to member id, from ``message group show``."""

    result: dict[str, str] = {}
    for row in show.get("members") or []:
        if not isinstance(row, dict) or not isinstance(row.get("memberId"), str):
            continue
        name = row.get("displayName")
        result[str(name) if isinstance(name, str) and name else row["memberId"]] = row["memberId"]
    return result


__all__ = [
    "BLOCKED",
    "Expected",
    "FAIL",
    "Group",
    "NOT_RUN",
    "PASS",
    "Report",
    "UNKNOWN",
    "daemon_log_count",
    "daemon_pid",
    "durable_rows",
    "evidence",
    "group_row_id",
    "member_ids",
    "require_ok",
    "rows_for_entry",
    "run_json",
    "wait_for",
]
