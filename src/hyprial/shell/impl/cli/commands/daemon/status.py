"""``hyprial ps`` and ``hyprial top`` process inspection."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Callable
from datetime import datetime
from hyprial.kernel import ipc_errors
import re
import subprocess
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject
@app.command("ps")
def process_status(
    connector_kind: str | None = typer.Argument(None, help="Optional connector kind."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show daemon and connector status."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        try:
            result = services._daemon_request("ps", restore_wait=0.0)
        # PR #332 F4②: both projections branch on the registered transient
        # classes instead of comparing code strings.  Other transient
        # failures (disconnected / timeout) surface to the CLI error
        # boundary, as the old ``!= DAEMON_RESTORING``/``!=
        # DAEMON_UNAVAILABLE`` raises did.
        except ipc_errors.DaemonRestoringError:
            # The daemon is up and answering but restore has not
            # finished: answer from the readiness probe instead of
            # hanging until ps is served.  `phase`/"restorePending" are
            # the restoring signal; connectors stay empty because the
            # fleet table is exactly what is still being built.
            probe = services._daemon_probe(timeout=2.0)
            return {
                "ok": True,
                "restoring": True,
                "daemon": {
                    key: probe[key]
                    for key in (
                        "running",
                        "pid",
                        "epoch",
                        "nodeId",
                        "owner",
                        "socket",
                        "phase",
                    )
                    if key in probe
                },
                "restorePending": probe.get("restorePending", True),
                "connectors": [],
            }
        except ipc_errors.DaemonUnavailableError:
            return {"ok": True, "daemon": {"running": False}, "connectors": []}
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "daemon ps result must be an object")
        connectors = result.get("connectors", [])
        if connector_kind is not None and isinstance(connectors, list):
            connectors = [
                item
                for item in connectors
                if isinstance(item, dict) and item.get("runtime") == connector_kind
            ]
        return {"ok": True, **result, "connectors": connectors}

    services._execute(operation, json_output=json_output)


_TOP_SEVERITY = {
    "stuck": 0,
    "spin": 1,
    "stalled": 2,
    "backlog": 3,
    "ok": 4,
    "idle": 5,
    "online": 5,
    "offline": 6,
    "stopped": 7,
}


def _parse_ps_duration(value: str) -> float | None:
    """Parse ps ``time=``/``etime=`` values ([[dd-]hh:]mm:ss[.cc]) to seconds."""

    match = re.fullmatch(
        r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)(?:\.(\d+))?", value.strip()
    )
    if match is None:
        return None
    days, hours, minutes, seconds, fraction = match.groups()
    total = int(minutes) * 60 + int(seconds)
    total += int(hours) * 3600 if hours else 0
    total += int(days) * 86400 if days else 0
    if fraction:
        total += int(fraction) / (10 ** len(fraction))
    return float(total)


def _sample_process_table() -> dict[int, tuple[int, float, int, float, float]]:
    """One ``ps`` sweep: pid -> (ppid, cpu%, rss kb, cpu seconds, etime seconds).

    These harnesses are I/O-bound, so instantaneous %cpu is normally 0.0 and
    carries almost no diagnostic weight; it is sampled anyway (one fork total)
    and only rendered under ``--wide``.
    """

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,etime=,%cpu=,rss=,time="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    table: dict[int, tuple[int, float, int, float, float]] = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
            cpu_percent = float(fields[3])
            rss_kb = int(fields[4])
        except ValueError:
            continue
        etime = _parse_ps_duration(fields[2])
        cputime = _parse_ps_duration(fields[5])
        if etime is None or cputime is None:
            continue
        table[pid] = (ppid, cpu_percent, rss_kb, cputime, etime)
    return table


def _subtree_totals(
    table: dict[int, tuple[int, float, int, float, float]], pid: int
) -> tuple[float, int, float, float] | None:
    """Aggregate cpu%/rss/cputime over the pid's ppid subtree.

    pi workers share the daemon's process group, and codex grandchildren
    escape the worker's own group, so per-pgid aggregation is wrong in both
    directions; walking ppid children is correct for every runtime here.
    """

    if pid not in table:
        return None
    children: dict[int, list[int]] = {}
    for child_pid, entry in table.items():
        children.setdefault(entry[0], []).append(child_pid)
    cpu_percent = 0.0
    rss_kb = 0
    cputime = 0.0
    stack = [pid]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        entry = table.get(current)
        if entry is not None:
            cpu_percent += entry[1]
            rss_kb += entry[2]
            cputime += entry[3]
        stack.extend(children.get(current, ()))
    return cpu_percent, rss_kb, cputime, table[pid][4]


def _format_top_age(ms: int | None) -> str:
    if ms is None:
        return "-"
    seconds = max(0, int(ms / 1000))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h{minutes % 60:02d}m"
    return f"{hours // 24}d{hours % 24:02d}h"


def _format_top_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return _format_top_age(int(seconds * 1000))


def _format_top_rss(rss_kb: int | None) -> str:
    if rss_kb is None:
        return "-"
    if rss_kb >= 1024 * 1024:
        return f"{rss_kb / (1024 * 1024):.1f}G"
    if rss_kb >= 1024:
        return f"{rss_kb // 1024}M"
    return f"{rss_kb}K"


def _top_state_label(row: JsonObject, now_ms: int) -> str:
    state = str(row.get("state"))
    detail = row.get("stateDetail")
    detail = detail if isinstance(detail, dict) else {}
    if state == "stopped":
        return "\u00d7 stopped"
    if state == "online":
        return "online (proc n/a)"
    if state == "offline":
        return "offline (proc n/a)"
    if state == "stuck":
        return f"\u26a0 STUCK {_format_top_age(detail.get('openMs'))}"
    if state == "spin":
        mean_ms = detail.get("meanMs")
        return (
            f"\u26a0 SPIN {detail.get('failures', '?')}/{detail.get('window', '?')}"
            f" avg {_format_top_age(mean_ms)}"
        )
    if state == "stalled":
        return (
            f"\u26a0 STALLED {_format_top_age(detail.get('stalledForMs'))}"
            f" (turn {_format_top_age(detail.get('openMs'))})"
        )
    if state == "backlog":
        return (
            f"\u26a0 BACKLOG q={detail.get('pending', row.get('pendingCount', '?'))}"
            f" idle {_format_top_age(detail.get('idleMs'))}"
        )
    if state == "idle":
        return "idle"
    if detail.get("busy") is True and isinstance(row.get("openTurnStartedAtMs"), int):
        open_ms = now_ms - int(row["openTurnStartedAtMs"])
        return f"ok (turn {_format_top_age(open_ms)})"
    return "ok"


def _render_top(
    payload: JsonObject,
    *,
    wide: bool,
    sampler: Callable[[], dict[int, tuple[int, float, int, float, float]]]
    | None = None,
) -> str:
    services = get_services()
    daemon = payload.get("daemon")
    daemon = daemon if isinstance(daemon, dict) else {}
    now_ms = payload.get("nowMs")
    now_ms = now_ms if isinstance(now_ms, int) else int(services.time.time() * 1000)
    actors = [row for row in payload.get("actors", []) if isinstance(row, dict)]
    actors.sort(
        key=lambda row: (
            _TOP_SEVERITY.get(str(row.get("state")), 4),
            str(row.get("name")),
        )
    )
    sample = sampler if sampler is not None else services._sample_process_table
    table = sample()

    headers = ["ACTOR", "RT", "PID"]
    if wide:
        headers.append("CPU%")
    headers += ["RSS", "CPUTIME", "UPTIME", "TURNS", "AVG5", "IDLE", "Q", "STATE"]
    rows: list[list[str]] = []
    for row in actors:
        pid = row.get("pid")
        totals = _subtree_totals(table, pid) if isinstance(pid, int) else None
        cpu_percent: float | None = None
        rss_kb: int | None = None
        cputime: float | None = None
        etime: float | None = None
        if totals is not None:
            cpu_percent, rss_kb, cputime, etime = totals
        started_at = row.get("processStartedAtMs")
        uptime_ms = now_ms - started_at if isinstance(started_at, int) else None
        durations = row.get("recentTurnDurationsMs")
        avg5 = (
            int(sum(durations) / len(durations))
            if isinstance(durations, list) and durations
            else None
        )
        last_ended = row.get("lastTurnEndedAtMs")
        idle_ms = now_ms - last_ended if isinstance(last_ended, int) else None
        turn_count = row.get("turnCount")
        cells = [
            str(row.get("name")),
            str(row.get("runtime")) if row.get("runtime") is not None else "-",
            str(pid) if isinstance(pid, int) else "-",
        ]
        if wide:
            cells.append(f"{cpu_percent:.1f}" if cpu_percent is not None else "-")
        cells += [
            _format_top_rss(rss_kb),
            _format_top_seconds(cputime),
            (
                _format_top_age(uptime_ms)
                if uptime_ms is not None
                else _format_top_seconds(etime)
            ),
            str(turn_count) if isinstance(turn_count, int) else "-",
            _format_top_age(avg5),
            _format_top_age(idle_ms),
            str(row.get("pendingCount", 0)),
            _top_state_label(row, now_ms),
        ]
        rows.append(cells)

    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rows)])
        for index in range(len(headers))
    ]
    lines = [
        f"hyprial top   daemon {daemon.get('pid', '?')}   node {daemon.get('nodeId', '?')}"
    ]
    bypass = daemon.get("dispatchWithoutPacCount")
    if isinstance(bypass, int):
        # A3 dispatch gate (design-dispatch-always-pac §三②): the numerator
        # of the PAC bypass rate -- dispatches that never went through a
        # workflow run.  Record-only for now; the durable events live in
        # state/logs/daemon.jsonl.
        lines.append(f"dispatch without PAC: {bypass}")
    conversations = daemon.get("dispatchConversationCount")
    if isinstance(conversations, int):
        # spec-dispatch-gate-classifier-2026-09-04: the contrast counter --
        # request-shape sends the narrowed gate classified as conversation
        # (adjudications, progress reports, replies), observed but never in
        # the numerator above.
        lines.append(f"dispatch conversations (not counted): {conversations}")
    lines.append(
        "  ".join(
            header.ljust(widths[index]) for index, header in enumerate(headers)
        ).rstrip()
    )
    for row in rows:
        lines.append(
            "  ".join(
                cell.ljust(widths[index]) for index, cell in enumerate(row)
            ).rstrip()
        )
    stranded = [item for item in payload.get("stranded", []) if isinstance(item, dict)]
    if stranded:
        lines.append("")
        lines.append("stranded inbox keys (other node; never claimed here):")
        for item in stranded:
            lines.append(
                f"  {item.get('recipient')}  pending={item.get('pending', '?')}"
                f"  (current: {item.get('currentRecipient', '?')})"
            )
    lines.extend(_render_top_quota(payload.get("quota"), now_ms))
    return "\n".join(lines)


def _format_top_window_seconds(seconds: int | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _format_top_reset(resets_at_ms: int | None) -> str | None:
    if not isinstance(resets_at_ms, int):
        return None
    when = datetime.fromtimestamp(resets_at_ms / 1000).astimezone()
    return when.strftime("%m-%d %H:%M")


def _format_quota_window(window: JsonObject, now_ms: int) -> str | None:
    used = window.get("used")
    limit = window.get("limit")
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
        return None
    if limit <= 0:
        return None
    label = str(window.get("label") or window.get("id"))
    scope = window.get("scope")
    if isinstance(scope, str) and scope:
        label = f"{label}:{scope}"
    span = _format_top_window_seconds(window.get("windowSeconds"))
    if span is not None:
        label = f"{label}({span})"
    percent = used / limit * 100
    # Percent-limited sources (claude/codex) show the percentage directly;
    # count-limited sources (kimi) show the raw ratio so a limit other than
    # 100 is never mistaken for a percentage.
    value = f"{percent:g}%" if limit == 100 else f"{used:g}/{limit:g} ({percent:g}%)"
    flags: list[str] = []
    severity = window.get("severity")
    if isinstance(severity, str) and severity not in ("", "normal"):
        flags.append(severity.upper())
    if window.get("isActive") is True:
        flags.append("ACTIVE")
    reset = _format_top_reset(window.get("resetsAtMs"))
    suffix = f" {' '.join(flags)}" if flags else ""
    if reset is not None:
        suffix += f", resets {reset}"
    return f"{label} {value}{suffix}"


def _render_top_quota(quota: object, now_ms: int) -> list[str]:
    if not isinstance(quota, dict):
        return []
    sources = [
        source for source in quota.get("sources", []) if isinstance(source, dict)
    ]
    if not sources:
        return []
    lines = ["", "quota"]
    for source in sources:
        name = str(source.get("source"))
        age_ms = source.get("ageMs")
        age = _format_top_age(age_ms if isinstance(age_ms, int) else None)
        if source.get("backingOff") is True:
            # A backoff serve is old-but-real data: name the state instead of
            # pretending the reading is fresh.
            marker = f"[{age} ago, backing off]"
        else:
            stale = " STALE" if source.get("stale") is True else ""
            marker = f"[{age} ago{stale}]"
        if source.get("ok") is not True:
            reason = source.get("reason")
            lines.append(
                f"  {name:<8} n/a ({reason if isinstance(reason, str) else 'unknown'}) {marker}"
            )
            continue
        parts = [
            rendered
            for window in source.get("windows", [])
            if isinstance(window, dict)
            and (rendered := _format_quota_window(window, now_ms)) is not None
        ]
        extra = source.get("extra")
        if isinstance(extra, dict):
            spend = extra.get("spend")
            if isinstance(spend, dict) and isinstance(
                spend.get("limitUsd"), (int, float)
            ):
                used_usd = spend.get("usedUsd")
                text = f"spend ${used_usd or 0:g}/${spend['limitUsd']:g}"
                if spend.get("enabled") is not True:
                    reason = spend.get("disabledReason")
                    text += f" ({reason if isinstance(reason, str) else 'disabled'})"
                parts.append(text)
            credits = extra.get("credits")
            if isinstance(credits, dict) and isinstance(credits.get("balance"), str):
                parts.append(f"credits {credits['balance']}")
        lines.append(f"  {name:<8} {' · '.join(parts) or 'n/a'} {marker}")
    return lines


@app.command("top")
def top_status(
    wide: bool = typer.Option(
        False,
        "--wide",
        help="Also show instantaneous CPU% (normally 0.0 for these I/O-bound harnesses).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show whether each agent is actually working: turns, queue, verdicts.

    Headless turn fields come from the current worker-generation JSONL log and
    stay null when that generation boundary cannot be proven.
    Interactive Claude ``turnCount`` and ``lastTurnEndedAtMs`` come from fenced
    Stop-hook receipts held by this daemon generation; ``turnCountSinceMs``
    marks when that partial count began. The end timestamp is daemon receipt
    time, so reconnect/poll delay can make table IDLE understate model idle.
    ``recentTurnDurationsMs``, ``recentTurnOutcomes``, ``recentFailureRatio``,
    and ``openTurnStartedAtMs`` stay null for interactive Claude because no
    trustworthy start or outcome signal exists.
    After a daemon restart or for a session launched before turn reporting,
    counts stay null with ``turnStatsUnavailableReason`` until a reporting
    session registers or its next Stop pulse arrives; relaunch for a known zero.
    Queue fields always come from the durable inbox and are independent of turn
    reporting.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request("top.snapshot")
        if not isinstance(result, dict) or not isinstance(result.get("actors"), list):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon top.snapshot result must contain actors"
            )
        return CliResult({"ok": True, **result}, render=lambda data: _render_top(data, wide=wide))

    services._execute(operation, json_output=json_output)
