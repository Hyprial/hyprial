"""The supervision-boundary gate, and proof that it can tell red from green.

Step 1 of ``docs/notes/supervision-consolidation-standard-2026-08-29.md`` §5: stand
the gate up and find the *real* full set.  It is deliberately not gating yet --
the manifest starts holding every existing site, so the repository is green
today and goes red the moment a new one appears.

⚠️ "Report-only" is not "asserts nothing."  §2 says a permanently-green gate is
worse than none, so report-only here means *the manifest is not empty yet*, not
*nothing is checked*.  Three things are checked hard:

* the manifest matches reality exactly, in both directions
* the scanner still finds the sites the standard named by hand (recall)
* the rules discriminate, proven by mutation on ``tmp_path`` rather than by
  the repository happening to be green

The recall fixture is the one that would be easy to leave out and expensive to
lack.  If the scanner silently stops seeing a site, every manifest entry for it
reads as "stale -- migration finished", and the remedy the failure message asks
for (delete the entry) leaves that site unguarded with the gate green.  A check
that stopped running looks exactly like a check that passed.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.gates.supervision_boundary_gate import Finding, names_a_managed_concept, owned_symbols, scan_blocking_waits, scan_retry_backoff, scan_supervision_sites, validate_supervision_manifest

SRC = Path(__file__).resolve().parents[4] / 'src/hyprial'
MANIFEST = Path(__file__).resolve().parents[4] / 'tests/supervision_exemptions.json'

#: Transcribed from the standard's §2 table, which was compiled by hand before
#: this scanner existed.  That independent provenance is the whole value: the
#: manifest is generated *from* the scanner and so cannot detect the scanner
#: going blind, whereas this list can.
#:
#: One entry expands an abbreviation in that table rather than quote it --
#: ``startup_timeout`` for its "worker ready 信号 + launcher 自己的超时" -- so
#: its exact spelling was read back from the source file.  The rest are
#: quoted verbatim.
#:
#: ``HarnessStartTimerElapsedCommand`` is listed under ``harness_ports.py``
#: because that is where it is defined; the table filed it under
#: ``harness_actor.py``, which imports it.  Pinning the corrected location here
#: means a later edit cannot quietly restore the table's version.
#:
#: 2026-09-02: the harness quarantine policy was deleted outright (retrying /
#: failed two-state split; failed is terminal, no cooldown self-heal).  The
#: table's ``_quarantine_threshold`` / ``_quarantine_cooldown_*`` sites went
#: with it -- renamed to ``failure_budget`` or removed, names that
#: deliberately carry no quarantine vocabulary.  Verified by reading the
#: source: this is a removed site, not a scanner blind spot, so the two
#: entries leave the recall list.
#:
#: 2026-09-04 (U0BRACE): ``_BOOTSTRAP_REPORT_SECONDS`` left the same way --
#: the construction-phase bootstrap submissions it audited were the second
#: owner of every non-Lark running row and were deleted with their audit.
#: Verified by reading the source: removed site, not a scanner blind spot.
NAMED_BY_THE_STANDARD = (
    ("src/hyprial/daemon/impl/processes/supervisor.py", "_start_timeout_seconds"),
    ("src/hyprial/daemon/impl/harnesses/actor/actor.py", "start_timeout_seconds"),
    ("src/hyprial/daemon/impl/harnesses/actor/behaviors/lifecycle.py", "_starts_in_flight"),
    ("src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery.py", "_on_start_timeout"),
    ("src/hyprial/daemon/impl/harnesses/runtime/ports.py", "HarnessStartTimerElapsedCommand"),
    ("src/hyprial/daemon/impl/adapters/lark/worker/process.py", "startup_timeout"),
    ("src/hyprial/shell/impl/cli/commands/common/support.py", "DAEMON_START_HARD_CAP_SECONDS"),
    ("src/hyprial/shell/impl/cli/commands/common/support.py", "DAEMON_START_IDLE_BUDGET_SECONDS"),
    ("src/hyprial/shell/impl/cli/commands/common/daemon_start.py", "ready_timeout"),
)


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _manifest(root: Path, entries: list[dict[str, str]]) -> Path:
    path = root / "manifest.json"
    path.write_text(
        json.dumps({"schema_version": 1, "policy": "shrinking-only", "exemptions": entries}),
        encoding="utf-8",
    )
    return path


def _entry(path: str, symbol: str) -> dict[str, str]:
    return {
        "path": path,
        "symbol": symbol,
        "owner": "hyprial-developer",
        "card": "5c8743093ff5",
        "reason": "test fixture",
    }


# ── the repository's current state ────────────────────────────────────────


def test_the_manifest_matches_the_repository_exactly() -> None:
    """Both directions, per §2: a new site is a regression, a stale one is dead code."""

    errors = validate_supervision_manifest(SRC, MANIFEST)
    assert errors == [], "supervision manifest drift:\n" + "\n".join(errors)


def test_the_scanner_still_finds_every_site_the_standard_named() -> None:
    """Recall, asserted against a list written before the rules were tuned.

    Without this, a scanner that quietly went blind would present as a
    completed migration: all its manifest entries turn "stale", and deleting
    them -- what the message tells you to do -- is exactly wrong.
    """

    found = owned_symbols(SRC)
    missing = [key for key in NAMED_BY_THE_STANDARD if key not in found]
    assert not missing, (
        "the scanner no longer sees sites the standard named by hand: "
        + ", ".join(f"{path}:{symbol}" for path, symbol in missing)
        + " -- treat this as a detector gap, not as a finished migration, and "
        "do NOT delete their manifest entries"
    )


def test_the_managed_sites_are_a_larger_set_than_the_hand_written_five() -> None:
    """The finding this step exists to produce, pinned so it cannot regress quietly.

    hq-adjutant's table named five modules and flagged that it was probably
    incomplete.  It is: the whole ``harnesses/`` family owns its own startup
    budget, and the port types live in ``harness_ports.py`` rather than in
    ``harness_actor.py`` where the table filed them.
    """

    modules = {path for path, _ in owned_symbols(SRC)}
    # 2026-09-04 (U0BRACE): ``application.py`` left this set the sanctioned
    # way -- its one owned symbol (``_BOOTSTRAP_REPORT_SECONDS``) was
    # deleted with the bootstrap audit, and the scanner now finds nothing
    # in that module.  Verified by reading the source: removed site, not a
    # scanner blind spot (the other four modules still report).
    hand_written = {
        "src/hyprial/daemon/impl/processes/supervisor.py",
        "src/hyprial/daemon/impl/harnesses/actor/actor.py",
        "src/hyprial/daemon/impl/adapters/lark/worker/process.py",
        "src/hyprial/shell/impl/cli/commands/common/support.py",
    }
    assert hand_written <= modules, "a hand-named module vanished from the scan"
    assert len(modules) > len(hand_written), (
        "the scan no longer exceeds the hand-written five; either the "
        "migration really finished or the detector narrowed"
    )


# ── discriminating power: the rules, proven on synthetic sources ──────────


def test_mutation_a_new_self_built_start_timeout_is_caught(tmp_path: Path) -> None:
    """§2 mutation 1: plant a self-built budget in a 'migrated' module."""

    _write(tmp_path, "clean.py", "TIMEOUT = 5.0\n")
    assert owned_symbols(tmp_path) == set(), (
        "a bare 'timeout' must not match, or the gate drowns in RPC and socket "
        "timeouts and nobody reads it"
    )

    _write(tmp_path, "clean.py", "START_TIMEOUT_SECONDS = 5.0\n")
    assert owned_symbols(tmp_path) == {("src/hyprial/clean.py", "START_TIMEOUT_SECONDS")}


def test_mutation_dropping_a_live_entry_from_the_manifest_is_caught(
    tmp_path: Path,
) -> None:
    """§2 mutation 2: the site is still there, the exemption is gone."""

    _write(tmp_path, "site.py", "START_TIMEOUT_SECONDS = 5.0\n")
    errors = validate_supervision_manifest(tmp_path, _manifest(tmp_path, []))
    assert any("unmanifested self-built supervision" in error for error in errors), errors


def test_mutation_keeping_an_entry_for_a_removed_site_is_caught(
    tmp_path: Path,
) -> None:
    """§2 mutation 3: the migration happened, the exemption became dead code."""

    _write(tmp_path, "site.py", "PLAIN = 1\n")
    manifest = _manifest(tmp_path, [_entry("src/hyprial/site.py", "START_TIMEOUT_SECONDS")])
    errors = validate_supervision_manifest(tmp_path, manifest)
    assert any("stale supervision exemption must shrink now" in error for error in errors), errors


def test_the_manifest_requires_an_owner_and_a_card(tmp_path: Path) -> None:
    """An exemption nobody owns is a permanent one."""

    _write(tmp_path, "site.py", "PLAIN = 1\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": "shrinking-only",
                "exemptions": [{"path": "src/hyprial/site.py", "symbol": "X", "reason": "r"}],
            }
        ),
        encoding="utf-8",
    )
    errors = validate_supervision_manifest(tmp_path, manifest)
    assert any("owner, card" in error for error in errors), errors


def test_the_compound_rule_separates_supervision_from_every_other_budget() -> None:
    """The whole reason this is not a grep.

    Each right-hand name is real and lives in this repository; matching them
    would have buried the real sites under noise, and a gate nobody can read
    is a gate nobody runs.
    """

    for name in (
        "start_timeout_seconds",
        "startup_timeout",
        "ready_timeout",
        "quarantine_threshold",
        "_starts_in_flight",
        "HarnessStartTimerElapsedCommand",
        "start_backoff_max_seconds",
    ):
        assert names_a_managed_concept(name), f"lost a real site: {name}"

    for name in (
        "timeout",  # cli RPC + socket connect
        "backoff_ms",  # inbox delivery retry
        "MAX_BACKOFF_SECONDS",  # usage API rate limiting
        "REPORT_RETRY_BACKOFF_MS",  # workflow effect retry
        "AGENT_HEARTBEAT_TTL_SECONDS",
        "CHANNEL_LIVENESS_TTL_SECONDS",
        "_EXIT_BACKSTOP_SECONDS",  # shutdown budget, not a start budget
        "MAX_START_BYTES",  # a payload cap that happens to say 'start'
    ):
        assert not names_a_managed_concept(name), f"false positive: {name}"


def test_the_blocking_wait_rule_finds_inlined_budgets_and_ignores_plain_reads(
    tmp_path: Path,
) -> None:
    """Capability #1 has no constant to name, so the name rule cannot see it.

    Proven on synthetic sources: the repository being green (or not) says
    nothing about whether this rule works.
    """

    _write(
        tmp_path,
        "site.py",
        "def start_worker(self):\n"
        "    self._ready.wait(60.0)\n"
        "\n"
        "def read_queue(self):\n"
        "    self._queue.get(timeout=5.0)\n",
    )
    found = {finding.symbol for finding in scan_blocking_waits(tmp_path)}
    assert "start_worker/wait" in found, "an inlined start budget went unseen"
    assert "read_queue/get" not in found, (
        "an ordinary queue read was reported; unscoped, this rule matches every "
        "blocking call in the repository and becomes unreadable"
    )


def test_retry_backoff_is_reported_separately_and_never_manifested() -> None:
    """§1.4 says 'retry and backoff'; every §0 incident is about *starting*.

    Whether the capability covers delivery retry, API rate limiting and poll
    backoff is the standard's call, so those are surfaced as their own list
    rather than silently folded in (which would have quadrupled the migration)
    or silently dropped (which would have hidden the question).
    """

    tier_two = {finding.key for finding in scan_retry_backoff(SRC)}
    assert tier_two, "the second tier vanished; the question it carries would vanish with it"
    assert not (tier_two & owned_symbols(SRC)), (
        "a symbol is in both tiers; they must partition, or one tier's silence "
        "stands in for the other's verdict"
    )
    manifested = {
        (entry["path"], entry["symbol"])
        for entry in json.loads(MANIFEST.read_text(encoding="utf-8"))["exemptions"]
    }
    assert not (tier_two & manifested), "tier two must not be manifested"


def test_update_probe_retry_is_registered_as_non_supervision_retry() -> None:
    tier_two = {finding.key for finding in scan_retry_backoff(SRC)}
    assert ("src/hyprial/daemon/impl/updates/installation.py", "LS_REMOTE_RETRY_COUNT") in tier_two


def test_core_defines_the_capabilities_and_is_therefore_exempt(tmp_path: Path) -> None:
    """A hit inside ``actor_runtime`` is core doing its job, not a violation."""

    _write(tmp_path, "kernel/impl/actor_runtime/api.py", "START_TIMEOUT_SECONDS = 5.0\n")
    assert owned_symbols(tmp_path) == set()

    _write(tmp_path, "daemon/impl/api.py", "START_TIMEOUT_SECONDS = 5.0\n")
    assert owned_symbols(tmp_path) == {("src/hyprial/daemon/impl/api.py", "START_TIMEOUT_SECONDS")}


def test_findings_are_keyed_without_line_numbers() -> None:
    """Otherwise every unrelated edit above a site reddens the gate.

    A gate that goes red for reasons unrelated to its subject gets switched
    off, and then it protects nothing.
    """

    assert Finding("p", "s", "field", 10).key == Finding("p", "s", "field", 99).key
    assert scan_supervision_sites(SRC), "the scan found nothing at all"
