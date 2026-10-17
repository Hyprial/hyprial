"""Sustained rate detection over a generation's cumulative write bytes."""
from __future__ import annotations


class WriteRateWindow:
    def __init__(self, *, bytes_per_second=50 * 1024 * 1024, sustain_seconds=300):
        self._threshold = bytes_per_second
        self._sustain = sustain_seconds
        self._last: tuple[float, int] | None = None
        self._above_since: float | None = None
        self._alarming = False
        self._rate: float | None = None

    def observe(self, total: int, now: float) -> str | None:
        last = self._last
        self._last = (now, total)
        if last is None:
            return None
        elapsed, written = now - last[0], total - last[1]
        if elapsed <= 0:
            return None
        self._rate = max(0, written) / elapsed
        if written < 0 or self._rate <= self._threshold:
            self._above_since = None
            was_alarming, self._alarming = self._alarming, False
            return "recovered" if was_alarming else None
        if self._above_since is None:
            self._above_since = last[0]
        if now - self._above_since > self._sustain and not self._alarming:
            self._alarming = True
            return "high"
        return None

    def status(self) -> dict:
        return {"scope": "orphan-custody", "bytesPerSecond": self._rate,
                "thresholdBytesPerSecond": self._threshold,
                "sustainSeconds": self._sustain, "aboveSince": self._above_since,
                "alarming": self._alarming}
