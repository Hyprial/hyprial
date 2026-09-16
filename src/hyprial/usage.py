"""Subscription quota collection for ``hyprial top`` (background-refreshed, cache-read).

Design contract (all four are load-bearing):

- The network is NEVER touched on the ``top.snapshot`` path.  A daemon-owned
  background thread refreshes every ``REFRESH_INTERVAL_SECONDS``; the IPC
  handler reads an in-memory cache.  The fastest source measures ~0.7s and
  kimi ~3s median, so a synchronous fetch would make ``top`` feel broken.
- Every value carries its fetch instant; the display marks data older than
  ``STALE_MS``.  A quota number without its age is a misleading one.
- Any source failure degrades to ``n/a`` plus a readable reason (missing /
  expired / rejected / network / invalid response) and never blocks the rest.
- Credentials are read from the CLIs' own files on every refresh and never
  written, refreshed, or relayed.  kimi has two independent credential
  sets: pi's own (which pi refreshes itself, so it is fresh exactly when
  kimi workers are running) and the kimi CLI's 900s one; pi's is preferred
  and the CLI's is the fallback.  A refresh hop is deliberately NOT
  implemented either way -- refresh stays with pi, so neither login can be
  broken by a token rotation we failed to write back.

Normalized model: every window stores ``used`` and ``limit`` as two
quantities (the sources disagree on polarity and units; kimi returns
strings whose ``limit`` only happens to equal 100 today).  The display
layer decides how to render the ratio.

Security: token values never appear in logs, errors, snapshots, or tests.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from hyprial.backoff import capped_exponential

JsonObject = dict[str, Any]

REFRESH_INTERVAL_SECONDS = 120.0
HTTP_TIMEOUT_SECONDS = 10.0
STALE_MS = 10 * 60_000

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"

# Stable source order for display and tests.
SOURCES = ("claude", "codex", "kimi")

REASON_MISSING = "credentials missing"
REASON_EXPIRED = "credentials expired"
REASON_NETWORK = "network error"
REASON_INVALID = "invalid response"

# A network-attempted failure backs off exponentially (up to
# MAX_BACKOFF_SECONDS) so a rate-limited or down endpoint is not hammered
# every refresh cycle -- observed live: the claude endpoint answers http 429
# after repeated polling.  Local-only outcomes (missing/expired credentials)
# cost a file read and are retried every cycle on purpose: pi can refresh
# its kimi credential at any time and the next cycle should pick it up.
MAX_BACKOFF_SECONDS = 30 * 60.0
# Claude's usage endpoint answers http 429 (retry-after: 0, no rate-limit
# headers, the same token's messages API fine) once it decides a token is
# polling too hard -- that is ENDPOINT behavior, not a transient network
# fault.  Re-hammering a penalized endpoint at a fixed cadence turns the
# penalty into a steady state, so claude backs off exponentially to a
# one-hour ceiling and keeps serving its last successful reading while
# penalized (age + stale marker stay honest instead of a bare n/a).
# codex/kimi keep the generic path: neither endpoint has shown this
# penalty behavior.
CLAUDE_MAX_BACKOFF_SECONDS = 60 * 60.0


def _is_network_failure(reason: str | None) -> bool:
    if reason is None:
        return False
    return reason in (REASON_NETWORK, REASON_INVALID, "collector error") or (
        reason.startswith("http ") or reason.startswith("credentials rejected")
    )

# Fetcher seam: (url, bearer_token, timeout_seconds) -> decoded JSON object.
# Tests inject fakes; production uses _http_get_json.  The token exists only
# inside the call frame, never in stored state.
Fetcher = Callable[[str, str, float], JsonObject]


def _http_get_json(url: str, token: str, timeout: float) -> JsonObject:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise ValueError("quota endpoint returned a non-object body")
    return body


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    """One quota window, normalized to used/limit quantities."""

    id: str
    label: str
    used: float
    limit: float
    resets_at_ms: int | None = None
    window_seconds: int | None = None
    scope: str | None = None
    severity: str | None = None
    is_active: bool | None = None

    def to_json(self) -> JsonObject:
        return {
            "id": self.id,
            "label": self.label,
            "used": self.used,
            "limit": self.limit,
            "resetsAtMs": self.resets_at_ms,
            "windowSeconds": self.window_seconds,
            "scope": self.scope,
            "severity": self.severity,
            "isActive": self.is_active,
        }


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One source's last collection outcome (success or readable failure)."""

    source: str
    ok: bool
    fetched_at_ms: int
    reason: str | None = None
    windows: tuple[QuotaWindow, ...] = ()
    # Source-specific extras with already-public values only (plan tier,
    # spend/credit fallback state).  Never credentials.
    extra: JsonObject = field(default_factory=dict)
    # True only when this is a cached last-success served during a backoff
    # window: the numbers are real, the age is real, and the data is old --
    # the display must say so instead of pretending it is fresh.
    backing_off: bool = False

    def to_json(self, now_ms: int) -> JsonObject:
        age_ms = max(0, now_ms - self.fetched_at_ms)
        payload: JsonObject = {
            "source": self.source,
            "ok": self.ok,
            "fetchedAtMs": self.fetched_at_ms,
            "ageMs": age_ms,
            "stale": self.backing_off or age_ms > STALE_MS,
            "reason": self.reason,
            "windows": [window.to_json() for window in self.windows],
        }
        if self.backing_off:
            payload["backingOff"] = True
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload


def _read_json_file(path: Path) -> JsonObject | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_reset_ms(value: object) -> int | None:
    """Accept RFC3339 timestamps or epoch seconds; return epoch ms."""

    if isinstance(value, (int, float)) and value > 0:
        # Epoch seconds (codex reset_at) vs epoch milliseconds heuristic.
        return int(value * 1000) if value < 10_000_000_000 else int(value)
    if isinstance(value, str):
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp() * 1000)
    return None


def _float_field(obj: JsonObject, *keys: str) -> float | None:
    """First numeric field among ``keys``; kimi sends numbers as strings."""

    for key in keys:
        value = obj.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def collect_claude(
    credentials_path: Path,
    *,
    now_ms: int,
    fetcher: Fetcher = _http_get_json,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> SourceSnapshot:
    credentials = _read_json_file(credentials_path)
    oauth = (
        credentials.get("claudeAiOauth")
        if isinstance(credentials, dict)
        else None
    )
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        return SourceSnapshot("claude", False, now_ms, reason=REASON_MISSING)
    expires_at = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    if isinstance(expires_at, (int, float)) and expires_at <= now_ms:
        return SourceSnapshot("claude", False, now_ms, reason=REASON_EXPIRED)
    failure, body = _fetch("claude", CLAUDE_USAGE_URL, token, timeout, fetcher, now_ms)
    if failure is not None:
        return failure
    assert body is not None
    windows: list[QuotaWindow] = []
    limits = body.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            percent = _float_field(entry, "percent")
            if not isinstance(kind, str) or percent is None:
                continue
            scope = entry.get("scope")
            scope_label = None
            if isinstance(scope, dict):
                model = scope.get("model")
                if isinstance(model, dict) and isinstance(
                    model.get("display_name"), str
                ):
                    scope_label = model["display_name"]
            windows.append(
                QuotaWindow(
                    id=kind,
                    label=kind,
                    used=percent,
                    limit=100.0,
                    resets_at_ms=_parse_reset_ms(entry.get("resets_at")),
                    scope=scope_label,
                    severity=(
                        entry.get("severity")
                        if isinstance(entry.get("severity"), str)
                        else None
                    ),
                    is_active=(
                        entry.get("is_active")
                        if isinstance(entry.get("is_active"), bool)
                        else None
                    ),
                )
            )
    extra: JsonObject = {}
    spend = body.get("spend")
    if isinstance(spend, dict):
        used = spend.get("used")
        limit = spend.get("limit")
        extra["spend"] = {
            "usedUsd": _money_to_float(used),
            "limitUsd": _money_to_float(limit),
            "enabled": spend.get("enabled") is True,
            "disabledReason": (
                spend.get("disabled_reason")
                if isinstance(spend.get("disabled_reason"), str)
                else None
            ),
        }
    return SourceSnapshot(
        "claude", True, now_ms, windows=tuple(windows), extra=extra
    )


def _money_to_float(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    amount = _float_field(value, "amount_minor")
    exponent = _float_field(value, "exponent")
    if amount is None or exponent is None:
        return None
    return amount / (10**exponent)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _jwt_expiry_ms(token: str) -> int | None:
    """Read ``exp`` from a JWT payload without verifying the signature.

    This is an expiry pre-check so a dead token reports "credentials
    expired" instead of a network round trip; it never authorizes anything.
    """

    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    return int(exp) * 1000 if isinstance(exp, (int, float)) else None


def _codex_window(window_id: str, label: str, body: JsonObject) -> QuotaWindow | None:
    primary = body.get("primary_window")
    if not isinstance(primary, dict):
        return None
    used = _float_field(primary, "used_percent")
    if used is None:
        return None
    window_seconds = primary.get("limit_window_seconds")
    return QuotaWindow(
        id=window_id,
        label=label,
        used=used,
        limit=100.0,
        resets_at_ms=_parse_reset_ms(primary.get("reset_at")),
        window_seconds=(
            int(window_seconds) if isinstance(window_seconds, (int, float)) else None
        ),
    )


def collect_codex(
    auth_path: Path,
    *,
    now_ms: int,
    fetcher: Fetcher = _http_get_json,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> SourceSnapshot:
    auth = _read_json_file(auth_path)
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token:
        return SourceSnapshot("codex", False, now_ms, reason=REASON_MISSING)
    expiry_ms = _jwt_expiry_ms(token)
    if expiry_ms is not None and expiry_ms <= now_ms:
        return SourceSnapshot("codex", False, now_ms, reason=REASON_EXPIRED)
    failure, body = _fetch("codex", CODEX_USAGE_URL, token, timeout, fetcher, now_ms)
    if failure is not None:
        return failure
    assert body is not None
    windows: list[QuotaWindow] = []
    rate_limit = body.get("rate_limit")
    if isinstance(rate_limit, dict):
        primary = _codex_window("primary", "weekly", rate_limit)
        if primary is not None:
            windows.append(primary)
    additional = body.get("additional_rate_limits")
    if isinstance(additional, list):
        for entry in additional:
            if not isinstance(entry, dict):
                continue
            name = entry.get("limit_name")
            limit_body = entry.get("rate_limit")
            if not isinstance(name, str) or not isinstance(limit_body, dict):
                continue
            window = _codex_window(name, name, limit_body)
            if window is not None:
                windows.append(window)
    extra: JsonObject = {}
    plan = body.get("plan_type")
    if isinstance(plan, str):
        extra["planType"] = plan
    credits = body.get("credits")
    if isinstance(credits, dict):
        extra["credits"] = {
            "hasCredits": credits.get("has_credits") is True,
            "unlimited": credits.get("unlimited") is True,
            "balance": (
                credits.get("balance")
                if isinstance(credits.get("balance"), str)
                else None
            ),
        }
    return SourceSnapshot(
        "codex", True, now_ms, windows=tuple(windows), extra=extra
    )


# ---------------------------------------------------------------------------
# Kimi
# ---------------------------------------------------------------------------

# Two independent credential sets exist on a pi-managed machine, refreshed
# independently: pi keeps its own (self-refreshing while any kimi worker
# runs -- exactly the population `hyprial top` observes), the kimi CLI keeps
# its 900s one.  Prefer pi's; fall back to the CLI's; only when both are
# expired does the source read n/a.  The field names differ between the two
# files, and pi's `expires` has been observed in MILLISECONDS -- normalize
# before comparing.
_KIMI_EXPIRY_MS_THRESHOLD = 100_000_000_000  # > this => milliseconds


def _expiry_to_ms(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0:
        return None
    return float(value) if value > _KIMI_EXPIRY_MS_THRESHOLD else float(value) * 1000


def _kimi_cli_credential(path: Path) -> tuple[str, float | None] | None:
    data = _read_json_file(path)
    if data is None:
        return None
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    return token, _expiry_to_ms(data.get("expires_at"))


def _kimi_pi_credential(path: Path) -> tuple[str, float | None] | None:
    data = _read_json_file(path)
    if data is None:
        return None
    entry = data.get("kimi-coding")
    if not isinstance(entry, dict):
        return None
    token = entry.get("access")
    if not isinstance(token, str) or not token:
        return None
    return token, _expiry_to_ms(entry.get("expires"))


def _select_kimi_credential(
    credentials: tuple[tuple[Path, Callable[[Path], tuple[str, float | None] | None]], ...],
    now_ms: int,
) -> tuple[str | None, str | None]:
    """First fresh credential wins; report why when none does.

    Returns ``(token, None)`` on success, ``(None, reason)`` otherwise.  A
    credential without a readable expiry is attempted anyway -- a wrong
    guess degrades through the normal 401 path instead of hiding data.
    """

    saw_expired = False
    for path, parser in credentials:
        parsed = parser(path)
        if parsed is None:
            continue
        token, expires_ms = parsed
        if expires_ms is None or expires_ms > now_ms:
            return token, None
        saw_expired = True
    return None, REASON_EXPIRED if saw_expired else REASON_MISSING


def _kimi_usage_window(
    window_id: str, label: str, usage: JsonObject, window_seconds: int | None
) -> QuotaWindow | None:
    used = _float_field(usage, "used")
    limit = _float_field(usage, "limit")
    if used is None or limit is None or limit <= 0:
        return None
    return QuotaWindow(
        id=window_id,
        label=label,
        used=used,
        limit=limit,
        resets_at_ms=_parse_reset_ms(
            usage.get(
                "resetTime",
                usage.get("reset_at", usage.get("resetAt", usage.get("resets_at"))),
            )
        ),
        window_seconds=window_seconds,
    )


def collect_kimi(
    *,
    now_ms: int,
    pi_auth_path: Path | None = None,
    cli_credentials_path: Path | None = None,
    fetcher: Fetcher = _http_get_json,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> SourceSnapshot:
    """Kimi Coding subscription windows (weekly + 5h).

    Credential preference order: the pi-side credential first (pi refreshes
    it itself, so it is fresh exactly when kimi workers are running), then
    the kimi CLI's own 900s file.  hyprial only reads -- refresh stays with pi,
    never with us, so the kimi CLI's login cannot be broken by a refresh we
    failed to write back.
    """

    candidates: list[
        tuple[Path, Callable[[Path], tuple[str, float | None] | None]]
    ] = []
    if pi_auth_path is not None:
        candidates.append((pi_auth_path, _kimi_pi_credential))
    if cli_credentials_path is not None:
        candidates.append((cli_credentials_path, _kimi_cli_credential))
    token, reason = _select_kimi_credential(tuple(candidates), now_ms)
    if token is None:
        assert reason is not None
        return SourceSnapshot("kimi", False, now_ms, reason=reason)
    failure, body = _fetch("kimi", KIMI_USAGE_URL, token, timeout, fetcher, now_ms)
    if failure is not None:
        return failure
    assert body is not None
    windows: list[QuotaWindow] = []
    # Structure convention (from the 2026-08-18 probe): the WEEKLY window is
    # the top-level "usage" object, which carries no duration field; only the
    # shorter windows in "limits" describe their window explicitly
    # (window = {duration: 300, timeUnit: "TIME_UNIT_MINUTE"} for 5h).
    # "top-level means weekly" is an inference from that probe, not a
    # documented contract -- keep this comment with the code.
    top_usage = body.get("usage")
    if isinstance(top_usage, dict):
        weekly = _kimi_usage_window("weekly", "weekly", top_usage, None)
        if weekly is not None:
            windows.append(weekly)
    limits = body.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            window = entry.get("window")
            # Live shape: the numbers sit under "detail" ("usage" and the
            # entry itself are accepted as fallbacks).
            usage = entry.get("detail")
            if not isinstance(usage, dict):
                usage = entry.get("usage")
            if not isinstance(usage, dict):
                usage = entry
            window_seconds: int | None = None
            if isinstance(window, dict):
                duration = window.get("duration")
                unit = window.get("timeUnit")
                if isinstance(duration, (int, float)):
                    multiplier = 1.0
                    if unit == "TIME_UNIT_MINUTE":
                        multiplier = 60.0
                    elif unit == "TIME_UNIT_HOUR":
                        multiplier = 3600.0
                    elif unit == "TIME_UNIT_DAY":
                        multiplier = 86400.0
                    window_seconds = int(duration * multiplier)
            label = (
                f"{window_seconds // 3600}h"
                if window_seconds is not None and window_seconds % 3600 == 0
                else None
            )
            parsed = _kimi_usage_window(
                label or "window", label or "window", usage, window_seconds
            )
            if parsed is not None:
                windows.append(parsed)
    return SourceSnapshot(
        "kimi", True, now_ms, windows=tuple(windows)
    )


# ---------------------------------------------------------------------------
# Shared fetch + cache
# ---------------------------------------------------------------------------


def _fetch(
    source: str,
    url: str,
    token: str,
    timeout: float,
    fetcher: Fetcher,
    now_ms: int,
) -> tuple[SourceSnapshot | None, JsonObject | None]:
    """Run one HTTP fetch, mapping every failure to a readable reason.

    Error messages deliberately carry only categories (HTTP status, error
    class) -- never response bodies or request details, which could echo
    credential material.
    """

    try:
        return None, fetcher(url, token, timeout)
    except urllib.error.HTTPError as error:
        reason = (
            "credentials rejected (http 401)"
            if error.code in (401, 403)
            else f"http {error.code}"
        )
        return SourceSnapshot(source, False, now_ms, reason=reason), None
    except (urllib.error.URLError, TimeoutError, OSError):
        return SourceSnapshot(source, False, now_ms, reason=REASON_NETWORK), None
    except (ValueError, json.JSONDecodeError):
        return SourceSnapshot(source, False, now_ms, reason=REASON_INVALID), None


class UsageCache:
    """Daemon-owned background refresher; ``top.snapshot`` only reads it."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        refresh_interval_seconds: float = REFRESH_INTERVAL_SECONDS,
        fetcher: Fetcher = _http_get_json,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh interval must be positive")
        self._home = Path.home() if home is None else Path(home)
        self._refresh_interval_seconds = refresh_interval_seconds
        self._fetcher = fetcher
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshots: dict[str, SourceSnapshot] = {}
        # Backoff bookkeeping, touched only by the refresh thread.
        self._failures: dict[str, int] = {}
        self._retry_after_ms: dict[str, int] = {}
        # Last successful claude reading, served during a backoff window so
        # top keeps showing real numbers (age + stale stay honest) instead of
        # n/a while the endpoint is penalized.  claude-only: the other
        # sources do not show the 429-penalty behavior.
        self._last_success: dict[str, SourceSnapshot] = {}

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="hyprial-usage-refresh",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh_once()
            self._stop.wait(self._refresh_interval_seconds)

    def refresh_once(self) -> None:
        """Collect every source once; a failed source never blocks the rest."""

        now_ms = self._now_ms()
        results: dict[str, SourceSnapshot] = {}
        home = self._home
        collectors: tuple[tuple[str, Callable[[], SourceSnapshot]], ...] = (
            (
                "claude",
                lambda: collect_claude(
                    home / ".claude" / ".credentials.json",
                    now_ms=now_ms,
                    fetcher=self._fetcher,
                ),
            ),
            (
                "codex",
                lambda: collect_codex(
                    home / ".codex" / "auth.json",
                    now_ms=now_ms,
                    fetcher=self._fetcher,
                ),
            ),
            (
                "kimi",
                lambda: collect_kimi(
                    pi_auth_path=home / "my-pi-setup" / "agent" / "auth.json",
                    cli_credentials_path=(
                        home / ".kimi-code" / "credentials" / "kimi-code.json"
                    ),
                    now_ms=now_ms,
                    fetcher=self._fetcher,
                ),
            ),
        )
        for source, collect in collectors:
            previous = self._snapshots.get(source)
            if (
                previous is not None
                and not previous.ok
                and _is_network_failure(previous.reason)
                and now_ms < self._retry_after_ms.get(source, 0)
            ):
                # Backing off: keep the last known outcome (its age/stale
                # marker stays honest) instead of re-hammering the endpoint.
                if source == "claude":
                    cached = self._last_success.get(source)
                    if cached is not None:
                        # A penalized endpoint is not a dead source: the last
                        # successful reading is still the most honest answer
                        # top can give, clearly marked as a backoff serve.
                        results[source] = replace(cached, backing_off=True)
                        continue
                results[source] = previous
                continue
            try:
                snapshot = collect()
            except Exception:  # noqa: BLE001 - a collector bug must not kill the loop
                snapshot = SourceSnapshot(
                    source, False, now_ms, reason="collector error"
                )
            results[source] = snapshot
            if snapshot.ok and source == "claude":
                self._last_success[source] = snapshot
            if not snapshot.ok and _is_network_failure(snapshot.reason):
                failures = self._failures.get(source, 0) + 1
                self._failures[source] = failures
                cap = (
                    CLAUDE_MAX_BACKOFF_SECONDS
                    if source == "claude"
                    else MAX_BACKOFF_SECONDS
                )
                delay = min(
                    capped_exponential(self._refresh_interval_seconds, cap, failures - 1),
                    cap,
                )
                self._retry_after_ms[source] = now_ms + int(delay * 1000)
            else:
                self._failures.pop(source, None)
                self._retry_after_ms.pop(source, None)
        with self._lock:
            self._snapshots = results

    def snapshot_payload(self, now_ms: int | None = None) -> JsonObject:
        """Cache-only view for ``top.snapshot``; never triggers a fetch."""

        now = self._now_ms() if now_ms is None else now_ms
        with self._lock:
            snapshots = dict(self._snapshots)
        return {
            "sources": [
                (
                    snapshots[source].to_json(now)
                    if source in snapshots
                    else SourceSnapshot(
                        source, False, now, reason="not yet fetched"
                    ).to_json(now)
                )
                for source in SOURCES
            ]
        }


def usage_collection_disabled(environ: dict[str, str] | None = None) -> bool:
    """Ops kill-switch: ``HYPRIAL_USAGE_DISABLE=1`` skips collection entirely."""

    env = os.environ if environ is None else environ
    return env.get("HYPRIAL_USAGE_DISABLE", "").strip() in {"1", "true", "yes"}
