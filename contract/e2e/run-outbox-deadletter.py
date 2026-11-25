#!/usr/bin/env python3
"""E2E-017: outbox dead-letter defects (C1/C2/C3) over a real isolated daemon.

The cross-component chain this scenario exists to pin (unit tests only ever
see one segment of it):

    dead message held  →  identity resolved  →  it must NEVER appear in
    `hyprial message targets` (C3) — while offline time spends no attempts and TTL is
    the only negative terminal (C2), and prune retains its existing C1 cells.

Gate-driving shortcut: the TTL transition is seeded with the daemon stopped;
offline parking and the retired-attempt-cap control run live.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, hyprial_executable, short_temp_root

SCENARIO = "E2E-017"
DRIVER = "duty-driver"
SLEEPER = "sleeper-agent"
GHOST_BARE = "agent:ghost-bare"

CHECKS = (
    "daemon",
    "seed-queued",
    "offline-held-zero-attempts",
    "c3-ghost-never-in-targets",
    "c2-ttl-only-terminal",
    "c1-prune-discriminates",
    "targets-final",
)


class Report:
    def __init__(self) -> None:
        self.counts = {"PASS": 0, "FAIL": 0}

    def add(self, status: str, check: str, detail: str) -> None:
        clean = " ".join(str(detail).replace("\x00", " ").split())[:800]
        self.counts[status] = self.counts.get(status, 0) + 1
        print(f"RESULT {status} {SCENARIO} {check} {clean}", flush=True)

    def exit_code(self) -> int:
        overall = "PASS" if not self.counts["FAIL"] else "FAIL"
        print(
            f"RESULT {overall} {SCENARIO} pass={self.counts['PASS']} fail={self.counts['FAIL']}",
            flush=True,
        )
        return 0 if overall == "PASS" else 1


def run_cli(
    daemon: IsolatedDaemon, *args: str, timeout: float = 90.0
) -> tuple[int, dict[str, Any] | None, str]:
    completed = subprocess.run(
        [str(daemon.executable), *args],
        env=daemon.env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        value = None
    return completed.returncode, value if isinstance(value, dict) else None, (
        completed.stderr or completed.stdout
    )[-2000:]


def expire_ghost_ttl(root: Path, ghost_uri: str) -> None:
    """Daemon-stopped seed: move the ghost to its one terminal deadline."""
    db = sqlite3.connect(root / "state" / "inbox.sqlite3")
    try:
        now = int(time.time() * 1000)
        db.execute(
            "UPDATE outbox SET expires_at_ms = ?, next_attempt_ms = ? "
            "WHERE recipient = ?",
            (now, now, ghost_uri),
        )
        db.commit()
    finally:
        db.close()


def outbox_list(daemon: IsolatedDaemon) -> list[dict[str, Any]]:
    code, value, diagnostic = run_cli(daemon, "message", "outbox", "list", "--json")
    if code != 0 or value is None or not isinstance(value.get("entries"), list):
        raise AssertionError(f"outbox list failed ({code}): {diagnostic}")
    return value["entries"]


def targets_uris(daemon: IsolatedDaemon) -> set[str]:
    code, value, diagnostic = run_cli(daemon, "message", "targets", "--json")
    if code != 0 or value is None:
        raise AssertionError(f"targets failed ({code}): {diagnostic}")
    rows = value.get("targets") or value.get("items") or []
    uris: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            for key in ("targetUri", "actor", "uri"):
                if isinstance(row.get(key), str):
                    uris.add(row[key])
    return uris


def daemon_log_lines(root: Path) -> list[str]:
    log = root / "state" / "logs" / "daemon.jsonl"
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def seed_sqlite(
    root: Path, ghost_uri: str, ghost_local: str, sleeper_uri: str, old_cap: int
) -> tuple[str, str]:
    """Daemon-stopped seed: put the ghost beyond the retired attempt cap and add
    the bare host-class row the send gate would now reject at ingress."""
    db = sqlite3.connect(root / "state" / "inbox.sqlite3")
    try:
        now = int(time.time() * 1000)
        sleeper_ids = (
            f"seeds-{uuid4().hex[:12]}",
            f"seeds-{uuid4().hex[:12]}",
        )
        db.execute(
            "UPDATE outbox SET attempts = ?, next_attempt_ms = ?, "
            "retry_started_at_ms = ? WHERE recipient = ?",
            (old_cap + 2, now, now - 86_400_000, ghost_uri),
        )
        db.execute(
            """INSERT INTO outbox (message_id, conversation_id, sender, recipient,
                                   payload, intent, lifecycle, idempotency_key,
                                   created_at_ms, expires_at_ms, attempts,
                                   next_attempt_ms, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, NULL)""",
            (
                f"seedl-{uuid4().hex[:12]}",
                "conv-seed-local",
                "agent:o:node:duty-driver",
                ghost_local,
                b'{"message":"same-node ghost, unregistered"}',
                "request",
                "durable_service",
                now,
                now + 30 * 86_400_000,
                # Far-future due time: the same-node ghost would otherwise be
                # DELIVERED instantly by the retry pump (daemon-held inbox for
                # local URIs), leaving nothing for prune to judge.
                now + 3_600_000,
            ),
        )
        db.execute(
            """INSERT INTO outbox (message_id, conversation_id, sender, recipient,
                                   payload, intent, lifecycle, idempotency_key,
                                   created_at_ms, expires_at_ms, attempts,
                                   next_attempt_ms, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, NULL)""",
            (
                sleeper_ids[0],
                "conv-seed-sleeper",
                "agent:o:node:duty-driver",
                sleeper_uri,
                b'{"message":"registered but offline"}',
                "request",
                "durable_service",
                now,
                now + 30 * 86_400_000,
                now + 3_600_000,
            ),
        )
        db.execute(
            """INSERT INTO outbox (message_id, conversation_id, sender, recipient,
                                   payload, intent, lifecycle, idempotency_key,
                                   created_at_ms, expires_at_ms, attempts,
                                   next_attempt_ms, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, NULL)""",
            (
                f"seed-{uuid4().hex[:12]}",
                "conv-seed",
                f"agent:{GHOST_BARE.split(':')[1]}:node:duty-driver" if False else "agent:o:node:duty-driver",
                GHOST_BARE,
                b'{"message":"legacy bare-addressed letter"}',
                "request",
                "durable_service",
                now,
                now + 30 * 86_400_000,
                # Far-future due time: the same-node ghost would otherwise be
                # DELIVERED instantly by the retry pump (daemon-held inbox for
                # local URIs), leaving nothing for prune to judge.
                now + 3_600_000,
            ),
        )
        db.execute(
            """INSERT INTO outbox (message_id, conversation_id, sender, recipient,
                                   payload, intent, lifecycle, idempotency_key,
                                   created_at_ms, expires_at_ms, attempts,
                                   next_attempt_ms, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, NULL)""",
            (
                sleeper_ids[1],
                "conv-seed-sleeper",
                "agent:o:node:duty-driver",
                sleeper_uri,
                b'{"message":"registered but offline"}',
                "request",
                "durable_service",
                now,
                now + 30 * 86_400_000,
                now + 3_600_000,
            ),
        )
        db.commit()
        return sleeper_ids
    finally:
        db.close()


def main() -> int:
    report = Report()
    executable = hyprial_executable()
    with tempfile.TemporaryDirectory(prefix="hyprial-e2e-017-", dir=short_temp_root()) as temporary:
        root = Path(temporary)
        daemon = IsolatedDaemon(executable, root, "dl-e2e017-node")
        try:
            # ── 1. isolated daemon + identities ───────────────────────────
            daemon.start()
            created = daemon.command("agent", "create", "--name", DRIVER, "--json")
            assert created.get("ok") is True, created
            sleeper = daemon.command("agent", "create", "--name", SLEEPER, "--json")
            sleeper_uri = str(sleeper["agent"]["uri"])
            driver_uri = str(created["agent"]["uri"])
            owner, node = driver_uri.split(":")[1], driver_uri.split(":")[2]
            # Remote ghost: queues and retries (a same-node ghost would be
            # delivered instantly into a daemon-held inbox row — #195 — and
            # never exercise the pump).
            ghost_uri = f"agent:{owner}:other-node:ghost-x"
            # Local ghost for the prune side: same node, unregistered, offline.
            ghost_local = f"agent:{owner}:{node}:ghost-local"
            report.add("PASS", "daemon", f"owner={owner} node={node}")

            # ── 2. seed: a canonical-but-nonexistent target queues durably ─
            sent = daemon.command(
                "message", "send", "--from", DRIVER, "--to", ghost_uri,
                "--conversation", f"e2e017-{uuid4().hex[:8]}",
                "--summary", "letter to nobody",
                "letter to nobody", "--json",
            )
            assert sent.get("ok") is True, sent
            # NOTE: a message to a local registered agent is DELIVERED
            # instantly (daemon-held inbox), so the "offline-but-registered
            # keeps its outbox entry" side is seeded via sqlite below with a
            # far-future due time (keeps the retry pump away from it).
            entries = outbox_list(daemon)
            assert any(e["recipient"] == ghost_uri for e in entries), entries
            report.add("PASS", "seed-queued", "ghost + sleeper letters queued")

            # ── 3. C2: offline holds spend no attempts ────────────────────
            def attempts_of(uri: str) -> int:
                row = next((e for e in outbox_list(daemon) if e["recipient"] == uri), None)
                return int(row["attempts"]) if row else -1

            time.sleep(2.0)
            assert attempts_of(ghost_uri) == 0
            report.add("PASS", "offline-held-zero-attempts", "attempts=0 while ghost stayed offline")

            # ── 4. C3 core: the ghost NEVER appears in targets ────────────
            uris = targets_uris(daemon)
            assert ghost_uri not in uris, f"ghost appeared in targets: {sorted(u for u in uris if 'ghost' in u)}"
            assert sleeper_uri in uris, "registered sleeper must be listed (even offline)"
            report.add("PASS", "c3-ghost-never-in-targets", "ghost absent from targets while held offline")

            # ── 5. C2: the retired cap cannot terminalize; TTL can ────────
            daemon.stop()
            cap = 5  # RetryPolicy default backoff tuple length
            sleeper_ids = seed_sqlite(root, ghost_uri, ghost_local, sleeper_uri, cap)
            daemon.start()

            def terminal_lines(code: str) -> list[str]:
                return [line for line in daemon_log_lines(root) if code in line and ghost_uri in line]

            time.sleep(2.0)
            ghost_entry = next(e for e in outbox_list(daemon) if e["recipient"] == ghost_uri)
            assert int(ghost_entry["attempts"]) == cap + 2
            assert not terminal_lines("DELIVERY_RETRY_EXHAUSTED")
            assert not terminal_lines("DELIVERY_CONFIRMATION_UNRESOLVED")

            daemon.stop()
            expire_ghost_ttl(root, ghost_uri)
            daemon.start()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not terminal_lines("TTL_EXPIRED"):
                time.sleep(1.0)
            assert terminal_lines("TTL_EXPIRED"), "ghost never hit TTL_EXPIRED"
            assert not any(e["recipient"] == ghost_uri for e in outbox_list(daemon))
            before = len(terminal_lines("TTL_EXPIRED"))
            time.sleep(3.0)
            after = len(terminal_lines("TTL_EXPIRED"))
            assert before == after, f"retry kept acting after the verdict: {before} -> {after}"
            report.add(
                "PASS",
                "c2-ttl-only-terminal",
                f"attempts={cap + 2} stayed held; TTL_EXPIRED once; zero new terminal records after",
            )

            # ── 6. C1: prune discriminates nonexistent vs merely-offline ──
            code, pruned, diagnostic = run_cli(daemon, "message", "outbox", "prune", "--json")
            if code != 0 or pruned is None:
                raise AssertionError(f"prune call failed: code={code} diag={diagnostic!r}")
            pruned_recipients = {item["recipient"] for item in pruned.get("pruned", [])}
            if GHOST_BARE not in pruned_recipients:
                raise AssertionError(f"bare not pruned; pruned={json.dumps(pruned, ensure_ascii=False)[:600]}")
            assert GHOST_BARE in pruned_recipients, f"bare host row not pruned: {pruned}"
            bare_reason = next(item["reason"] for item in pruned["pruned"] if item["recipient"] == GHOST_BARE)
            assert bare_reason == "UNDELIVERABLE_SCHEME", bare_reason
            local_reason = next(item["reason"] for item in pruned["pruned"] if item["recipient"] == ghost_local)
            assert local_reason == "TARGET_UNRESOLVABLE", local_reason
            assert sleeper_uri not in pruned_recipients, pruned
            def sleeper_custody() -> dict[str, str]:
                with sqlite3.connect(root / "state" / "inbox.sqlite3") as database:
                    outbox_ids = {
                        row[0]
                        for row in database.execute(
                            "SELECT message_id FROM outbox WHERE message_id IN (?, ?)",
                            sleeper_ids,
                        )
                    }
                    delivery_status = {
                        row[0]: (row[1], row[2])
                        for row in database.execute(
                            "SELECT message_id, state, reason FROM delivery_status "
                            "WHERE message_id IN (?, ?)",
                            sleeper_ids,
                        )
                    }
                return {
                    message_id: (
                        "outbox"
                        if message_id in outbox_ids
                        else "ACK_RECEIVED"
                        if delivery_status.get(message_id)
                        == ("fetched", "ACK_RECEIVED")
                        else "LOST"
                    )
                    for message_id in sleeper_ids
                }

            # A letter the wake is delivering right now can be between leaving
            # the outbox and its commit receipt; judge LOST only if it is still
            # in neither place after a bounded settle window.
            settle_deadline = time.monotonic() + 10
            custody = sleeper_custody()
            while "LOST" in custody.values() and time.monotonic() < settle_deadline:
                time.sleep(0.25)
                custody = sleeper_custody()
            assert all(value != "LOST" for value in custody.values()), custody
            report.add(
                "PASS",
                "c1-prune-discriminates",
                "bare nonexistent pruned; ghost-local pruned; every sleeper retained or ACK_RECEIVED",
            )

            # ── 7. final: targets still clean after the whole cycle ───────
            uris = targets_uris(daemon)
            assert not any("ghost" in u for u in uris), f"ghost leaked into targets: {uris}"
            report.add("PASS", "targets-final", "no ghost in targets after retry-death and prune")

        except (AssertionError, KeyError, TypeError, StopIteration) as error:
            report.add("FAIL", "assertion", str(error))
            return report.exit_code()
        finally:
            try:
                daemon.stop()
            except Exception:
                pass
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
