"""Human rendering for the ``hyprial link`` command group.

Pure presentation: every field comes from the daemon's result, a missing or
ill-typed field renders as an em dash, and nothing here recomputes a window
bound (k, W, cwnd, RTT baselines) the daemon already decided.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from hyprial.shell.impl.cli.commands.daemon.top import _format_top_age
from hyprial.shell.impl.cli.output import tables

DASH = "\u2014"

#: Rendered once under a queued result.  The wait is bounded by the message
#: TTL, so a caller must not resend on its own.
RESEND_GUARD = (
    "the daemon re-evaluates this queued message; do not resend "
    "(the wait is bounded by the message TTL)"
)

_STATE_ORDER = {"established": 0, "pending": 1, "reclaimed": 2}
#: Lifecycle markers that name a PAC/task-bound edge.  README ruling 7 keeps
#: those out of the k budget; unknown or absent lifecycles count as regular.
_TASK_BOUND_MARKERS = ("pac", "task")


def _text(value: object) -> str:
    return value if isinstance(value, str) and value else DASH


def _count(value: object) -> str:
    # ``bool`` is an int subclass but never a window count.
    return str(value) if type(value) is int else DASH


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DASH
    return f"{value:g}"


def _rate(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DASH
    return f"{value:g}/s"


def _duration(value: object) -> str:
    return _format_top_age(value) if type(value) is int else DASH


def _signed_duration(value: object) -> str:
    if type(value) is not int:
        return DASH
    sign = "-" if value < 0 else ""
    return f"{sign}{_format_top_age(abs(value))}"


def _ago(value: object, *, now_ms: int) -> str:
    if type(value) is not int:
        return DASH
    return _format_top_age(max(0, now_ms - value))


def _now_ms(data: Mapping[str, Any]) -> int:
    raw = data.get("nowMs")
    if type(raw) is int:
        return raw
    return int(time.time() * 1000)


def _rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _peer(row: Mapping[str, Any]) -> str:
    return _text(row.get("peer"))


def _counts_toward_k(row: Mapping[str, Any]) -> bool:
    lifecycle = row.get("lifecycle")
    if not isinstance(lifecycle, str):
        return True
    lowered = lifecycle.strip().lower()
    return not any(marker in lowered for marker in _TASK_BOUND_MARKERS)


def _hold_text(row: Mapping[str, Any]) -> str:
    from hyprial.daemon import HOLD_REASON_KEY

    reason = row.get(HOLD_REASON_KEY)
    if not isinstance(reason, str) or not reason:
        # Legacy free-text reason (e.g. "target-not-visible") as a fallback.
        fallback = row.get("reason")
        reason = fallback if isinstance(fallback, str) and fallback else "queued"
    detail = row.get("detail")
    return f"{reason}: {detail}" if isinstance(detail, str) and detail else reason


def _is_queued(row: Mapping[str, Any]) -> bool:
    from hyprial.daemon import HOLD_REASON_KEY

    hold = row.get(HOLD_REASON_KEY)
    return row.get("queued") is True or (
        isinstance(hold, str) and hold != ""
    )


def render_link_list(data: Mapping[str, Any]) -> str:
    """The link table: owner row first, then established/pending/reclaimed."""

    now_ms = _now_ms(data)
    links = _rows(data.get("links"))
    links.sort(
        key=lambda row: (
            _STATE_ORDER.get(str(row.get("state")), len(_STATE_ORDER)),
            _peer(row),
        )
    )
    counted = sum(
        1
        for row in links
        if row.get("state") == "established" and _counts_toward_k(row)
    )
    task_bound = sum(
        1
        for row in links
        if row.get("state") == "established" and not _counts_toward_k(row)
    )
    window = _count(data.get("W"))
    head = (
        f"{_text(data.get('actor'))}   k {_count(data.get('k'))}   W {window}"
        f"   in-flight {_count(data.get('inFlight'))}/{window}"
        f"   agent links {counted}/{_count(data.get('k'))}"
    )
    if task_bound:
        head += f" (+{task_bound} task-bound)"

    cells: list[list[str]] = []
    owner = data.get("owner")
    if isinstance(owner, Mapping):
        cells.append([_peer(owner), "owner", DASH, DASH, DASH, DASH])
    for row in links:
        cells.append(
            [
                _peer(row),
                _text(row.get("state")),
                _count(row.get("cwnd")),
                _count(row.get("inFlight")),
                _text(row.get("lifecycle")),
                _ago(row.get("lastUsedAtMs"), now_ms=now_ms),
            ]
        )
    table = tables.render_table(
        None,
        ["PEER", "STATE", "CWND", "IN-FLIGHT", "LIFECYCLE", "LAST USED"],
        cells,
        truncate_column=0,
    )
    return f"{head}\n{table}"


def render_link_stats(data: Mapping[str, Any]) -> str:
    """The window summary plus one row per link."""

    window = f"k {_count(data.get('k'))}   W {_count(data.get('W'))}"
    target = data.get("wTarget")
    if type(target) is int:
        window += f" (target {target})"
    lines = [
        _text(data.get("actor")),
        f"N_active {_count(data.get('nActive'))}   {window}",
        f"T_min {_duration(data.get('tMinMs'))}   T_bar {_duration(data.get('tBarMs'))}"
        f"   mu_max {_rate(data.get('muMax'))}   diff {_number(data.get('diff'))}"
        f"   grad {_signed_duration(data.get('grad'))}",
    ]
    cells = [
        [
            _peer(row),
            _duration(row.get("rttMinMs")),
            _duration(row.get("rttMs")),
            _count(row.get("cwnd")),
            _count(row.get("inFlight")),
        ]
        for row in _rows(data.get("links"))
    ]
    lines.append(
        tables.render_table(
            None,
            ["PEER", "RTT_MIN", "RTT", "CWND", "IN-FLIGHT"],
            cells,
            truncate_column=0,
        )
    )
    return "\n".join(lines)


def render_link_change(data: Mapping[str, Any]) -> str:
    """``link add``/``link drop``: a hold, a reclaimed edge or a reference."""

    actor = _text(data.get("actor"))
    peer = _text(data.get("peer"))
    if _is_queued(data):
        return f"held    {peer}   {_hold_text(data)}\n{RESEND_GUARD}"
    drained = data.get("drained")
    if data.get("state") == "reclaimed" or type(drained) is int:
        line = f"dropped link {actor} -> {peer}"
        if type(drained) is int:
            line += f"; drained {drained} pending request(s) as ack"
        return line
    state = _text(data.get("state"))
    if state == "pending":
        return f"link {actor} -> {peer}: pending (waiting for the peer to reference back)"
    return f"link {actor} -> {peer}: {state}"
