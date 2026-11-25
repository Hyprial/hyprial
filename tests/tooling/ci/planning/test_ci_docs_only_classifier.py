"""Contracts for the conservative docs-only CI classifier."""

from importlib import util
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
CLASSIFIER = ROOT / "scripts" / "ci" / "docs_only.py"


def _classifier():
    assert CLASSIFIER.is_file(), "docs-only classifier script is missing"
    spec = util.spec_from_file_location("ci_docs_only", CLASSIFIER)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "paths",
    [
        ["docs/x.md"],
        ["ROADMAP.md"],
        ["notes/a.md", "docs/b.md"],
    ],
)
def test_documentation_paths_are_docs_only(paths):
    assert _classifier().is_docs_only(paths)


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md"],
        ["CONTRIBUTING.md"],
        ["GLOSSARY.md"],
        [".agents/skills/example/SKILL.md"],
        ["contract/e2e/README.md"],
        ["docs/guides/cutover-runbook.md"],
        ["docs/design/hyprial-login-design.md"],
        ["docs/guides/operator-guide.md"],
        ["docs/domains/squire/SKILL-template.md"],
        ["docs/notes/contract-align-report.md"],
        ["docs/reference/cli-command-inventory.json"],
        ["src/hyprial/example.py"],
        ["tests/test_example.py"],
    ],
)
def test_test_inputs_and_non_documentation_paths_are_not_docs_only(paths):
    assert not _classifier().is_docs_only(paths)


def test_generated_inventory_add_or_delete_is_not_docs_only():
    # The classifier sees the changed path, not the diff mode: this exact path
    # is the same for an add, a JSON edit and a deletion, so all run inventory.
    assert not _classifier().is_docs_only(
        ["docs/reference/cli-command-inventory.json"]
    )


def test_rename_checks_both_old_and_new_paths():
    assert not _classifier().is_docs_only(["docs/old.md", "src/new.md"])


def test_empty_change_list_is_not_docs_only():
    assert not _classifier().is_docs_only([])


def test_required_jobs_stay_present_and_gate_expensive_suites_by_step():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    inventory = jobs["cli-help-inventory"]
    unit = jobs["unit"]
    checks = jobs["checks"]
    shard = jobs["pytest-shard"]

    assert "if" not in inventory
    assert unit["if"] == "always() && needs.cli-help-inventory.outputs.wip != 'true'"
    # `always()` leads, so this job reports rather than vanishing.  Since the
    # 2026-11-06 split pr-hygiene is its own workflow, so nothing else gates it.
    assert checks["if"] == "always() && needs.cli-help-inventory.outputs.wip != 'true'"
    assert inventory["outputs"]["docs_only"] == "${{ steps.docs_only.outputs.docs_only }}"

    inventory_steps = {step["name"]: step for step in inventory["steps"]}
    assert inventory_steps["Classify docs-only pull request"]["id"] == "docs_only"
    assert (
        inventory_steps["Compare Typer tree with generated help inventory"]["if"]
        == "steps.docs_only.outputs.docs_only != 'true' && "
        "steps.scope.outputs.python == 'true'"
    )

    # The expensive suite is gated at the shard JOB: a docs-only PR never
    # starts it, and the aggregator requires exactly `skipped` in that case.
    assert "needs.cli-help-inventory.outputs.docs_only != 'true'" in shard["if"]
    assert "needs.cli-help-inventory.outputs.python == 'true'" in shard["if"]
    assert "Run unit tests shard (excludes E2E)" in {step["name"] for step in shard["steps"]}

    check_steps = {step["name"]: step for step in checks["steps"]}
    text_gates = check_steps["Run documentation text gates"]
    assert text_gates["if"] == "steps.scope.outputs.docs_only == 'true'"
    for test_path in (
        'tests/tooling/architecture/lint/test_provider_term_lint.py',
        'tests/tooling/architecture/merge/test_hyprial_merge_residuals.py',
        'tests/tooling/architecture/repo/test_stale_repo_url_lint.py',
        'tests/tooling/ci/planning/test_design_doc_tag_policy.py::test_design_doc_tag_corrected',
        'tests/tooling/release/publish/github/test_snapshot.py::test_intranet_gate_on_a_real_repo_snapshot_reports_only_pending_hits',
    ):
        assert test_path in text_gates["run"]
