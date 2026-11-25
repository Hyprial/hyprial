"""CLI rendering for ``hyprial agent top``: one ps sweep, ppid-subtree totals, verdicts."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.daemon.impl.operations.top as daemon_top
import hyprial.shell.impl.cli.commands.daemon.top as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

NOW_MS = 1_800_000_000_000


def _snapshot() -> dict[str, object]:
    return {
        "daemon": {"pid": 17428, "nodeId": "node-1", "owner": "op", "epoch": "e"},
        "nowMs": NOW_MS,
        "actors": [
            {
                "actor": "agent:op:node-1:wk-stuck",
                "name": "wk-stuck",
                "runtime": "pi",
                "running": True,
                "pid": 2222,
                "processStartedAtMs": NOW_MS - 3_600_000,
                "turnCount": 0,
                "lastTurnEndedAtMs": None,
                "recentTurnDurationsMs": [],
                "recentTurnOutcomes": [],
                "recentFailureRatio": None,
                "openTurnStartedAtMs": NOW_MS - 14 * 60_000,
                "pendingCount": 1,
                "oldestPendingAgeMs": 14 * 60_000,
                "state": "stuck",
                "stateDetail": {"openMs": 14 * 60_000},
            },
            {
                "actor": "agent:op:node-1:wk-ok",
                "name": "wk-ok",
                "runtime": "codex",
                "running": True,
                "pid": 3333,
                "processStartedAtMs": NOW_MS - 7 * 3_600_000,
                "turnCount": 4,
                "lastTurnEndedAtMs": NOW_MS - 11 * 60_000,
                "recentTurnDurationsMs": [300_000, 400_000, 350_000, 410_000],
                "recentTurnOutcomes": ["completed"] * 4,
                "recentFailureRatio": 0.0,
                "openTurnStartedAtMs": None,
                "pendingCount": 0,
                "oldestPendingAgeMs": None,
                "state": "ok",
            },
            {
                "actor": "agent:op:node-1:wk-dead",
                "name": "wk-dead",
                "runtime": "pi",
                "running": False,
                "pid": None,
                "processStartedAtMs": None,
                "turnCount": 14,
                "lastTurnEndedAtMs": NOW_MS - 4 * 3_600_000,
                "recentTurnDurationsMs": [304_000],
                "recentTurnOutcomes": ["completed"],
                "recentFailureRatio": 0.0,
                "openTurnStartedAtMs": None,
                "pendingCount": 0,
                "oldestPendingAgeMs": None,
                "state": "stopped",
            },
        ],
        "stranded": [
            {
                "recipient": "agent:op:old-node:wk-ok",
                "currentRecipient": "agent:op:node-1:wk-ok",
                "pending": 2,
            }
        ],
    }


# pid -> (ppid, cpu%, rss kb, cpu seconds, etime seconds)
_FAKE_PS = {
    17428: (1, 0.0, 100_000, 30.0, 28_000.0),
    2222: (17428, 0.0, 168_000, 2.65, 3_600.0),
    3333: (17428, 0.0, 211_000, 23.05, 25_000.0),
    4444: (3333, 0.0, 50_000, 5.0, 20_000.0),  # codex grandchild
}


def test_top_renders_verdicts_and_subtree_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, *_a, **_k: daemon_top.inspect_snapshot(_snapshot()) if method == "top.inspect" else _snapshot())
    monkeypatch.setattr(daemon_top, "_sample_process_table", lambda: dict(_FAKE_PS))

    result = CliRunner().invoke(_cli_root_app, ["agent", "top"], color=False)

    assert result.exit_code == 0, result.output
    out = result.output
    assert "daemon 17428" in out
    # Warning rows sort ahead of healthy ones; stopped sinks to the bottom.
    assert out.index("wk-stuck") < out.index("wk-ok") < out.index("wk-dead")
    assert "STUCK 14m" in out
    assert "× stopped" in out
    # RSS totals include the codex grandchild (ppid subtree, not pgid).
    assert "254M" in out  # 211000 + 50000 kb
    assert "28s" in out  # cputime 23.05 + 5.0
    # CPU% is hidden by default (measured 0.0 for these I/O-bound harnesses).
    assert "CPU%" not in out
    # Stranded keys render in the footer, never joined into the live row.
    assert "agent:op:old-node:wk-ok" in out
    assert "pending=2" in out
    # The live row's Q column reflects only its own canonical URI; W/OUT/INJQ
    # sit between Q and the verdict.
    stuck_line = next(line for line in out.splitlines() if "wk-stuck" in line)
    tokens = stuck_line.split()
    assert tokens[tokens.index("\u26a0") - 1] == "1"


def test_top_wide_shows_cpu_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, *_a, **_k: daemon_top.inspect_snapshot(_snapshot()) if method == "top.inspect" else _snapshot())
    monkeypatch.setattr(daemon_top, "_sample_process_table", lambda: dict(_FAKE_PS))

    result = CliRunner().invoke(_cli_root_app, ["agent", "top", "--wide"], color=False)

    assert result.exit_code == 0, result.output
    assert "CPU%" in result.output


_QUOTA = {
    "sources": [
        {
            "source": "claude",
            "ok": True,
            "fetchedAtMs": NOW_MS - 120_000,
            "ageMs": 120_000,
            "stale": False,
            "reason": None,
            "windows": [
                {
                    "id": "session",
                    "label": "session",
                    "used": 12.0,
                    "limit": 100.0,
                    "resetsAtMs": None,
                    "windowSeconds": None,
                    "scope": None,
                    "severity": "normal",
                    "isActive": False,
                },
                {
                    "id": "weekly_scoped",
                    "label": "weekly_scoped",
                    "used": 100.0,
                    "limit": 100.0,
                    "resetsAtMs": NOW_MS + 3_600_000,
                    "windowSeconds": None,
                    "scope": "Fable",
                    "severity": "critical",
                    "isActive": True,
                },
            ],
            "extra": {
                "spend": {
                    "usedUsd": 0.0,
                    "limitUsd": 200.0,
                    "enabled": False,
                    "disabledReason": "out_of_credits",
                }
            },
        },
        {
            "source": "codex",
            "ok": True,
            "fetchedAtMs": NOW_MS - 60_000,
            "ageMs": 60_000,
            "stale": False,
            "reason": None,
            "windows": [
                {
                    "id": "primary",
                    "label": "weekly",
                    "used": 84.0,
                    "limit": 100.0,
                    "resetsAtMs": None,
                    "windowSeconds": 604800,
                    "scope": None,
                    "severity": None,
                    "isActive": None,
                }
            ],
            "extra": {"planType": "pro", "credits": {"balance": "0"}},
        },
        {
            "source": "kimi",
            "ok": False,
            "fetchedAtMs": NOW_MS - 15 * 60_000,
            "ageMs": 15 * 60_000,
            "stale": True,
            "reason": "credentials expired",
            "windows": [],
        },
    ]
}


def test_top_renders_quota_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()
    snapshot["quota"] = _QUOTA
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, *_a, **_k: daemon_top.inspect_snapshot(snapshot) if method == "top.inspect" else snapshot)
    monkeypatch.setattr(daemon_top, "_sample_process_table", lambda: dict(_FAKE_PS))

    result = CliRunner().invoke(_cli_root_app, ["agent", "top"], color=False)

    assert result.exit_code == 0, result.output
    out = result.output
    assert "quota" in out
    # The scoped weekly limit -- invisible in the official CLI -- renders with
    # its model scope, severity, and active flag.
    assert "weekly_scoped:Fable 100% CRITICAL ACTIVE" in out
    assert "spend $0/$200 (out_of_credits)" in out
    # Codex weekly window shows its span and the credit balance.
    assert "weekly(7d) 84%" in out
    assert "credits 0" in out
    # Every line carries its fetch age; stale data is marked.
    assert "[2m ago]" in out
    assert "n/a (credentials expired) [15m ago STALE]" in out


def test_top_quota_marks_backoff_served_data() -> None:
    """A cached serve during claude backoff renders its age and state, not
    a bare STALE flag: old-but-real numbers beat n/a."""

    payload = {
        "sources": [
            {
                "source": "claude",
                "ok": True,
                "fetchedAtMs": NOW_MS - 2 * 3_600_000,
                "ageMs": 2 * 3_600_000,
                "stale": True,
                "backingOff": True,
                "reason": None,
                "windows": [
                    {
                        "id": "weekly_all",
                        "label": "weekly",
                        "used": 79.0,
                        "limit": 100.0,
                        "resetsAtMs": None,
                        "windowSeconds": None,
                        "scope": None,
                        "severity": None,
                        "isActive": None,
                    }
                ],
            }
        ]
    }
    lines = _cli_owner_1._render_top_quota(payload, NOW_MS)
    assert "weekly 79%" in lines[2]
    assert "[2h00m ago, backing off]" in lines[2]
    assert "STALE" not in lines[2]


def test_top_json_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, *_a, **_k: daemon_top.inspect_snapshot(_snapshot()) if method == "top.inspect" else _snapshot())

    result = CliRunner().invoke(_cli_root_app, ["agent", "top", "--json"], color=False)

    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["actors"][0]["actor"] == "agent:op:node-1:wk-stuck"
    assert payload["stranded"][0]["pending"] == 2


def test_top_renders_with_zero_connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: an empty actors table crashed column-width computation
    (max over a bare int) — found live on a connector-less daemon."""

    snapshot = _snapshot()
    snapshot["actors"] = []
    snapshot["stranded"] = []
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, *_a, **_k: daemon_top.inspect_snapshot(snapshot) if method == "top.inspect" else snapshot)
    monkeypatch.setattr(daemon_top, "_sample_process_table", lambda: dict(_FAKE_PS))

    result = CliRunner().invoke(_cli_root_app, ["agent", "top"], color=False)

    assert result.exit_code == 0, result.output
    assert "ACTOR" in result.output


def test_parse_ps_duration_formats() -> None:
    assert daemon_top._parse_ps_duration("0:02.65") == pytest.approx(2.65)
    assert daemon_top._parse_ps_duration("07:35:34") == pytest.approx(7 * 3600 + 35 * 60 + 34)
    assert daemon_top._parse_ps_duration("2-03:04:05") == pytest.approx(
        2 * 86400 + 3 * 3600 + 4 * 60 + 5
    )
    assert daemon_top._parse_ps_duration("not-a-time") is None


def test_top_omits_removed_link_columns_in_table_and_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, *_a, **_k: daemon_top.inspect_snapshot(_snapshot()) if method == "top.inspect" else _snapshot())
    monkeypatch.setattr(daemon_top, "_sample_process_table", lambda: dict(_FAKE_PS))

    result = CliRunner().invoke(_cli_root_app, ["agent", "top"], color=False)

    assert result.exit_code == 0, result.output
    header = next(line for line in result.output.splitlines() if "ACTOR" in line)
    tokens = header.split()
    assert tokens[-4:] == ["Q", "LEGACY", "RUN", "STATE"]
    for col in ("W", "OUT", "INJQ"):
        assert col not in tokens

    import json

    json_res = CliRunner().invoke(_cli_root_app, ["agent", "top", "--json"], color=False)
    assert json_res.exit_code == 0, json_res.output
    payload = json.loads(json_res.stdout)
    assert "capacityW" not in payload["actors"][0]
    assert "outboundInFlight" not in payload["actors"][0]
    assert "inboundAwaitingInjection" not in payload["actors"][0]


def test_top_text_and_json_keep_distinct_methods_and_timeouts(monkeypatch):
    calls = []
    snapshot = _snapshot()

    def request(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return snapshot

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()
    assert runner.invoke(_cli_root_app, ["agent", "top"]).exit_code == 0
    assert calls[-1][0] == "top.inspect"
    assert calls[-1][2]["timeout_allowance"] == 5.0
    assert "timeout" not in calls[-1][2]
    assert runner.invoke(_cli_root_app, ["agent", "top", "--json"]).exit_code == 0
    assert calls[-1][0] == "top.snapshot"
    assert "timeout" not in calls[-1][2]
    assert "timeout_allowance" not in calls[-1][2]


def test_top_text_old_daemon_fails_without_local_sampling(monkeypatch):
    from hyprial.kernel import RequestPortError

    calls = []

    def unavailable(method, *args, **kwargs):
        calls.append(method)
        raise RequestPortError("METHOD_NOT_FOUND", "unknown daemon method top.inspect")

    monkeypatch.setattr(_cli_support, "_daemon_request", unavailable)
    monkeypatch.setattr(daemon_top, "_sample_process_table", lambda: pytest.fail("local sample"))
    result = CliRunner().invoke(_cli_root_app, ["agent", "top"])
    assert result.exit_code == 1
    assert "unknown daemon method top.inspect" in result.output
    assert calls == ["top.inspect"]
