"""Tell the owner when an actor stops collecting its mail -- and when that
mail is thrown away.

2026-09-18: a resident actor stopped fetching at 03:00 and kept working --
it merged two pull requests in the hour that followed -- while eleven
messages addressed to it expired unread.  Nothing reported it.  The loss was
found by hand, and only because someone happened to compare two tables.

Two separate failures make the same silence, and both are covered here:

* the mail is still there and NOBODY HAS PICKED IT UP.  That is
  :meth:`InboxWatchdog.observe_unfetched`, and it fires while the messages
  can still be read -- the whole point is to speak BEFORE the deadline.
* the deadline passed and the sweep threw the message away.  That is
  :meth:`InboxWatchdog.observe_pruned`.  Reaping is a decision this system
  made on purpose (``InboxService.prune_inbox``: "left alone they accumulate
  forever"), so this does not change it -- it removes the silence around it.

⛔ What this must NOT do is alarm on a BUSY actor, and getting that right
needs BOTH spellings of "somebody took responsibility for this message":

* the pull path (``harness_read`` -> ``fetch_pending``) stamps
  ``fetched_at_ms``.  That column has exactly one writer;
* the streaming path never touches it.  The daemon calls ``refresh_hold``
  (#276) when the worker ACCEPTS a delivery into its queue, which pushes
  ``expires_at_ms`` past ``received_at_ms + hold_ttl_ms`` -- the only trace
  that path leaves.

Keying on ``fetched_at_ms IS NULL`` alone would report every healthy
streaming worker with a queued message as "not collecting its mail": the
alarm would be loudest on the normal case.  And neither reading is the
``consumed = 0`` depth ``hyprial top`` shows -- that counts messages already
taken into a turn, which is what being busy looks like.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.inbox.pull import DEFAULT_HOLD_TTL_MS

STATE_FILE_NAME = "inbox-watchdog.json"
STATE_SCHEMA_VERSION = 1

#: Speak at half the hold TTL, so the owner hears about mail that is still
#: readable rather than mail that is already gone.  Composed from the TTL it
#: is measured against -- ⛔ never an independent number, because the two
#: drifting apart is what would make this either mute or a nuisance.
UNFETCHED_ALERT_MS = DEFAULT_HOLD_TTL_MS // 2

#: How long before the same still-stuck actor is reported again.  A stuck
#: actor stays stuck, and repeating every tick would train the reader to
#: ignore exactly this line.
REPEAT_AFTER_MS = DEFAULT_HOLD_TTL_MS

Deliver = Callable[[str, str], bool]


@dataclass(frozen=True)
class InboxAlert:
    """One notification the watchdog decided to send."""

    key: str
    kind: str
    text: str


def _minutes(ms: int) -> str:
    return f"{ms / 60_000:.0f} 分钟"


class InboxWatchdog:
    """Edge-triggered mail-collection alerts with durable de-duplication."""

    def __init__(
        self,
        *,
        state_dir: Path,
        deliver: Deliver,
        clock_ms: Callable[[], int],
    ) -> None:
        self._path = Path(state_dir) / STATE_FILE_NAME
        self._deliver = deliver
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._state = self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "reported": {},
        }
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        if not isinstance(raw, dict) or raw.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raise ValueError(f"unreadable inbox watchdog state: {self._path}")
        return raw

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)

    def _send(self, key: str, text: str) -> bool:
        try:
            return bool(self._deliver(key, text))
        except Exception:  # noqa: BLE001 - a failed alert is retried next tick, never raised into the sweep
            return False

    def _due(self, key: str, now_ms: int) -> bool:
        last = self._state["reported"].get(key)
        return not isinstance(last, int) or now_ms - last >= REPEAT_AFTER_MS

    # -- before the deadline ---------------------------------------------

    def observe_unfetched(
        self,
        stats: Iterable[tuple[str, int, int]],
        *,
        is_running: Callable[[str], bool],
    ) -> list[InboxAlert]:
        """Report actors that are RUNNING and have not taken their mail.

        ``stats`` is ``(recipient, count, oldest_received_at_ms)`` over rows
        nobody has fetched.  ``is_running`` decides liveness, because only
        the daemon knows it -- the same split ``prune_outbox`` already uses
        for its address predicates.

        The pairing matters in both directions: mail piling up for an actor
        that is NOT running is an ordinary offline mailbox, and an actor
        that is running with no unfetched mail is simply working.  Only the
        two together describe "alive, and not listening".
        """

        now = self._clock_ms()
        alerts: list[InboxAlert] = []
        with self._lock:
            for recipient, count, oldest_ms in stats:
                age = now - oldest_ms
                if age < UNFETCHED_ALERT_MS or not is_running(recipient):
                    continue
                key = f"unfetched:{recipient}"
                if not self._due(key, now):
                    continue
                text = (
                    f"{recipient} 在跑,但已经 {_minutes(age)} 没有取过邮件:"
                    f"{count} 条待取,最老一条 {_minutes(age)} 前到达。"
                    f"邮件会在到达后 {_minutes(DEFAULT_HOLD_TTL_MS)} 到期,"
                    "到点就没了 —— 现在还读得到。"
                )
                if self._send(key, text):
                    self._state["reported"][key] = now
                    alerts.append(InboxAlert(key=key, kind="unfetched", text=text))
            if alerts:
                self._save()
        return alerts

    # -- after the deadline ----------------------------------------------

    def observe_pruned(
        self,
        items: Iterable[tuple[str, str]],
        *,
        is_running: Callable[[str], bool],
    ) -> list[InboxAlert]:
        """Report mail thrown away while its recipient was running.

        ``items`` is ``(recipient, message_id)`` for rows the TTL sweep just
        evicted.  A recipient that is not running is the population the
        sweep exists for (a decommissioned actor, a renamed node); one that
        IS running means a live reader lost a message, which is the event
        nobody could see on 2026-09-18.
        """

        now = self._clock_ms()
        lost: dict[str, list[str]] = {}
        for recipient, message_id in items:
            if is_running(recipient):
                lost.setdefault(recipient, []).append(message_id)
        alerts: list[InboxAlert] = []
        with self._lock:
            for recipient, message_ids in lost.items():
                key = f"pruned:{recipient}"
                text = (
                    f"{recipient} 在跑,而它的 {len(message_ids)} 条邮件已经到期被丢弃"
                    f"(最早一条 id {message_ids[0]})。"
                    "⛔ 这些内容不会再出现,发件方那边看到的仍是「已投递」。"
                )
                if self._send(key, text):
                    self._state["reported"][key] = now
                    alerts.append(InboxAlert(key=key, kind="pruned", text=text))
            if alerts:
                self._save()
        return alerts


__all__ = [
    "REPEAT_AFTER_MS",
    "STATE_FILE_NAME",
    "UNFETCHED_ALERT_MS",
    "Deliver",
    "InboxAlert",
    "InboxWatchdog",
]
