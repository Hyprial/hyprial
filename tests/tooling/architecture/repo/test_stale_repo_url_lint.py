"""Repo-rename lint (2026-08-13): the shipped surface may mention the old
``harness-bridge-py`` name only in explicitly whitelisted historical
references.  Any other hit fails here, preventing a stale install URL or a
stale fallback source from regressing after the rename.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]

# Each rule is (path regex, line-content regex). A hit is allowed when at
# least one rule matches both the path and the matched line content.
_RULES: tuple[tuple[str, str], ...] = (
    # README: the concise v0.4.0 recovery pointer.
    (r"^README\.md$", r"harness-bridge-py"),
    (r"^docs/i18n/README\.zh-CN\.md$", r"harness-bridge-py"),
    # The linked operator guide preserves the detailed rename recovery note.
    (r"^docs/guides/operator-guide\.md$", r"harness-bridge-py"),
    # design.md §8 decision record: the rename as completed.
    (r"^docs/design/design\.md$", r"harness-bridge-py"),
    # updates.py retargets known pre-rename install sources (rename healing).
    (r"^src/hyprial/daemon/impl/updates/source\.py$", r"harness-bridge-py"),
    # E2E scenarios exercise the pre-rename -> renamed healing path.
    (r"^contract/e2e/", r"harness-bridge-py"),
)


def _grep_hits() -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "grep", "-nw", "harness-bridge-py", "--", "src/", "contract/", "scripts/", "docs/", "README.md",
         # Historical evidence includes source preimages and pytest node IDs
         # for rename-healing tests. Preserve those bytes; they are not live
         # install instructions. Active docs/guides/design and code stay gated.
         ":(exclude)docs/notes/", ":(exclude)docs/evidence/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    hits = []
    for line in result.stdout.splitlines():
        path, _lineno, content = line.split(":", 2)
        hits.append((path, content))
    return hits


def test_remaining_old_repo_name_hits_are_all_whitelisted() -> None:
    hits = _grep_hits()
    assert hits, "sanity: the whitelisted rename references must still exist"

    unmatched = []
    for path, content in hits:
        if not any(
            re.search(path_pattern, path) and re.search(line_pattern, content)
            for path_pattern, line_pattern in _RULES
        ):
            unmatched.append(f"{path}: {content.strip()}")

    assert not unmatched, (
        "non-whitelisted 'harness-bridge-py' hits (README/operator-guide rename note, "
        "or decision record in design.md):\n" + "\n".join(unmatched)
    )


def test_repository_lint_rejects_real_old_urls_not_longer_checkout_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lint guards the retired repository name, not a prefix of 'python'."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("historical harness-bridge-py rename\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "current.json").write_text(
        '"root": "/isolated/harness-bridge-python-review-r3"\n'
    )
    subprocess.run(["git", "add", "README.md", "scripts/current.json"],
                   cwd=tmp_path, check=True)
    monkeypatch.setattr(__import__(__name__, fromlist=["REPO_ROOT"]), "REPO_ROOT", tmp_path)
    test_remaining_old_repo_name_hits_are_all_whitelisted()
    (scripts / "current.json").write_text(
        '"url": "https://example.invalid/org/harness-bridge-py.git"\n'
    )
    with pytest.raises(AssertionError, match="scripts/current.json"):
        test_remaining_old_repo_name_hits_are_all_whitelisted()


@pytest.mark.parametrize("artifact", [
    "docs/evidence/tauri-v2-integration-2026-10-03/runs/collection.log",
    "docs/evidence/test-long-files-2026-10-03/outer/preimages/updates.py.txt",
    "docs/evidence/tailcat-main-integration-2026-10-03/runs/first-fail.log",
])
def test_historical_evidence_is_not_an_active_install_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("historical harness-bridge-py rename\n")
    evidence = tmp_path / artifact
    evidence.parent.mkdir(parents=True)
    evidence.write_text("https://example.invalid/org/harness-bridge-py.git\n")
    subprocess.run(["git", "add", "README.md", artifact], cwd=tmp_path, check=True)
    monkeypatch.setattr(__import__(__name__, fromlist=["REPO_ROOT"]), "REPO_ROOT", tmp_path)
    test_remaining_old_repo_name_hits_are_all_whitelisted()

    guide = tmp_path / "docs/guides/install-current.md"
    guide.parent.mkdir(parents=True)
    guide.write_text(evidence.read_text())
    subprocess.run(["git", "add", "docs/guides/install-current.md"], cwd=tmp_path, check=True)
    with pytest.raises(AssertionError, match="docs/guides/install-current.md"):
        test_remaining_old_repo_name_hits_are_all_whitelisted()
