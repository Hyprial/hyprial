"""An e2e failure must not be trimmed before the part that explains it.

`Report.add` caps a detail at a thousand characters. `hyprial daemon status --json` orders its
keys `daemon`, `zenoh`, `connectors`, and since endpoints stopped being
persisted the `zenoh` value carries forty-odd tailnet addresses. The cap
therefore fell inside `zenoh`, and the E2E-006 / E2E-012 restart-restore
failures reported this and nothing else:

    condition did not become true; last={'ok': True, 'daemon': {'running': True,
    ...}, 'zenoh': {'listen': [], 'connect': ['tcp/100.64.10.5:7447', ... ␘

⚠️ `wait_running` waits on the **connector** status. That key never appeared.
Three scheduled runs across three shas failed identically, and the report
described a daemon that was running fine.

🔑 The surviving half made the failure look benign; the half that would have
named the cause was never written. This is the same loss as a stack dump landing
in a file the next launch replaces -- one record, two halves, and the one that
mattered had no budget of its own.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / 'contract/e2e'))

import pytest  # noqa: E402

from _daemon_runner import IsolatedDaemon, bounded_repr  # noqa: E402


def _ps_like() -> dict[str, object]:
    """Shaped like the real probe: a noisy key ahead of the decisive one."""

    return {
        "ok": True,
        "daemon": {"running": True, "pid": 43723, "nodeId": "codex-e2e012-node"},
        "zenoh": {
            "listen": [],
            "connect": [f"tcp/100.{n}.{n}.{n}:7447" for n in range(40)],
        },
        "connectors": [{"name": "codex-worker", "running": False}],
    }


def test_the_decisive_key_survives_the_report_cap() -> None:
    """The regression itself: `connectors` must be inside the first 1000 chars.

    ⚠️ Asserted against the real cap rather than "is present anywhere", because
    present-but-trimmed-away is exactly the state that produced three
    indistinguishable failure reports.
    """

    rendered = bounded_repr(_ps_like())[:1000]

    assert "'connectors'" in rendered, (
        "the connector status is cut off before the cap, so a restart-restore "
        f"failure still reports only that the daemon is running: {rendered[-200:]!r}"
    )
    assert "'running': False" in rendered, (
        "the connector key survived but its contents did not; the report names "
        "the right field and still cannot say what it held"
    )


def test_the_noisy_key_is_shortened_rather_than_dropped() -> None:
    """Eliding is not the same as deleting.

    A reader who cannot see `zenoh` at all would not know it was there, and
    `listen: []` is itself a fact worth keeping. The point is to bound it, not
    to decide for the reader which keys matter -- that decision is what went
    wrong when the cap made it implicitly, by position.
    """

    rendered = bounded_repr(_ps_like())

    assert "'zenoh'" in rendered
    assert "'listen': []" in rendered, (
        "the elision removed the beginning of the value; a truncation that "
        "keeps the tail rather than the head hides what was declared"
    )
    assert "chars)" in rendered, "no sign that anything was elided"


def test_the_wait_helper_actually_uses_it() -> None:
    """⚠️ The test that was missing, and the omission is the session's own theme.

    The three above exercise `bounded_repr` directly. Reverting `wait` to
    `last!r` -- undoing the entire fix -- left all three green, because none of
    them went through the call site. A helper that is correct and unreached is
    the same as no helper, and it reads better.

    So this drives the real failure path and asserts on the message a scenario
    would actually print.
    """

    def probe() -> dict[str, object]:
        return _ps_like()

    with pytest.raises(AssertionError) as failure:
        IsolatedDaemon.wait(object(), probe, lambda _value: False, timeout=0.3)

    message = str(failure.value)[:1000]
    assert "'connectors'" in message, (
        "the wait helper still renders the raw probe result, so the report is "
        f"cut off before the field it was waiting on: {message[-160:]!r}"
    )


def test_a_short_probe_result_is_left_alone() -> None:
    """The control. If this rewrote small results too, every passing wait would
    read differently from the command it ran, and the next person debugging a
    scenario would be reading a paraphrase without knowing it."""

    small = {"ok": True, "connectors": []}
    assert bounded_repr(small) == "'ok': True, 'connectors': []".join(("{", "}"))
