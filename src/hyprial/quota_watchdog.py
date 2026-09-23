"""Quota watchdog: tell the owner in time to switch accounts.

Allen, 2026-09-17: 「不应该对额度耗尽的agent做单独处理，保持现状即可。实际上，
应该在修复top之后上线看门狗及时提醒用户切换」.  So this module only NOTIFIES;
it never changes how an exhausted agent's turns are settled.

Two signals, one recipient (the daemon owner, through the Squire user-delivery
path):

- **Readings** -- every ``UsageCache`` refresh.  Per account window
  (``source/window id``) the watchdog moves between ``normal``, ``warning``
  (used/limit >= ``WARN_RATIO``) and ``exhausted`` (>= 1.0), and says so once
  per transition kind per window.  A window is identified by its reset
  instant, so the next window starts clean; falling back below
  ``WARN_RATIO`` after a warning or exhaustion is reported once as
  ``recovered``.
- **Turn failures** -- a harness turn classified ``PROVIDER_USAGE_LIMIT``.  The
  failing turn does not say which account it used (``HarnessResult`` carries
  no harness or model vendor), so the alert names the agent and attaches the
  current readings of every account instead of guessing a mapping.  At most
  one such alert per ``FAILURE_HOLD_MS``; agents first seen during a hold go
  into the next one, and an agent already named is not named again for
  ``FAILURE_REPORTED_TTL_MS``.

"Account" means the credential files the daemon process can read under its
own home -- the only accounts the usage cache sees.

State lives in one JSON file next to the daemon state so a daemon restart
(autoupdate restarts it daily) neither repeats nor forgets an alert.  A
delivery that did not go through is retried on the next evaluation with a
fresh idempotency key: the Squire ledger records failures too, so reusing
the key would replay the failure instead of retrying.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hyprial.usage import SourceSnapshot

WARN_RATIO = 0.9
FAILURE_HOLD_MS = 30 * 60_000
#: An agent reported for a usage-limit failure is not named again for this
#: long -- one Claude session window -- so a still-exhausted agent whose
#: next turns keep failing does not reappear in every hold.
FAILURE_REPORTED_TTL_MS = 5 * 60 * 60_000
STATE_FILE_NAME = "quota-watchdog.json"
STATE_SCHEMA_VERSION = 1

LEVEL_NORMAL = "normal"
LEVEL_WARNING = "warning"
LEVEL_EXHAUSTED = "exhausted"

KIND_WARNING = "warning"
KIND_EXHAUSTED = "exhausted"
KIND_RECOVERED = "recovered"

#: ``deliver(idempotency_key, text) -> accepted``
Deliver = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class QuotaAlert:
    """One notification the watchdog decided to send."""

    key: str
    kind: str
    text: str


def _level(used: float, limit: float) -> str | None:
    if limit <= 0:
        return None
    ratio = used / limit
    if ratio >= 1.0:
        return LEVEL_EXHAUSTED
    if ratio >= WARN_RATIO:
        return LEVEL_WARNING
    return LEVEL_NORMAL


def _format_reset(resets_at_ms: int | None) -> str:
    if resets_at_ms is None:
        return "重置时间未知"
    moment = datetime.fromtimestamp(resets_at_ms / 1000).astimezone()
    return f"{moment:%m-%d %H:%M} 重置"


def _format_amount(used: float, limit: float) -> str:
    percent = used / limit * 100
    if limit == 100:
        return f"{percent:.0f}%"
    return f"{used:g}/{limit:g} ({percent:.0f}%)"


def _readings_lines(snapshots: Iterable[SourceSnapshot]) -> list[str]:
    lines: list[str] = []
    for snapshot in snapshots:
        if not snapshot.ok:
            lines.append(f"- {snapshot.source}: 读不到({snapshot.reason or '原因未知'})")
            continue
        parts = [
            f"{window.label} {_format_amount(window.used, window.limit)}"
            for window in snapshot.windows
            if window.limit > 0
        ]
        lines.append(f"- {snapshot.source}: " + ("; ".join(parts) if parts else "无窗口数据"))
    return lines


def _headroom(snapshots: Iterable[SourceSnapshot], exclude: str) -> list[str]:
    """Accounts whose every known window is still below the warning line."""

    names: list[str] = []
    for snapshot in snapshots:
        if snapshot.source == exclude or not snapshot.ok or not snapshot.windows:
            continue
        levels = {_level(window.used, window.limit) for window in snapshot.windows}
        if levels <= {LEVEL_NORMAL, None} and LEVEL_NORMAL in levels:
            names.append(snapshot.source)
    return names


class QuotaWatchdog:
    """Edge-triggered quota alerts with durable de-duplication."""

    def __init__(
        self,
        *,
        state_dir: Path,
        deliver: Deliver,
        readings: Callable[[], tuple[SourceSnapshot, ...]],
        clock_ms: Callable[[], int],
    ) -> None:
        self._path = Path(state_dir) / STATE_FILE_NAME
        self._deliver = deliver
        self._readings = readings
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._state = self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "windows": {},
            "failure": {"holdUntilMs": 0, "reported": {}, "pending": [], "attempt": 0},
        }
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        if not isinstance(raw, dict) or raw.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raise ValueError(f"unreadable quota watchdog state: {self._path}")
        return raw

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)

    # -- delivery --------------------------------------------------------

    def _send(self, base_key: str, attempt: int, text: str) -> bool:
        key = base_key if attempt == 0 else f"{base_key}:retry-{attempt}"
        try:
            return bool(self._deliver(key, text))
        except Exception:  # noqa: BLE001 - a delivery failure is retried, never raised into the refresher
            return False

    # -- readings --------------------------------------------------------

    def observe_readings(self) -> list[QuotaAlert]:
        """Evaluate the cache once; returns the alerts that were delivered."""

        snapshots = self._readings()
        delivered: list[QuotaAlert] = []
        with self._lock:
            windows: dict[str, Any] = self._state["windows"]
            for snapshot in snapshots:
                if not snapshot.ok:
                    continue
                for window in snapshot.windows:
                    level = _level(window.used, window.limit)
                    if level is None:
                        continue
                    window_key = f"{snapshot.source}/{window.id}"
                    entry = windows.get(window_key)
                    new_window = (
                        entry is not None
                        and entry.get("resetsAtMs") != window.resets_at_ms
                    )
                    if entry is None or new_window:
                        # Kinds already said belong to the old window.  The
                        # level carries over so a drop below the line is
                        # still reported as a recovery.
                        entry = {
                            "resetsAtMs": window.resets_at_ms,
                            "level": LEVEL_NORMAL if entry is None else entry["level"],
                            "sent": [],
                            "attempts": {},
                        }
                        windows[window_key] = entry
                    kind = self._transition(entry["level"], level)
                    if kind is None and new_window and level != LEVEL_NORMAL:
                        # Still high in a NEW window (no reading caught the
                        # low point): this window has not been announced.
                        kind = self._transition(LEVEL_NORMAL, level)
                    if kind is None or kind in entry["sent"]:
                        if kind is None:
                            entry["level"] = level
                        continue
                    text = self._window_text(kind, snapshot, window, snapshots)
                    base_key = (
                        f"quota-watchdog:{window_key}:{window.resets_at_ms}:{kind}"
                    )
                    attempt = int(entry["attempts"].get(kind, 0))
                    if self._send(base_key, attempt, text):
                        entry["sent"].append(kind)
                        entry["level"] = level
                        entry["attempts"].pop(kind, None)
                        delivered.append(QuotaAlert(base_key, kind, text))
                    else:
                        # Keep the previous level so the transition is seen
                        # again next time; the retry uses a fresh key.
                        entry["attempts"][kind] = attempt + 1
            self._save()
        return delivered

    @staticmethod
    def _transition(previous: str, current: str) -> str | None:
        if current == LEVEL_EXHAUSTED and previous != LEVEL_EXHAUSTED:
            return KIND_EXHAUSTED
        if current == LEVEL_WARNING and previous == LEVEL_NORMAL:
            return KIND_WARNING
        if current == LEVEL_NORMAL and previous in {LEVEL_WARNING, LEVEL_EXHAUSTED}:
            return KIND_RECOVERED
        return None

    @staticmethod
    def _window_text(
        kind: str,
        snapshot: SourceSnapshot,
        window: Any,
        snapshots: tuple[SourceSnapshot, ...],
    ) -> str:
        amount = _format_amount(window.used, window.limit)
        reset = _format_reset(window.resets_at_ms)
        name = f"{snapshot.source} 账号 {window.label} 窗口"
        if kind == KIND_RECOVERED:
            return f"额度恢复:{name}已回到 {amount}。"
        headline = (
            f"额度耗尽:{name}已用 {amount},{reset}。"
            if kind == KIND_EXHAUSTED
            else f"额度预警:{name}已用 {amount},{reset}。"
        )
        headroom = _headroom(snapshots, exclude=snapshot.source)
        advice = (
            "还有余量的账号:" + "、".join(headroom) + "。"
            if headroom
            else "其他账号目前没有确认有余量的。"
        )
        return headline + advice

    # -- turn failures ---------------------------------------------------

    def observe_usage_limit_failure(self, actor: str) -> QuotaAlert | None:
        """Record one ``PROVIDER_USAGE_LIMIT`` turn; alert unless held."""

        now = self._clock_ms()
        with self._lock:
            failure: dict[str, Any] = self._state["failure"]
            reported_at = failure["reported"].get(actor)
            recently_reported = (
                reported_at is not None and now - int(reported_at) < FAILURE_REPORTED_TTL_MS
            )
            if not recently_reported and actor not in failure["pending"]:
                failure["pending"].append(actor)
            alert = self._flush_failures(now)
            self._save()
        return alert

    def flush_failures(self) -> QuotaAlert | None:
        """Send held agents once the hold has passed (called on refresh)."""

        with self._lock:
            alert = self._flush_failures(self._clock_ms())
            self._save()
        return alert

    def _flush_failures(self, now: int) -> QuotaAlert | None:
        failure: dict[str, Any] = self._state["failure"]
        if not failure["pending"] or now < int(failure["holdUntilMs"]):
            return None
        actors = list(failure["pending"])
        lines = [
            "额度耗尽:以下 agent 的 turn 报了 PROVIDER_USAGE_LIMIT:",
            *(f"- {actor}" for actor in actors),
            "当前各账号读数:",
            *_readings_lines(self._readings()),
        ]
        text = "\n".join(lines)
        base_key = f"quota-watchdog:turn-failure:{now}"
        attempt = int(failure["attempt"])
        if not self._send(base_key, attempt, text):
            failure["attempt"] = attempt + 1
            return None
        failure["reported"] = {
            name: at
            for name, at in failure["reported"].items()
            if now - int(at) < FAILURE_REPORTED_TTL_MS
        } | {name: now for name in actors}
        failure["pending"] = []
        failure["attempt"] = 0
        failure["holdUntilMs"] = now + FAILURE_HOLD_MS
        return QuotaAlert(base_key, KIND_EXHAUSTED, text)


__all__ = [
    "FAILURE_HOLD_MS",
    "FAILURE_REPORTED_TTL_MS",
    "QuotaAlert",
    "QuotaWatchdog",
    "STATE_FILE_NAME",
    "WARN_RATIO",
]
