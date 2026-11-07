"""Every SQLite store in WAL mode commits with ``synchronous=NORMAL``.

Allen 2026-09-23 adopted SQLite's own recommendation for WAL: "The
synchronous=NORMAL setting provides the best balance between performance and
safety for most applications running in WAL mode" (sqlite.org/pragma.html).
WAL with NORMAL is safe from corruption; a transaction committed just before
a power loss or OS crash may roll back.  A process crash loses nothing.

Why it matters here: under FULL every commit waited for an fsync, and on a
contended disk that wait sat inside time budgets it was never meant to
consume -- a 2 s lifecycle completion timeout, a 1 s custody wait, a busy
timeout another writer spent queueing behind a flushing lock holder.  Four
intermittent CI failures traced to that one wait.

⚠️ NORMAL is only corruption-safe in WAL mode, so the rule is paired: a
module that turns WAL on must also set NORMAL, and nothing may ask for FULL
or OFF.  Stores that do not use WAL keep SQLite's default and are left alone.

Exception (Allen 2026-09-23, "unless there is an explicit reason"): a line may
ask for FULL only if it carries, on the same line, a marker
``# durability: FULL -- <reason>`` with a non-empty reason, so the choice is
written down where a reviewer reads it.  OFF has no exception.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "src" / "hyprial"

_WAL = re.compile(r"PRAGMA\s+journal_mode\s*=\s*WAL", re.IGNORECASE)
_SYNC = re.compile(r"PRAGMA\s+synchronous\s*=\s*([A-Za-z0-9]+)", re.IGNORECASE)
# The reason must start with a word character: a separator alone, or dashes
# only, is not a reason (an earlier `\S` accepted "-- " by matching a dash).
_JUSTIFIED_FULL = re.compile(r"#\s*durability:\s*FULL\s*(?:--|—)\s*\w")


def _violations(text: str) -> list[str]:
    """Each non-NORMAL setting that is not a justified FULL, as `VALUE@line`."""

    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for value in _SYNC.findall(line):
            value = value.upper()
            if value == "NORMAL":
                continue
            if value == "FULL" and _JUSTIFIED_FULL.search(line):
                continue
            found.append(f"{value}@{number}")
    return found


def _modules() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in SOURCE.rglob("*.py")}


def test_every_wal_store_sets_synchronous_normal() -> None:
    wal_modules = {path: text for path, text in _modules().items() if _WAL.search(text)}
    # Denominator: an empty match would make this test vacuously green.
    # 8, not 9: #708 retired src/hyprial/workflow/store.py (the v1 workflow
    # store), which was one of the nine WAL stores when this test was written.
    assert len(wal_modules) >= 8, sorted(str(p.relative_to(ROOT)) for p in wal_modules)
    missing = [
        str(path.relative_to(ROOT))
        for path, text in wal_modules.items()
        if not _SYNC.findall(text) or _violations(text)
    ]
    assert not missing, (
        f"WAL stores without synchronous=NORMAL or a justified FULL: {missing}"
    )


def test_no_store_asks_for_full_or_off_without_a_written_reason() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}: synchronous={hit}"
        for path, text in _modules().items()
        for hit in _violations(text)
    ]
    assert not offenders, (
        f"{offenders}: use NORMAL, or justify FULL on the same line with "
        "`# durability: FULL -- <reason>`"
    )


def test_the_pattern_recognises_what_it_forbids() -> None:
    # Positive control: the checks above must be able to see a violation.
    assert _SYNC.findall('db.execute("PRAGMA synchronous=FULL")') == ["FULL"]
    assert _SYNC.findall("PRAGMA synchronous = OFF") == ["OFF"]
    assert _WAL.search('connection.execute("PRAGMA journal_mode=WAL")')


def test_only_a_written_reason_admits_full() -> None:
    bare = 'db.execute("PRAGMA synchronous=FULL")'
    justified = (
        'db.execute("PRAGMA synchronous=FULL")  '
        "# durability: FULL -- payment ledger must survive power loss"
    )
    empty_reason = 'db.execute("PRAGMA synchronous=FULL")  # durability: FULL -- '
    assert _violations(bare) == ["FULL@1"]
    assert _violations(justified) == []
    assert _violations(empty_reason) == ["FULL@1"]
    # OFF has no exception, even with a marker.
    assert _violations('db.execute("PRAGMA synchronous=OFF")  # durability: FULL -- x') == [
        "OFF@1"
    ]
