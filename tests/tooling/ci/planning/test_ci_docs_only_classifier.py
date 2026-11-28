"""Contracts for the conservative docs-only CI classifier."""

from importlib import util
from pathlib import Path

import pytest

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
