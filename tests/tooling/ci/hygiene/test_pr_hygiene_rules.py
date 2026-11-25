"""Pure-logic contracts for scripts/ci/check_pr_hygiene.py.

These drive the readers directly (no git, no network). The git repros for
merge and registry rules live in test_pr_hygiene_git.py; identity and retired
source-policy compatibility are covered here.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_pr_hygiene.py"
SPEC = importlib.util.spec_from_file_location("check_pr_hygiene", SCRIPT)
hygiene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hygiene)


@pytest.mark.parametrize(
    "base,head",
    [
        ("dev", "feat/example"),
        ("dev", "fix/example"),
        ("dev", "intg/example"),
        ("dev", "docs/example"),
        ("dev", "ci/example"),
        ("dev", "release/v0.5.5.4"),
        ("dev", "hotfix/example"),
        ("release/v0.5.5.4", "feat/example"),
        ("main", "dev"),
        ("", ""),
    ],
)
def test_retired_source_branch_cli_accepts_legacy_workflow_invocations(base, head, capsys):
    assert hygiene.main([
        "source-branch", "--base-ref", base, "--head-ref", head,
    ]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "retired" in captured.out
    assert "dev-first" in captured.out


def test_workflows_do_not_reintroduce_release_first_admission():
    from tests.support.ci.ci_workflow_policy import runner_jobs

    invocations = [
        (job.key, step["name"])
        for job in runner_jobs()
        for step in job.steps
        if "source-branch --base-ref" in step.get("run", "")
    ]
    assert invocations == []


def identity_config(tmp_path: Path, **overrides) -> Path:
    config = {
        "version": 1,
        "required_shape": "Submitted-by: agent:<URI>",
        "required_line": "^Submitted-by: agent:[^\\s<>]+$",
        "cutoff": "2026-10-11T00:00:00Z",
        "exempt_pr_numbers": [],
        "upgrade_pr_numbers": [],
    }
    config.update(overrides)
    path = tmp_path / "pr-identity.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


CUTOFF = "2026-10-11T00:00:00Z"
VALID = "Submitted-by: agent:owner@example.com:example-hq.example:wf-1"


# --- rule 4: body reader -----------------------------------------------------


def test_valid_first_line_is_green(tmp_path):
    errors, warnings = hygiene.identity_errors(
        f"{VALID}\n\n## Summary\n", identity_config(tmp_path), "10", CUTOFF
    )
    assert (errors, warnings) == ([], [])


@pytest.mark.parametrize(
    "body",
    [
        f"\ufeff{VALID}\n",
        f"\n\n   \n{VALID}\n\n## Summary\n",
        f"<!-- template prose -->\n{VALID}\n",
        f"<!-- a\nmultiline\ncomment -->\n\n{VALID}\n",
        f"<!-- first -->\n<!-- second -->\n{VALID}\n",
        f"<!-- inline --> {VALID}\n",
    ],
)
def test_bom_whitespace_and_leading_comments_are_skipped(tmp_path, body):
    errors, _ = hygiene.identity_errors(body, identity_config(tmp_path), "10", CUTOFF)
    assert errors == []


def test_missing_line_on_a_new_pr_is_red(tmp_path):
    errors, warnings = hygiene.identity_errors(
        "## Summary\n\nno identity line\n", identity_config(tmp_path), "10", CUTOFF
    )
    assert warnings == []
    assert len(errors) == 1
    assert "missing:" in errors[0] and "Submitted-by" in errors[0]


def test_line_present_but_not_first_is_red_for_a_new_pr(tmp_path):
    """#1344's shape: the line is there, but a heading comes first."""

    body = f"## Summary\n\n{VALID}\n"
    errors, warnings = hygiene.identity_errors(body, identity_config(tmp_path), "1344", CUTOFF)
    assert warnings == []
    assert len(errors) == 1
    assert "present-but-not-first-line:" in errors[0]


def test_line_present_but_not_first_warns_for_an_old_pr(tmp_path):
    """#1344 is in-flight, so the same shape warns rather than fails."""

    body = f"## Summary\n\n{VALID}\n"
    errors, warnings = hygiene.identity_errors(
        body, identity_config(tmp_path), "1344", "2026-10-09T12:00:00+08:00"
    )
    assert errors == []
    assert len(warnings) == 1 and "present-but-not-first-line:" in warnings[0]


def test_upgrade_pr_number_forces_red_for_an_old_pr(tmp_path):
    errors, warnings = hygiene.identity_errors(
        "## Summary\n",
        identity_config(tmp_path, upgrade_pr_numbers=[1310]),
        "1310",
        "2026-10-09T12:00:00+08:00",
    )
    assert warnings == []
    assert len(errors) == 1 and "named for upgrade" in errors[0]


def test_exempt_pr_number_wins_over_an_upgrade(tmp_path):
    errors, warnings = hygiene.identity_errors(
        "## Summary\n",
        identity_config(tmp_path, exempt_pr_numbers=[4242], upgrade_pr_numbers=[4242]),
        "4242",
        "2026-12-01T00:00:00Z",
    )
    assert errors == []
    assert len(warnings) == 1 and "allowlist" in warnings[0]


def test_empty_body_is_red(tmp_path):
    errors, _ = hygiene.identity_errors("", identity_config(tmp_path), "10", CUTOFF)
    assert len(errors) == 1


def test_leading_comment_only_body_is_red(tmp_path):
    errors, _ = hygiene.identity_errors(
        "<!-- only a comment -->\n\n", identity_config(tmp_path), "10", CUTOFF
    )
    assert len(errors) == 1


def test_unterminated_leading_comment_fails_closed(tmp_path):
    errors, _ = hygiene.identity_errors(
        "<!-- never closed\nSubmitted-by: agent:x\n",
        identity_config(tmp_path),
        "10",
        CUTOFF,
    )
    assert len(errors) == 1


def test_pr_created_before_the_cutoff_warns_only(tmp_path):
    errors, warnings = hygiene.identity_errors(
        "## Summary\n", identity_config(tmp_path), "10", "2026-10-09T10:49:58+08:00"
    )
    assert errors == []
    assert len(warnings) == 1
    assert "permanent warn-only" in warnings[0]
    # The criterion is the creation time only: a later push cannot upgrade it.
    after_push = hygiene.identity_errors(
        "## Summary\n", identity_config(tmp_path), "10", "2026-10-09T10:49:58+08:00"
    )
    assert after_push == (errors, warnings)


def test_pr_created_at_the_cutoff_is_red(tmp_path):
    errors, warnings = hygiene.identity_errors(
        "## Summary\n", identity_config(tmp_path), "10", CUTOFF
    )
    assert warnings == [] and len(errors) == 1


def test_missing_creation_time_fails_closed(tmp_path):
    errors, _ = hygiene.identity_errors(
        "## Summary\n", identity_config(tmp_path), "10", None
    )
    assert len(errors) == 1


def test_allowlisted_pr_number_warns_only_even_when_new(tmp_path):
    errors, warnings = hygiene.identity_errors(
        "## Summary\n",
        identity_config(tmp_path, exempt_pr_numbers=[4242]),
        "4242",
        "2026-12-01T00:00:00Z",
    )
    assert errors == []
    assert len(warnings) == 1 and "allowlist" in warnings[0]


@pytest.mark.parametrize(
    "line",
    [
        "Submitted-by: agent:<URI>",  # the template placeholder is not an identity
        "Submitted-by: agent:",  # empty URI
        "Submitted-by: someone",  # wrong shape
        "submitted-by: agent:x",  # case matters
    ],
)
def test_near_miss_lines_are_rejected(tmp_path, line):
    errors, _ = hygiene.identity_errors(
        f"{line}\n", identity_config(tmp_path), "10", CUTOFF
    )
    assert len(errors) == 1


def test_surrounding_whitespace_on_the_line_is_stripped(tmp_path):
    errors, _ = hygiene.identity_errors(
        f"   {VALID}   \n", identity_config(tmp_path), "10", CUTOFF
    )
    assert errors == []


def test_real_identity_config_is_coherent():
    config = json.loads((ROOT / "scripts" / "ci" / "pr-identity.json").read_text())
    assert re.fullmatch(config["required_line"], VALID)
    assert not re.fullmatch(config["required_line"], "Submitted-by: agent:<URI>")
    assert datetime.fromisoformat(config["cutoff"].replace("Z", "+00:00")).tzinfo
    assert config["exempt_pr_numbers"] == [] and config["upgrade_pr_numbers"] == []


# 2026-10-09 FROZEN SNAPSHOT of e2e-verifier's open-PR enforcement surface,
# vendored verbatim from
#   e2e-resident/notes/submitted-by-fixtures-2026-10-09.json
# at e2e-resident `c21676e` (scanned 2026-10-09T13:31:25Z).  It holds 20 rows:
# 18 `missing`, 1 `not-first-line` (#1344), 1 `ok` (#1165).  (The landing brief
# said "19 missing"; the file it names holds 18 -- #1310 is NOT in the frozen
# file, it appears only in the earlier live scan.  The test pins the file.)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SNAPSHOT_2026_10_09 = FIXTURE_DIR / "submitted-by-fixtures-2026-10-09.json"

# The fixture's coarse schema (ok | not-first-line | missing) maps onto this
# gate's fine vocabulary; a `missing` row must NOT be misreported as a near-form.
_SNAPSHOT_VERDICT_TO_STATE = {
    "ok": hygiene.OK,
    "not-first-line": hygiene.PRESENT_BUT_NOT_FIRST,
    "missing": hygiene.MISSING,
}


def _snapshot_body(row: dict) -> str:
    """A body that reproduces the row's recorded verdict.

    The frozen file stores only the first line, not the whole body, so the
    non-first marker the `not-first-line` row needs is appended here.  The
    `missing` rows' real first lines are prose, so they must stay `missing`.
    """

    first = str(row["first_line"])
    if row["verdict"] == "ok":
        return f"{first}\n\n## Summary\n"
    if row["verdict"] == "not-first-line":
        return f"{first}\n\n{VALID}\n"
    return f"{first}\n\n(no identity line)\n"


def test_2026_10_09_frozen_snapshot_verdicts_are_reproduced():
    """Every frozen row's coarse verdict comes back from the new classifier."""

    snapshot = json.loads(SNAPSHOT_2026_10_09.read_text(encoding="utf-8"))
    counts = {}
    for row in snapshot["prs"]:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    assert counts == {"missing": 18, "not-first-line": 1, "ok": 1}

    required_line = json.loads(
        (ROOT / "scripts" / "ci" / "pr-identity.json").read_text(encoding="utf-8")
    )["required_line"]
    for row in snapshot["prs"]:
        state, _line = hygiene.identity_state(_snapshot_body(row), required_line)
        assert state == _SNAPSHOT_VERDICT_TO_STATE[row["verdict"]], row["number"]


# e2e-verifier's 13-case self-test table (instruments/submitted-by-scan.sh
# --selftest, 2026-10-09).  Both implementations must keep producing the same
# verdicts, or "comparable" is prose.  The `not-first-line` verdict is spelled
# `present-but-not-first-line` here (this gate's existing name for it).
E2E_SELFTEST_CASES: tuple[tuple[str, str, str], ...] = (
    ("01 ok: marker first line", f"{VALID}\n\n## Summary\nbody", hygiene.OK),
    (
        "02 ok: BOM + blank + HTML comment",
        f"\ufeff\n<!-- template note -->\n{VALID}\n\n## Summary",
        hygiene.OK,
    ),
    (
        "02b ok: MULTI-LINE HTML comment",
        f"<!-- line one\n     line two -->\n{VALID}\n",
        hygiene.OK,
    ),
    ("03 ok: surrounding whitespace + CRLF", f"  {VALID}  \r\n\r\n## Summary\r\n", hygiene.OK),
    (
        "04 not-first-line (e.g. #1344)",
        f"## Summary\n\ntext\n{VALID}\n",
        hygiene.PRESENT_BUT_NOT_FIRST,
    ),
    ("05 missing: absent", "## Summary\n\nno marker here\n", hygiene.MISSING),
    (
        "06 missing: lowercase marker",
        "submitted-by: agent:x@y:z:w\n",
        hygiene.MISSING_WRONG_CASE,
    ),
    (
        "07 missing: capital B",
        "Submitted-By: agent:x@y:z:w\n",
        hygiene.MISSING_WRONG_CASE,
    ),
    (
        "08 missing: no agent: prefix",
        "Submitted-by: x@y:z:w\n",
        hygiene.MISSING_NO_AGENT_PREFIX,
    ),
    (
        "09 missing: empty uri after agent:",
        "Submitted-by: agent:\n",
        hygiene.MISSING_EMPTY_URI,
    ),
    (
        "10 conflict set: three markers",
        f"{VALID}\nSkill-Impact: updated\nContract-Impact: not-needed: docs only\n",
        hygiene.OK,
    ),
    ("11 comments only, no marker", "<!-- only a comment -->\n", hygiene.MISSING),
    (
        "12 unclosed leading comment (fail closed)",
        f"<!-- never closed\n{VALID}\n",
        hygiene.BLOCKED_UNCLOSED_COMMENT,
    ),
)


def test_e2e_selftest_table_verdicts_are_reproduced():
    required_line = json.loads(
        (ROOT / "scripts" / "ci" / "pr-identity.json").read_text(encoding="utf-8")
    )["required_line"]
    for name, body, expected in E2E_SELFTEST_CASES:
        state, _line = hygiene.identity_state(body, required_line)
        assert state == expected, name
    assert len(E2E_SELFTEST_CASES) == 13


def test_near_form_reasons_name_and_quote_what_was_seen(tmp_path):
    """Each near-form failure names the mistake AND quotes the offending line."""

    config = identity_config(tmp_path)
    for line, needle in (
        ("submitted-by: agent:x", hygiene.MISSING_WRONG_CASE),
        ("Submitted-by: x@y:z:w", hygiene.MISSING_NO_AGENT_PREFIX),
        ("Submitted-by: agent:", hygiene.MISSING_EMPTY_URI),
    ):
        errors, warnings = hygiene.identity_errors(
            f"{line}\n", config, "10", CUTOFF
        )
        assert warnings == [], line
        assert len(errors) == 1, line
        assert needle in errors[0], line
        assert line in errors[0], line  # the offending first line is quoted


def test_the_template_placeholder_is_still_rejected(tmp_path):
    """e2e's prefix-level `ok` would accept `agent:<URI>`; this gate must not."""

    errors, warnings = hygiene.identity_errors(
        "Submitted-by: agent:<URI>\n", identity_config(tmp_path), "10", CUTOFF
    )
    assert warnings == []
    assert len(errors) == 1 and "missing:" in errors[0]
    assert "<URI>" in errors[0]


# --- rule 3: registry union reader ------------------------------------------


def test_registry_entries_reads_dict_and_set_literals():
    assert hygiene.registry_entries("NATIVE_ORIGINS = {'a': 1, 'b': 2}\n", "NATIVE_ORIGINS") == {
        "a",
        "b",
    }
    assert hygiene.registry_entries('X = {"a", "b"}\n', "X") == {"a", "b"}


def test_registry_entries_fail_closed_on_a_non_string_entry():
    with pytest.raises(hygiene.RuleFailure):
        hygiene.registry_entries("X = {'a': 1, 2: 'b'}\n", "X")
    with pytest.raises(hygiene.RuleFailure):
        hygiene.registry_entries("X = compute()\n", "X")
    with pytest.raises(hygiene.RuleFailure):
        hygiene.registry_entries("Y = {'a': 1}\n", "X")


def test_registry_union_flags_an_entry_dropped_by_the_merge():
    assert hygiene.registry_union_dropped({"a", "c"}, {"a", "b"}, {"a", "b"}) == ["c"]


def test_registry_union_accepts_the_union_and_extra_entries():
    assert hygiene.registry_union_dropped({"a", "c"}, {"a", "b"}, {"a", "b", "c"}) == []
    assert hygiene.registry_union_dropped({"a"}, {"b"}, {"a", "b", "c"}) == []


def test_merge_tree_runs_all_repository_structural_gates():
    assert hygiene.GATE_SCRIPTS == (
        "scripts/ci/check_structural_limits.py",
        "scripts/ci/check_module_boundaries.py",
        "scripts/ci/check_unbounded_waits.py",
        "scripts/ci/check_import_ratchet.py",
    )
    for relative in hygiene.GATE_SCRIPTS:
        assert (ROOT / relative).is_file()
    # The cheap tree-level gates ride rule 2's fast commands.  They are pure
    # stdlib + pytest, run WITHOUT the locked environment, and --noconftest is
    # required (the repo conftest imports hyprial.*); test_boundaries.py is
    # deliberately out because it needs the locked environment.
    assert hygiene.FAST_GATE_COMMAND in hygiene.FAST_GATE_COMMANDS
    assert hygiene.TREE_LEVEL_TEST_COMMAND in hygiene.FAST_GATE_COMMANDS
    assert "--noconftest" in hygiene.TREE_LEVEL_TEST_COMMAND
    assert "test_boundaries.py" not in hygiene.TREE_LEVEL_TEST_COMMAND
    for relative in hygiene.TREE_LEVEL_TESTS:
        assert (ROOT / relative).is_file()


def test_registry_config_targets_the_real_tripwire_inventory():
    config = json.loads((ROOT / "scripts" / "ci" / "registry-union.json").read_text())
    assert config["registries"], "at least the NATIVE_ORIGINS entry must be configured"
    for registry in config["registries"]:
        path = ROOT / registry["path"]
        assert path.is_file(), f"{path} is configured but missing"
        entries = hygiene.registry_entries(
            path.read_text(encoding="utf-8"), registry["symbol"]
        )
        assert entries, f"{registry['symbol']} in {registry['path']} read as empty"
