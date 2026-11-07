"""Usage/quota collection for ``hyprial agent top``: parsing, normalization, degradation.

Fixture bodies mirror the live 2026-08-18 probe captures; all three shapes
(claude, codex, kimi) were re-verified against the real endpoints during
implementation.  All tokens in this file are fake.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import base64
import json
import urllib.error
from pathlib import Path

import pytest

import hyprial.biz.impl.usage as usage
from hyprial.biz.impl.usage import (
    UsageCache,
    collect_claude,
    collect_codex,
    collect_kimi,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

NOW_MS = 1_787_043_000_000
FIXTURE_TOKEN = "fixture-token-not-real"


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _jwt(exp_seconds: int) -> str:
    def segment(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'exp': exp_seconds})}.fixture"


# Live-shaped fixtures (values from the 2026-08-18 probe, no secrets).

CLAUDE_BODY = {
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 12,
            "severity": "normal",
            "resets_at": "2026-08-18T09:10:00.183541+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 80,
            "severity": "warning",
            "resets_at": "2026-08-18T22:00:00.183561+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 100,
            "severity": "critical",
            "resets_at": "2026-08-18T22:00:00.183758+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
            "is_active": True,
        },
    ],
    "spend": {
        "used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
        "limit": {"amount_minor": 20000, "currency": "USD", "exponent": 2},
        "enabled": False,
        "disabled_reason": "out_of_credits",
    },
}

CODEX_BODY = {
    "plan_type": "pro",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 84,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 262935,
            "reset_at": 1787306352,
        },
        "secondary_window": None,
    },
    "additional_rate_limits": [
        {
            "limit_name": "GPT-5.3-Codex-Spark",
            "metered_feature": "codex_bengalfox",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 604800,
                    "reset_at": 1787648218,
                },
                "secondary_window": None,
            },
        }
    ],
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
}

# Live-verified shape (2026-08-18): the top-level "usage" object has no
# duration field and is the weekly window; "limits" entries carry explicit
# windows with numbers under "detail".  limit is deliberately NOT 100 to
# catch percentage hardcoding.
KIMI_BODY = {
    "usage": {
        "used": "125",
        "remaining": "125",
        "limit": "250",
        "resetTime": "2026-08-21T00:42:50.516545Z",
    },
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {
                "used": "10",
                "remaining": "90",
                "limit": "100",
                "resetTime": "2026-08-18T12:42:50.516545Z",
            },
        }
    ],
}


def _claude_credentials(tmp_path: Path, *, expires_at_ms: int = NOW_MS + 3_600_000) -> Path:
    return _write(
        tmp_path / ".claude" / ".credentials.json",
        {
            "claudeAiOauth": {
                "accessToken": FIXTURE_TOKEN,
                "expiresAt": expires_at_ms,
            }
        },
    )


def _codex_credentials(tmp_path: Path, *, exp_seconds: int = NOW_MS // 1000 + 864_000) -> Path:
    return _write(
        tmp_path / ".codex" / "auth.json",
        {"tokens": {"access_token": _jwt(exp_seconds)}},
    )


def _kimi_credentials(tmp_path: Path, *, expires_at: int = NOW_MS // 1000 + 900) -> Path:
    return _write(
        tmp_path / ".kimi-code" / "credentials" / "kimi-code.json",
        {"access_token": FIXTURE_TOKEN, "expires_at": expires_at, "expires_in": 900},
    )


def _kimi_pi_auth(
    tmp_path: Path,
    *,
    expires: int = NOW_MS + 900_000,  # pi stores MILLISECONDS
    token: str = "fixture-pi-token-not-real",
) -> Path:
    return _write(
        tmp_path / "my-pi-setup" / "agent" / "auth.json",
        {
            "kimi-coding": {
                "type": "oauth",
                "access": token,
                "refresh": "fixture-refresh-not-real",
                "expires": expires,
            }
        },
    )


def _ok_fetcher(body: dict[str, object]):
    def fetch(url: str, token: str, timeout: float) -> dict[str, object]:
        return dict(body)

    return fetch


# ---------------------------------------------------------------------------
# Parsing / normalization
# ---------------------------------------------------------------------------


def test_claude_windows_include_scoped_weekly_with_model_scope(tmp_path: Path) -> None:
    snapshot = collect_claude(
        _claude_credentials(tmp_path), now_ms=NOW_MS, fetcher=_ok_fetcher(CLAUDE_BODY)
    )

    assert snapshot.ok is True
    by_id = {window.id: window for window in snapshot.windows}
    assert set(by_id) == {"session", "weekly_all", "weekly_scoped"}
    scoped = by_id["weekly_scoped"]
    assert scoped.scope == "Fable"
    assert scoped.severity == "critical"
    assert scoped.is_active is True
    assert scoped.used == 100.0 and scoped.limit == 100.0
    assert by_id["weekly_all"].severity == "warning"
    spend = snapshot.extra["spend"]
    assert spend["usedUsd"] == 0.0
    assert spend["limitUsd"] == 200.0
    assert spend["enabled"] is False
    assert spend["disabledReason"] == "out_of_credits"


def test_codex_windows_cover_primary_and_spark(tmp_path: Path) -> None:
    snapshot = collect_codex(
        _codex_credentials(tmp_path), now_ms=NOW_MS, fetcher=_ok_fetcher(CODEX_BODY)
    )

    assert snapshot.ok is True
    by_id = {window.id: window for window in snapshot.windows}
    assert set(by_id) == {"primary", "GPT-5.3-Codex-Spark"}
    primary = by_id["primary"]
    assert primary.used == 84.0 and primary.limit == 100.0
    assert primary.resets_at_ms == 1787306352 * 1000
    assert primary.window_seconds == 604800
    spark = by_id["GPT-5.3-Codex-Spark"]
    assert spark.used == 0.0
    # Spark's floating reset_at (window not started) is passed through as-is.
    assert spark.resets_at_ms == 1787648218 * 1000
    assert snapshot.extra["planType"] == "pro"
    assert snapshot.extra["credits"] == {
        "hasCredits": False,
        "unlimited": False,
        "balance": "0",
    }


def test_kimi_ratio_uses_real_limit_not_a_hardcoded_100(tmp_path: Path) -> None:
    """Regression guard: kimi returns used/limit as strings and today's
    limit=100 only LOOKS like a percentage.  This fixture's limit is 250."""

    snapshot = collect_kimi(
        cli_credentials_path=_kimi_credentials(tmp_path),
        now_ms=NOW_MS,
        fetcher=_ok_fetcher(KIMI_BODY),
    )

    assert snapshot.ok is True
    by_id = {window.id: window for window in snapshot.windows}
    weekly = by_id["weekly"]
    assert weekly.used == 125.0
    assert weekly.limit == 250.0  # NOT coerced to 100
    assert weekly.used / weekly.limit == pytest.approx(0.5)
    five_hour = by_id["5h"]
    assert five_hour.used == 10.0 and five_hour.limit == 100.0
    assert five_hour.window_seconds == 18_000
    assert five_hour.resets_at_ms is not None
    assert weekly.resets_at_ms is not None


# ---------------------------------------------------------------------------
# Kimi credential selection: pi's self-refreshing set first, kimi CLI's as
# the fallback, n/a only when both are expired.
# ---------------------------------------------------------------------------


def _recording_fetcher(body: dict[str, object], seen: list[str]):
    def fetch(url: str, token: str, timeout: float) -> dict[str, object]:
        seen.append(token)
        return dict(body)

    return fetch


def test_kimi_prefers_the_pi_credential_when_fresh(tmp_path: Path) -> None:
    seen: list[str] = []
    snapshot = collect_kimi(
        pi_auth_path=_kimi_pi_auth(tmp_path),
        cli_credentials_path=_kimi_credentials(tmp_path),
        now_ms=NOW_MS,
        fetcher=_recording_fetcher(KIMI_BODY, seen),
    )
    assert snapshot.ok is True
    assert seen == ["fixture-pi-token-not-real"]


def test_kimi_falls_back_to_the_cli_credential(tmp_path: Path) -> None:
    seen: list[str] = []
    snapshot = collect_kimi(
        pi_auth_path=_kimi_pi_auth(tmp_path, expires=NOW_MS - 1),
        cli_credentials_path=_kimi_credentials(tmp_path),
        now_ms=NOW_MS,
        fetcher=_recording_fetcher(KIMI_BODY, seen),
    )
    assert snapshot.ok is True
    assert seen == [FIXTURE_TOKEN]


def test_kimi_na_only_when_both_credentials_are_expired(tmp_path: Path) -> None:
    def forbidden_fetch(url: str, token: str, timeout: float) -> None:
        raise AssertionError("must not fetch with expired credentials")

    snapshot = collect_kimi(
        pi_auth_path=_kimi_pi_auth(tmp_path, expires=NOW_MS - 1),
        cli_credentials_path=_kimi_credentials(tmp_path, expires_at=NOW_MS // 1000 - 1),
        now_ms=NOW_MS,
        fetcher=forbidden_fetch,
    )
    assert snapshot.ok is False
    assert snapshot.reason == "credentials expired"


def test_kimi_expiry_accepts_milliseconds_and_seconds(tmp_path: Path) -> None:
    """pi's `expires` has been observed in milliseconds; the CLI's
    `expires_at` is seconds.  Both units must be judged correctly."""

    # pi, milliseconds, fresh
    assert collect_kimi(
        pi_auth_path=_kimi_pi_auth(tmp_path / "a", expires=(NOW_MS + 900_000)),
        now_ms=NOW_MS,
        fetcher=_ok_fetcher(KIMI_BODY),
    ).ok is True
    # pi, milliseconds, expired
    assert collect_kimi(
        pi_auth_path=_kimi_pi_auth(tmp_path / "b", expires=(NOW_MS - 1)),
        now_ms=NOW_MS,
        fetcher=_ok_fetcher(KIMI_BODY),
    ).reason == "credentials expired"
    # pi, SECONDS, fresh (unit must not be assumed)
    assert collect_kimi(
        pi_auth_path=_kimi_pi_auth(tmp_path / "c", expires=NOW_MS // 1000 + 900),
        now_ms=NOW_MS,
        fetcher=_ok_fetcher(KIMI_BODY),
    ).ok is True
    # pi, seconds, expired
    assert collect_kimi(
        pi_auth_path=_kimi_pi_auth(tmp_path / "d", expires=NOW_MS // 1000 - 1),
        now_ms=NOW_MS,
        fetcher=_ok_fetcher(KIMI_BODY),
    ).reason == "credentials expired"


# ---------------------------------------------------------------------------
# Degradation paths: each fails soft with a readable reason, never raises.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("collector", "relative"),
    [
        (
            lambda path, **kw: collect_claude(path, **kw),
            ".claude/.credentials.json",
        ),
        (
            lambda path, **kw: collect_codex(path, **kw),
            ".codex/auth.json",
        ),
        (
            lambda path, **kw: collect_kimi(cli_credentials_path=path, **kw),
            ".kimi-code/credentials/kimi-code.json",
        ),
    ],
)
def test_missing_credentials_degrade_without_fetching(
    tmp_path: Path, collector, relative: str
) -> None:
    def forbidden_fetch(url: str, token: str, timeout: float) -> None:
        raise AssertionError("must not fetch without credentials")

    snapshot = collector(
        tmp_path / relative, now_ms=NOW_MS, fetcher=forbidden_fetch
    )
    assert snapshot.ok is False
    assert snapshot.reason == "credentials missing"


def test_claude_expired_credentials_skip_the_network(tmp_path: Path) -> None:
    def forbidden_fetch(url: str, token: str, timeout: float) -> None:
        raise AssertionError("must not fetch with expired credentials")

    snapshot = collect_claude(
        _claude_credentials(tmp_path, expires_at_ms=NOW_MS - 1),
        now_ms=NOW_MS,
        fetcher=forbidden_fetch,
    )
    assert snapshot.ok is False
    assert snapshot.reason == "credentials expired"


def test_codex_expired_jwt_skips_the_network(tmp_path: Path) -> None:
    def forbidden_fetch(url: str, token: str, timeout: float) -> None:
        raise AssertionError("must not fetch with expired credentials")

    snapshot = collect_codex(
        _codex_credentials(tmp_path, exp_seconds=NOW_MS // 1000 - 60),
        now_ms=NOW_MS,
        fetcher=forbidden_fetch,
    )
    assert snapshot.ok is False
    assert snapshot.reason == "credentials expired"


def test_kimi_expired_credentials_skip_the_network(tmp_path: Path) -> None:
    """With no pi credential present, an expired CLI credential is n/a."""

    def forbidden_fetch(url: str, token: str, timeout: float) -> None:
        raise AssertionError("must not fetch with expired credentials")

    snapshot = collect_kimi(
        cli_credentials_path=_kimi_credentials(tmp_path, expires_at=NOW_MS // 1000 - 1),
        now_ms=NOW_MS,
        fetcher=forbidden_fetch,
    )
    assert snapshot.ok is False
    assert snapshot.reason == "credentials expired"


def test_network_failure_degrades_to_a_readable_reason(tmp_path: Path) -> None:
    def broken_fetch(url: str, token: str, timeout: float) -> None:
        raise urllib.error.URLError("fixture network down")

    snapshot = collect_codex(
        _codex_credentials(tmp_path), now_ms=NOW_MS, fetcher=broken_fetch
    )
    assert snapshot.ok is False
    assert snapshot.reason == "network error"


def test_http_401_reports_rejected_credentials(tmp_path: Path) -> None:
    def unauthorized(url: str, token: str, timeout: float) -> None:
        raise urllib.error.HTTPError(url, 401, "unauthorized", None, None)

    snapshot = collect_claude(
        _claude_credentials(tmp_path), now_ms=NOW_MS, fetcher=unauthorized
    )
    assert snapshot.ok is False
    assert snapshot.reason == "credentials rejected (http 401)"


def test_invalid_response_body_degrades(tmp_path: Path) -> None:
    def garbage(url: str, token: str, timeout: float) -> None:
        raise json.JSONDecodeError("expecting value", "", 0)

    snapshot = collect_kimi(
        cli_credentials_path=_kimi_credentials(tmp_path),
        now_ms=NOW_MS,
        fetcher=garbage,
    )
    assert snapshot.ok is False
    assert snapshot.reason == "invalid response"


# ---------------------------------------------------------------------------
# Cache behavior: one failure never blocks the others; top only reads.
# ---------------------------------------------------------------------------


def _populate_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    _claude_credentials(home)
    _codex_credentials(home)
    _kimi_credentials(home)
    return home


def test_refresh_collects_each_source_independently(tmp_path: Path) -> None:
    home = _populate_home(tmp_path)

    def fetch(url: str, token: str, timeout: float) -> dict[str, object]:
        if "anthropic" in url:
            return dict(CLAUDE_BODY)
        if "chatgpt" in url:
            raise urllib.error.URLError("fixture network down")
        return dict(KIMI_BODY)

    cache = UsageCache(home=home, fetcher=fetch, clock=lambda: NOW_MS / 1000)
    cache.refresh_once()
    payload = cache.snapshot_payload(NOW_MS)

    by_source = {source["source"]: source for source in payload["sources"]}
    assert by_source["claude"]["ok"] is True
    assert by_source["codex"]["ok"] is False
    assert by_source["codex"]["reason"] == "network error"
    assert by_source["kimi"]["ok"] is True
    # Every value carries its fetch age and a stale flag.
    assert by_source["claude"]["ageMs"] == 0
    assert by_source["claude"]["stale"] is False


def test_snapshot_marks_old_data_stale(tmp_path: Path) -> None:
    home = _populate_home(tmp_path)
    cache = UsageCache(
        home=home,
        fetcher=_ok_fetcher(CLAUDE_BODY),
        clock=lambda: NOW_MS / 1000,
    )
    cache.refresh_once()

    # The stale line is two refresh intervals (1800 s each): a reading is
    # stale only once a refresh was missed, not merely between refreshes.
    between_refreshes = cache.snapshot_payload(NOW_MS + 31 * 60_000)
    on_the_line = cache.snapshot_payload(NOW_MS + 60 * 60_000)
    missed_a_refresh = cache.snapshot_payload(NOW_MS + 61 * 60_000)

    for source in between_refreshes["sources"]:
        assert source["ageMs"] == 31 * 60_000
        assert source["stale"] is False
    for source in on_the_line["sources"]:
        assert source["stale"] is False
    for source in missed_a_refresh["sources"]:
        assert source["ageMs"] == 61 * 60_000
        assert source["stale"] is True


def test_snapshot_before_any_refresh_is_explicit(tmp_path: Path) -> None:
    cache = UsageCache(home=tmp_path / "home", clock=lambda: NOW_MS / 1000)
    payload = cache.snapshot_payload(NOW_MS)
    reasons = {source["source"]: source["reason"] for source in payload["sources"]}
    assert reasons == {
        "claude": "not yet fetched",
        "codex": "not yet fetched",
        "kimi": "not yet fetched",
    }


def test_snapshot_payload_never_fetches(tmp_path: Path) -> None:
    """The synchronous top path must not touch the network (acceptance 2)."""

    home = _populate_home(tmp_path)
    cache = UsageCache(
        home=home,
        fetcher=_ok_fetcher(CODEX_BODY),
        clock=lambda: NOW_MS / 1000,
    )
    cache.refresh_once()

    def forbidden_fetch(url: str, token: str, timeout: float) -> None:
        raise AssertionError("snapshot_payload must be cache-only")

    cache._fetcher = forbidden_fetch
    payload = cache.snapshot_payload(NOW_MS)
    assert any(source["ok"] for source in payload["sources"])


def test_network_failures_back_off_exponentially(tmp_path: Path) -> None:
    """A rate-limited endpoint must not be hit every cycle (the claude
    endpoint answered http 429 after repeated polling during development)."""

    home = _populate_home(tmp_path)
    clock = {"now": NOW_MS / 1000}
    calls = {"n": 0}

    def rate_limited(url: str, token: str, timeout: float) -> None:
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 429, "too many", None, None)

    cache = UsageCache(
        home=home,
        fetcher=rate_limited,
        clock=lambda: clock["now"],
        refresh_interval_seconds=120.0,
    )
    cache.refresh_once()
    first = calls["n"]
    assert first == 3  # every source attempted once

    # Immediately again: all three are inside their backoff windows.
    cache.refresh_once()
    assert calls["n"] == first

    # After the first backoff interval (120s) sources retry once more.
    clock["now"] += 121
    cache.refresh_once()
    assert calls["n"] == first + 3

    # Second consecutive failure backs off 240s: +121s is not enough.
    clock["now"] += 121
    cache.refresh_once()
    assert calls["n"] == first + 3
    clock["now"] += 120
    cache.refresh_once()
    assert calls["n"] == first + 6


def _source_by_name(payload: dict[str, object], name: str) -> dict[str, object]:
    return next(
        source
        for source in payload["sources"]
        if isinstance(source, dict) and source.get("source") == name
    )


def test_claude_429_backs_off_with_capped_doubling_and_cached_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claude usage endpoint penalizes high-frequency polling with 429;
    the source must back off (doubling intervals, one-hour ceiling) and keep
    serving its last successful reading -- age and stale stay honest -- while
    the endpoint is penalized, instead of re-hammering it or showing n/a.
    """

    assert usage.CLAUDE_MAX_BACKOFF_SECONDS == 60 * 60.0
    monkeypatch.setattr(usage, "CLAUDE_MAX_BACKOFF_SECONDS", 800.0)
    home = tmp_path / "home"
    _claude_credentials(home)
    _codex_credentials(home)
    _kimi_credentials(home)
    clock = {"now": NOW_MS / 1000}
    claude_calls: list[float] = []

    def flaky(url: str, token: str, timeout: float) -> dict[str, object]:
        if url == usage.CLAUDE_USAGE_URL:
            claude_calls.append(clock["now"])
            if len(claude_calls) >= 2:
                raise urllib.error.HTTPError(url, 429, "too many", None, None)
            return CLAUDE_BODY
        raise urllib.error.HTTPError(url, 429, "too many", None, None)

    cache = UsageCache(
        home=home,
        fetcher=flaky,
        clock=lambda: clock["now"],
        refresh_interval_seconds=100.0,
    )

    def now_ms() -> int:
        return int(clock["now"] * 1000)

    # t=0: claude succeeds once, then every cycle hits 429.
    cache.refresh_once()
    assert len(claude_calls) == 1

    # t=0: the next cycle hits 429 -> backoff window [0, 100).
    cache.refresh_once()
    assert len(claude_calls) == 2

    # t=50: inside the window -- no network call; the last success is served
    # with its original fetch instant and an explicit stale/backing-off mark.
    clock["now"] += 50
    cache.refresh_once()
    assert len(claude_calls) == 2
    claude = _source_by_name(cache.snapshot_payload(now_ms()), "claude")
    assert claude["ok"] is True
    assert claude["backingOff"] is True
    assert claude["stale"] is True
    assert claude["fetchedAtMs"] == NOW_MS
    assert {window["label"] for window in claude["windows"]} == {
        "session",
        "weekly_all",
        "weekly_scoped",
    }
    # codex/kimi keep the generic path: no stale-success cache, the failure
    # itself is surfaced.
    codex = _source_by_name(cache.snapshot_payload(now_ms()), "codex")
    assert codex["ok"] is False and codex["reason"] == "http 429"
    assert "backingOff" not in codex and codex["windows"] == []

    # t=100: first window expires -> retry -> 429 -> 200s window.
    clock["now"] += 50
    cache.refresh_once()
    assert len(claude_calls) == 3
    clock["now"] += 50
    cache.refresh_once()
    assert len(claude_calls) == 3  # still inside the 200s window

    # t=300: retry -> 429 -> 400s window.
    clock["now"] += 150
    cache.refresh_once()
    assert len(claude_calls) == 4
    # t=700: retry -> 429 -> 800s window (doubled up to the cap).
    clock["now"] += 400
    cache.refresh_once()
    assert len(claude_calls) == 5
    # t=1500 and t=2300: the cap holds -- no 1600s window.
    clock["now"] += 800
    cache.refresh_once()
    assert len(claude_calls) == 6
    clock["now"] += 800
    cache.refresh_once()
    assert len(claude_calls) == 7

    gaps = [round(b - a, 3) for a, b in zip(claude_calls, claude_calls[1:])]
    assert gaps == [0.0, 100.0, 200.0, 400.0, 800.0, 800.0]


def test_claude_backoff_resets_after_success(tmp_path: Path) -> None:
    """A successful refresh clears the backoff: stale/backing-off marks
    disappear and the next 429 backs off at the base interval again, not a
    doubled one."""

    home = tmp_path / "home"
    _claude_credentials(home)
    _codex_credentials(home)
    _kimi_credentials(home)
    clock = {"now": NOW_MS / 1000}
    claude_calls: list[float] = []
    mode = {"value": "429"}

    def flaky(url: str, token: str, timeout: float) -> dict[str, object]:
        if url == usage.CLAUDE_USAGE_URL:
            claude_calls.append(clock["now"])
            if mode["value"] == "429":
                raise urllib.error.HTTPError(url, 429, "too many", None, None)
            return CLAUDE_BODY
        return dict(CODEX_BODY)

    cache = UsageCache(
        home=home,
        fetcher=flaky,
        clock=lambda: clock["now"],
        refresh_interval_seconds=100.0,
    )

    def now_ms() -> int:
        return int(clock["now"] * 1000)

    # t=0: first 429 -> window [0, 100); the next cycles skip.
    cache.refresh_once()
    cache.refresh_once()
    assert len(claude_calls) == 1
    clock["now"] += 50
    cache.refresh_once()
    assert len(claude_calls) == 1

    # t=100: retry -> second 429 -> window [100, 300).
    clock["now"] += 50
    cache.refresh_once()
    assert len(claude_calls) == 2

    # t=300: retry succeeds -> failure counter resets, marks clear.
    clock["now"] += 200
    mode["value"] = "200"
    cache.refresh_once()
    assert len(claude_calls) == 3
    claude = _source_by_name(cache.snapshot_payload(now_ms()), "claude")
    assert claude["ok"] is True
    assert "backingOff" not in claude
    assert claude["stale"] is False
    assert claude["fetchedAtMs"] == now_ms()

    # A fresh 429 immediately after recovery backs off at the BASE interval
    # (100s), proving the counter was reset, not merely paused; inside that
    # window the just-cached success is served with the backing-off mark.
    mode["value"] = "429"
    cache.refresh_once()
    assert len(claude_calls) == 4
    clock["now"] += 50
    cache.refresh_once()
    assert len(claude_calls) == 4
    claude = _source_by_name(cache.snapshot_payload(now_ms()), "claude")
    assert claude["ok"] is True and claude["backingOff"] is True
    clock["now"] += 50
    cache.refresh_once()
    assert len(claude_calls) == 5
    assert round(claude_calls[4] - claude_calls[3], 3) == 100.0


def test_local_credential_failures_do_not_back_off(tmp_path: Path) -> None:
    """Expired/missing credentials cost a file read only; the next cycle
    must re-check so a pi-side refresh is picked up promptly."""

    home = tmp_path / "home"
    cache = UsageCache(
        home=home,
        fetcher=_ok_fetcher(KIMI_BODY),
        clock=lambda: NOW_MS / 1000,
    )
    cache.refresh_once()
    first = {
        s["source"]: s["reason"] for s in cache.snapshot_payload(NOW_MS)["sources"]
    }
    assert first["kimi"] == "credentials missing"

    # The pi credential appears (pi refreshed it); the very next cycle sees it.
    _kimi_pi_auth(home)
    cache.refresh_once()
    second = {s["source"]: s for s in cache.snapshot_payload(NOW_MS)["sources"]}
    assert second["kimi"]["ok"] is True


def test_payloads_never_contain_token_material(tmp_path: Path) -> None:
    home = _populate_home(tmp_path)
    cache = UsageCache(
        home=home,
        fetcher=_ok_fetcher(CLAUDE_BODY),
        clock=lambda: NOW_MS / 1000,
    )
    cache.refresh_once()
    serialized = json.dumps(cache.snapshot_payload(NOW_MS))
    assert FIXTURE_TOKEN not in serialized
    # The codex fixture JWT must not leak either.
    codex_token = json.loads(
        (home / ".codex" / "auth.json").read_text(encoding="utf-8")
    )["tokens"]["access_token"]
    assert codex_token not in serialized


# ---------------------------------------------------------------------------
# Daemon integration: top.snapshot carries the quota section.
# ---------------------------------------------------------------------------


def test_top_snapshot_includes_quota(tmp_path: Path) -> None:
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport

    state_dir = tmp_path / "state"
    (state_dir / "logs" / "workers").mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())

    class _Harnesses:
        def status(self) -> tuple[()]:
            return ()

    class _Presence:
        def online_actors(self) -> tuple[()]:
            return ()

        def online_mailboxes(self) -> tuple[()]:
            return ()

    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]

    # Without a cache the section is present but empty.
    result = app.handle("top.snapshot", {})
    assert result["quota"] == {"sources": []}

    cache = UsageCache(
        home=_populate_home(tmp_path / "home"),
        fetcher=_ok_fetcher(CODEX_BODY),
        clock=lambda: NOW_MS / 1000,
    )
    cache.refresh_once()
    app._usage_cache = cache

    result = app.handle("top.snapshot", {})
    sources = {source["source"] for source in result["quota"]["sources"]}
    assert sources == {"claude", "codex", "kimi"}
