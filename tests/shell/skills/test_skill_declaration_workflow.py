"""The Skill-Impact workflow must implement the contract it documents."""

import os
import subprocess
from pathlib import Path
import re

import pytest
import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[3] / '.forgejo/workflows/skill-declaration.yml'
)


def _workflow_gate_script() -> str:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = document["jobs"]["declaration"]["steps"]
    return next(
        step["run"]
        for step in steps
        if step["name"] == "Require a Skill-Impact declaration"
    )


def _git(repo: Path, *args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit_file(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _stale_pull_request_repo(
    tmp_path: Path, *, unrelated_head: bool = False
) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "Workflow Test")
    _git(source, "config", "user.email", "workflow@example.invalid")
    common_sha = _commit_file(source, "README.md", "common\n", "common")

    _git(source, "checkout", "-qb", "moved-base")
    base_sha = _commit_file(
        source,
        "src/hyprial/skills/example/SKILL.md",
        "base-only skill edit\n",
        "move base with skill edit",
    )

    if unrelated_head:
        _git(source, "checkout", "--orphan", "stale-head")
    else:
        _git(source, "checkout", "-qb", "stale-head", common_sha)
    head_sha = _commit_file(
        source,
        "src/hyprial/cli.py",
        "head-only ordinary edit\n",
        "change ordinary file on head",
    )

    remote = tmp_path / "server" / "HyprialOS" / "harness-bridge.git"
    remote.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    return remote, base_sha, head_sha


def _run_gate(
    tmp_path: Path,
    declaration: str,
    *,
    unrelated_head: bool = False,
) -> subprocess.CompletedProcess[str]:
    remote, base_sha, head_sha = _stale_pull_request_repo(
        tmp_path, unrelated_head=unrelated_head
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = {
        **os.environ,
        "PR_BODY": declaration,
        "BASE_SHA": base_sha,
        "HEAD_SHA": head_sha,
        "GITHUB_SERVER_URL": remote.parents[1].as_uri(),
        "GITHUB_TOKEN": "test-only-token",
        "EVENT_ACTION": "synchronize",
    }
    return subprocess.run(
        ["bash", "-c", _workflow_gate_script()],
        cwd=run_dir,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _declared_value_sed_program() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    value_line = next(
        line for line in text.splitlines() if 'value="$(printf' in line
    )
    match = re.search(r"sed -E '([^']+)'", value_line)
    assert match is not None
    return match.group(1)


def test_skill_impact_gate_covers_every_pull_request_base() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    trigger = text.split("jobs:", 1)[0]
    assert "pull_request:" in trigger
    assert "branches:" not in trigger


def test_skill_impact_values_match_the_documented_grammar() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "updated)" in text
    assert "updated*)" not in text
    assert "not-needed:*)" in text
    assert "s/[[:space:]]+$//" in text


@pytest.mark.parametrize(
    "line",
    (
        "Skill-Impact: updated",
        "Skill-Impact: updated ",
        "Skill-Impact: updated\r",
    ),
)
def test_updated_declaration_accepts_web_and_trailing_whitespace(line: str) -> None:
    completed = subprocess.run(
        [
            "sed",
            "-E",
            _declared_value_sed_program(),
        ],
        input=line,
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout == "updated"


def test_skill_gate_never_embeds_the_run_token_in_a_remote_url() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "x-access-token" not in text
    assert "GIT_CONFIG_VALUE_0" in text


def test_stale_not_needed_ignores_skill_edit_added_only_to_base(
    tmp_path: Path,
) -> None:
    result = _run_gate(tmp_path, "Skill-Impact: not-needed: workflow-only change")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "declared not-needed: workflow-only change" in result.stdout


def test_stale_updated_cannot_borrow_skill_edit_added_only_to_base(
    tmp_path: Path,
) -> None:
    result = _run_gate(tmp_path, "Skill-Impact: updated")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "diff 中没有 skill 文件改动" in result.stdout


def test_unreachable_merge_base_fails_loudly(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        "Skill-Impact: not-needed: workflow-only change",
        unrelated_head=True,
    )

    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "unable to determine merge-base" in output


# ---------------------------------------------------------------------------
# ⭐ Which paths the gate can see.
#
# The `updated` branch is only as good as this pattern. Until now it was
# `(^|/)(SKILL\.md$|skills/)`, which does not match `SKILL-template.md` -- the
# source of every squire SKILL.md that gets rendered. So the edit with the
# widest reach over the capability surface was the one edit the gate could not
# see, and #324, which really did update that surface, was left with
# `not-needed` as the only declaration the gate would accept. It was untrue.
#
# ⚠️ Nothing failed while that hole existed. A gate that cannot see a file does
# not complain about it -- it quietly agrees with whatever you declared.
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[3]


def _skill_path_pattern() -> str:
    """The pattern the workflow really greps with -- read out, not restated.

    ⚠️ Anchored on the `git diff --name-only` line rather than on the pattern's
    own shape. An earlier version searched the whole file for a `grep -E`
    starting with `(^|/)`, which had two failure modes worth avoiding: it would
    stop finding the pattern the moment someone rewrote it in another shape
    (red for the wrong reason), and the file contains a second `grep -E` -- the
    one matching the declaration line -- that a looser search could pick up
    instead, silently testing something else entirely.
    """

    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if "git diff --name-only" in line)
    for line in lines[index : index + 3]:
        match = re.search(r"grep -E '([^']+)'", line)
        if match is not None:
            return match.group(1)
    raise AssertionError(
        "no grep -E pattern within 3 lines of the diff command; the gate's "
        "path filter has moved and this test is no longer reading it"
    )


def _gate_sees(paths: list[str]) -> set[str]:
    completed = subprocess.run(
        ["grep", "-E", _skill_path_pattern()],
        input="\n".join(paths) + "\n",
        text=True,
        capture_output=True,
    )
    return {line for line in completed.stdout.splitlines() if line}


def _is_capability_surface(path: str) -> bool:
    """The same idea said a second time, in Python instead of shell.

    Deliberately not a transcription of the regex. The value of having two is
    that they must agree over every tracked file, so a path only one of them
    recognises surfaces as a named disagreement rather than as silence.
    """

    parts = path.split("/")
    name = parts[-1]
    return (name.startswith("SKILL") and name.endswith(".md")) or "skills" in parts[:-1]


def test_the_gate_sees_the_template_that_generates_every_squire_skill() -> None:
    templates = ["src/hyprial/squire/SKILL-template.md", "docs/domains/squire/SKILL-template.md"]
    assert _gate_sees(templates) == set(templates), (
        "the gate cannot see SKILL-template.md, so a PR changing the source of "
        "every generated skill cannot honestly declare 'updated'"
    )


def test_the_gate_still_sees_what_it_always_saw() -> None:
    """Widening must not be paid for with the original coverage."""

    originals = [
        ".agents/skills/hyprial-workflow/SKILL.md",
        "src/hyprial/skills/hyprial-ops/SKILL.md",
        "contract/e2e/fixtures/resident-skill/SKILL.md",
        ".agents/skills/forgejo-ops/references/api.md",
    ]
    assert _gate_sees(originals) == set(originals)


def test_the_gate_does_not_see_ordinary_files() -> None:
    """⭐ The control group: without it, `.` would pass every test above.

    A pattern matching everything makes `updated` declarable on any PR at all
    -- the same as deleting the check, while looking like a very well covered
    gate.
    """

    ordinary = [
        "src/hyprial/cli.py",
        'tests/shell/upgrades/test_upgrade_selfcheck.py',
        "docs/reference/p4-squire.md",
        "notes/e2e-review-pr324.md",
        # The gate's own machinery is not capability-surface documentation.
        ".forgejo/workflows/skill-declaration.yml",
        'tests/shell/plugins/test_plugin_load.py',
    ]
    assert _gate_sees(ordinary) == set()


def test_the_shell_pattern_and_the_stated_rule_agree_on_the_whole_repo() -> None:
    """Two implementations, every tracked file, no disagreement permitted.

    This is what makes the rule outlive today's paths. A future `SKILL-v2.md`
    or a new `skills/` tree is covered without anyone remembering to come back
    here; and if the two ever diverge, the offending file is named rather than
    silently dropped by whichever one missed it.
    """

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, text=True, capture_output=True, check=True
    ).stdout.splitlines()
    assert tracked, "no tracked files; the cross-check would be vacuous"

    by_regex = _gate_sees(tracked)
    by_rule = {path for path in tracked if _is_capability_surface(path)}
    assert by_regex == by_rule, (
        "the workflow's pattern and the stated rule disagree on: "
        f"{sorted(by_regex ^ by_rule)}"
    )
    assert by_rule, "the rule matched nothing; it cannot be checking anything"
