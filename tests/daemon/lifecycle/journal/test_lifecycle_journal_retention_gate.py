"""The journal-retention tripwire behind the U0b migration's rescue leg.

``DesiredStateSqliteShadow._journal_proved_running`` rescues long-running
harnesses from the migration's ghost-delete rule by reading completed
``harness.ensure`` effects.  That evidence exists only because NOTHING in
src/ ever prunes ``lifecycle_effects`` / ``lifecycle_operations`` -- a
positional fact, not an enforced one.  The failure mode the developer
flagged (2026-09-04) is silent: add a retention policy tomorrow and the
rescue quietly stops rescuing; long-running rows start being deleted as
ghosts and no test goes red -- unless this gate is in the suite.

Same shape as the repo's other source gates (actor_boundary_gate,
atomic_lifecycle_authority_gate): a repository-wide assertion plus
mutation-style tests proving the scanner itself has teeth.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"

# Realistic prune spellings: plain DELETE, quoted identifiers, DROP TABLE
# (with or without IF EXISTS), across any whitespace/case.
_PRUNE_PATTERN = re.compile(
    r"(DELETE\s+FROM|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s*[\"'`]?\s*"
    r"lifecycle_(effects|operations)\b",
    re.IGNORECASE,
)

# The warning that must stay anchored where the dependency is consumed; if
# the docstring is reworded away, the gate red-flags the orphaned rescue.
_ANCHOR_FRAGMENT = 'tests/daemon/lifecycle/journal/test_lifecycle_journal_retention_gate.py is the tripwire'


def scan_journal_prunes(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in _PRUNE_PATTERN.finditer(source):
            findings.append(
                f"{path.relative_to(root)} prunes the durable journal "
                f"({match.group(0).split()[0].upper()} ... "
                f"lifecycle_{match.group(2)}): the U0b migration's "
                "ghost-delete rescue reads completed harness.ensure effects "
                "from these tables -- see the POSITIONAL DEPENDENCY note in "
                "desired_state_sqlite._journal_proved_running and decide the "
                "migration's evidence story together with the pruning"
            )
    return findings


def test_journal_tables_are_never_pruned_in_src() -> None:
    findings = scan_journal_prunes(SRC)
    assert findings == [], "journal prune found -- the migration rescue "
    "silently loses its evidence:\n" + "\n".join(findings)


def test_rescue_warning_stays_anchored_at_the_dependency() -> None:
    source = (SRC / "daemon" / "impl" / "desired_state" / "sqlite.py").read_text(
        encoding="utf-8"
    )
    assert _ANCHOR_FRAGMENT in source, (
        "the POSITIONAL DEPENDENCY warning no longer points at this gate "
        "from _journal_proved_running -- keep the comment and the tripwire "
        "wired to each other"
    )


def test_mutation_delete_from_effects_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "prune.py").write_text(
        'db.execute("DELETE FROM lifecycle_effects WHERE updated_at < ?")\n',
        encoding="utf-8",
    )
    findings = scan_journal_prunes(tmp_path)
    assert len(findings) == 1
    assert "lifecycle_effects" in findings[0]


def test_mutation_quoted_drop_table_operations_is_flagged(
    tmp_path: Path,
) -> None:
    (tmp_path / "prune.py").write_text(
        'db.executescript(\'DROP TABLE IF EXISTS "lifecycle_operations";\')\n',
        encoding="utf-8",
    )
    findings = scan_journal_prunes(tmp_path)
    assert len(findings) == 1
    assert "lifecycle_operations" in findings[0]


def test_benign_journal_sql_stays_green(tmp_path: Path) -> None:
    (tmp_path / "journal.py").write_text(
        'row = db.execute(\n'
        '    "SELECT request_json FROM lifecycle_operations '
        'WHERE operation_id = ?",\n'
        '    (operation_id,),\n'
        ').fetchone()\n'
        'db.execute(\n'
        '    "UPDATE lifecycle_effects SET status = \'completed\' "\n'
        '    "WHERE operation_id = ?",\n'
        '    (operation_id,),\n'
        ')\n'
        'db.executescript(\n'
        '    "CREATE TABLE IF NOT EXISTS lifecycle_effects "\n'
        '    "(operation_id TEXT NOT NULL);"\n'
        ')\n',
        encoding="utf-8",
    )
    assert scan_journal_prunes(tmp_path) == []
