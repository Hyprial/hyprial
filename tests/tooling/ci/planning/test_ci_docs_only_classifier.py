"""Contracts for the conservative documentation-only classifier."""

from importlib import util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
CLASSIFIER = ROOT / "scripts" / "ci" / "docs_only.py"


def _classifier():
    assert CLASSIFIER.is_file()
    spec = util.spec_from_file_location("ci_docs_only", CLASSIFIER)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_root(tmp_path: Path) -> Path:
    for directory in ("tests", "scripts", "contract", "src", ".github"):
        (tmp_path / directory).mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    (tmp_path / "GLOSSARY.md").write_text("# Terms\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("path", ["docs/x.md", "docs/guides/nested/page.md"])
def test_only_docs_markdown_paths_are_path_only_documentation(path):
    classifier = _classifier()
    assert classifier.is_docs_only([path])


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/x.txt",
        "docs",
        "docs/../README.md",
        "/docs/x.md",
        "docs\\x.md",
        "tests/test_example.py",
    ],
)
def test_root_markdown_and_unsafe_paths_are_not_documentation(path):
    assert not _classifier().is_docs_only([path])


def test_empty_change_list_is_not_documentation():
    assert not _classifier().is_docs_only([])


def test_runtime_references_make_a_changed_documentation_file_nonselectable(tmp_path):
    root = _runtime_root(tmp_path)
    (root / "tests" / "test_reference.py").write_text(
        "DOC = 'docs/guides/used.md'\n", encoding="utf-8"
    )
    result = _classifier().classify_paths(["docs/guides/used.md"], root=root)
    assert not result.docs_only
    assert "runtime reference" in result.reason


def test_mjs_and_glossary_references_are_in_the_runtime_corpus(tmp_path):
    root = _runtime_root(tmp_path)
    (root / "scripts" / "check.mjs").write_text(
        "const path = 'docs/guides/used.md';\n", encoding="utf-8"
    )
    (root / "GLOSSARY.md").write_text(
        "The canonical page is docs/guides/glossary.md.\n", encoding="utf-8"
    )
    classifier = _classifier()
    assert not classifier.classify_paths(["docs/guides/used.md"], root=root).docs_only
    assert not classifier.classify_paths(["docs/guides/glossary.md"], root=root).docs_only


def test_python_comments_are_removed_but_python_strings_remain(tmp_path):
    root = _runtime_root(tmp_path)
    (root / "src" / "module.py").write_text(
        "# docs/guides/comment-only.md\nVALUE = 'docs/guides/source-string.md'\n",
        encoding="utf-8",
    )
    classifier = _classifier()
    assert classifier.classify_paths(["docs/guides/comment-only.md"], root=root).docs_only
    assert not classifier.classify_paths(["docs/guides/source-string.md"], root=root).docs_only


def test_symlinked_changed_documentation_is_rejected(tmp_path):
    root = _runtime_root(tmp_path)
    target = root / "outside.md"
    target.write_text("outside\n", encoding="utf-8")
    link = root / "docs" / "linked.md"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(target)
    result = _classifier().classify_paths(["docs/linked.md"], root=root)
    assert not result.docs_only
    assert "outside docs/**/*.md" in result.reason


def test_dangling_symlinked_changed_documentation_is_rejected(tmp_path):
    root = _runtime_root(tmp_path)
    link = root / "docs" / "dangling.md"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(root / "missing.md")
    result = _classifier().classify_paths(["docs/dangling.md"], root=root)
    assert not result.docs_only
    assert "outside docs/**/*.md" in result.reason


def test_dangling_parent_symlinked_documentation_is_rejected(tmp_path):
    root = _runtime_root(tmp_path)
    (root / "docs").mkdir()
    parent = root / "docs" / "missing"
    parent.symlink_to(root / "does-not-exist", target_is_directory=True)
    result = _classifier().classify_paths(["docs/missing/page.md"], root=root)
    assert not result.docs_only
    assert "outside docs/**/*.md" in result.reason


def test_cli_path_arguments_use_the_repository_runtime_corpus(capsys):
    classifier = _classifier()
    assert classifier.main(["docs/guides/release-rules.md"]) == 0
    output = capsys.readouterr().out
    assert "docs_only=false" in output
    assert "runtime reference" in output
