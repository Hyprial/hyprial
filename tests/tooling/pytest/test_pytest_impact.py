"""Exercise impact selection against real Git histories, including fail-closed cases."""
import copy
import importlib.util
import json
import re
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def impact():
    spec = importlib.util.spec_from_file_location("pytest_impact_under_test", ROOT / "scripts/ci/pytest_impact.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def commit(root, message="fixture change"):
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def event(base, head):
    return {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Impact fixture")
    for path in (
        "src/hyprial/alpha.py", "src/hyprial/beta.py", "src/hyprial/shared.py",
        "tests/test_alpha.py", "tests/test_alpha_extra.py", "tests/test_beta.py",
        "tests/test_common.py", "README.md", "docs/notes/behavior.md",
    ):
        write(tmp_path, path, "# original fixture\n")
    return tmp_path, commit(tmp_path, "base")


@pytest.fixture
def mapping(repo):
    return {
        "version": 1,
        "auditCommit": repo[1],
        "commonTests": ["tests/test_common.py"],
        "groups": [
            {"name": "alpha", "sourcePaths": ["src/hyprial/alpha.py"],
             "tests": ["tests/test_alpha*.py"], "reason": "Alpha module and direct consumers"},
            {"name": "beta", "sourcePaths": ["src/hyprial/beta.py"],
             "tests": ["tests/test_beta.py", "tests/test_common.py"], "reason": "Beta consumers"},
        ],
    }


def changed(repo, *paths):
    root, base = repo
    for path in paths:
        write(root, path, "# changed fixture\n")
    return root, event(base, commit(root))


def assert_full(result):
    assert result["mode"] == "full", result
    assert result["tests"] == ["tests/"]
    assert result["reasons"], "Fallback must explain why selection was unsafe"


def test_one_module_selects_matching_tests_and_common_tests(impact, repo, mapping):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    result = impact.plan(root, payload, "pull_request", mapping)
    assert result["mode"] == "selected", result
    assert result["tests"] == ["tests/test_alpha.py", "tests/test_alpha_extra.py", "tests/test_common.py"]
    assert result["groups"] == ["alpha"]
    assert result["reasons"]


def test_multiple_modules_form_sorted_deduplicated_union(impact, repo, mapping):
    root, payload = changed(repo, "src/hyprial/alpha.py", "src/hyprial/beta.py")
    result = impact.plan(root, payload, "pull_request", mapping)
    assert result["mode"] == "selected", result
    assert result["tests"] == [
        "tests/test_alpha.py", "tests/test_alpha_extra.py", "tests/test_beta.py", "tests/test_common.py",
    ]
    assert set(result["groups"]) == {"alpha", "beta"}


@pytest.mark.parametrize("name", ["push", "workflow_dispatch", "schedule", "pull_request_target", ""])
def test_only_pull_requests_can_select(impact, repo, mapping, name):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    assert_full(impact.plan(root, payload, name, mapping))


@pytest.mark.parametrize("extra", ["README.md", "docs/notes/behavior.md"])
def test_documentation_can_accompany_a_known_module(impact, repo, mapping, extra):
    root, payload = changed(repo, "src/hyprial/alpha.py", extra)
    result = impact.plan(root, payload, "pull_request", mapping)
    assert result["mode"] == "selected", result
    assert result["groups"] == ["alpha"]


def test_documentation_only_does_not_produce_empty_pytest_command(impact, repo, mapping):
    root, payload = changed(repo, "README.md")
    assert_full(impact.plan(root, payload, "pull_request", mapping))


@pytest.mark.parametrize("path", ["src/hyprial/shared.py", "pyproject.toml", "src/hyprial/unknown.py"])
def test_shared_or_unknown_change_forces_full_even_with_known_module(impact, repo, mapping, path):
    root, payload = changed(repo, "src/hyprial/alpha.py", path)
    assert_full(impact.plan(root, payload, "pull_request", mapping))


@pytest.mark.parametrize("kind", ["delete_source", "rename_source", "add_source", "delete_test", "rename_test", "add_test"])
def test_file_structure_changes_fail_closed(impact, repo, mapping, kind):
    root, base = repo
    write(root, "src/hyprial/alpha.py", "# eligible change\n")
    is_test = kind.endswith("test")
    old = "tests/test_beta.py" if is_test else "src/hyprial/beta.py"
    new = "tests/test_new.py" if is_test else "src/hyprial/new.py"
    if kind.startswith("delete"):
        (root / old).unlink()
    elif kind.startswith("rename"):
        (root / old).rename(root / new)
    else:
        write(root, new, "# new\n")
        if not is_test:
            mapping["groups"][0]["sourcePaths"].append(new)
    assert_full(impact.plan(root, event(base, commit(root)), "pull_request", mapping))


def test_entire_pr_range_is_considered_not_only_latest_commit(impact, repo, mapping):
    root, base = repo
    write(root, "src/hyprial/shared.py", "# earlier dangerous change\n")
    commit(root, "shared change")
    write(root, "src/hyprial/alpha.py", "# latest eligible change\n")
    assert_full(impact.plan(root, event(base, commit(root)), "pull_request", mapping))


def test_base_branch_advancement_uses_merge_base(impact, repo, mapping):
    root, base = repo
    write(root, "src/hyprial/alpha.py", "# feature change\n")
    head = commit(root)
    git(root, "checkout", "--detach", base)
    write(root, "src/hyprial/shared.py", "# unrelated base advancement\n")
    newer_base = commit(root)
    git(root, "checkout", "--detach", head)
    result = impact.plan(root, event(newer_base, head), "pull_request", mapping)
    assert result["mode"] == "selected", result
    assert result["groups"] == ["alpha"]


@pytest.mark.parametrize("payload", [{}, {"pull_request": {}}, {"pull_request": {"base": {"sha": "f" * 40}, "head": {"sha": "e" * 40}}}])
def test_missing_event_or_unavailable_history_fails_closed(impact, repo, mapping, payload):
    assert_full(impact.plan(repo[0], payload, "pull_request", mapping))


def test_checked_out_head_must_match_event(impact, repo, mapping):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    write(root, "README.md", "# newer checkout\n")
    commit(root)
    assert_full(impact.plan(root, payload, "pull_request", mapping))


@pytest.mark.parametrize("damage", ["version", "empty_groups", "missing_glob", "traversal", "absolute", "non_test", "not_python"])
def test_invalid_mapping_cannot_silently_reduce_coverage(impact, repo, mapping, damage):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    broken = copy.deepcopy(mapping)
    if damage == "version":
        broken["version"] = 999
    elif damage == "empty_groups":
        broken["groups"] = []
    else:
        invalid = {
            "missing_glob": "tests/test_missing*.py", "traversal": "tests/../outside.py",
            "absolute": "/tmp/test_absolute.py", "non_test": "src/hyprial/alpha.py",
            "not_python": "tests/readme.txt",
        }[damage]
        broken["groups"][0]["tests"] = [invalid]
    assert_full(impact.plan(root, payload, "pull_request", broken))


def test_symlinked_test_cannot_escape_repository(impact, repo, mapping, tmp_path):
    root, base = repo
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("# must not be selected\n")
    target = root / "tests/test_alpha.py"
    target.unlink()
    target.symlink_to(outside)
    base = commit(root, "fixture symlink")
    write(root, "src/hyprial/alpha.py", "# feature change\n")
    assert_full(impact.plan(root, event(base, commit(root)), "pull_request", mapping))


def test_unavailable_base_history_fails_closed_at_valid_head(impact, repo, mapping):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    payload["pull_request"]["base"]["sha"] = "f" * 40
    assert_full(impact.plan(root, payload, "pull_request", mapping))


def test_source_paths_match_exactly_not_by_prefix(impact, repo, mapping):
    root, base = repo
    write(root, "src/hyprial/alpha.py_extra", "# preexisting unknown source\n")
    base = commit(root)
    write(root, "src/hyprial/alpha.py_extra", "# modified unknown source\n")
    assert_full(impact.plan(root, event(base, commit(root)), "pull_request", mapping))


def test_missing_common_test_is_not_silently_omitted(impact, repo, mapping):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    mapping["commonTests"].append("tests/test_missing_common.py")
    assert_full(impact.plan(root, payload, "pull_request", mapping))


def test_symlinked_directory_cannot_supply_selected_tests(impact, repo, mapping, tmp_path):
    root, base = repo
    outside = tmp_path.parent / f"{tmp_path.name}-outside-tests"
    outside.mkdir()
    (outside / "test_alpha.py").write_text("# external test\n")
    (root / "tests/linked").symlink_to(outside, target_is_directory=True)
    base = commit(root, "fixture linked directory")
    mapping["groups"][0]["tests"] = ["tests/linked/test_alpha.py"]
    write(root, "src/hyprial/alpha.py", "# feature change\n")
    assert_full(impact.plan(root, event(base, commit(root)), "pull_request", mapping))


def test_real_runner_preserves_failures_and_push_runs_unselected_tests(repo, mapping):
    import json
    import os
    import sys

    root, _ = repo
    write(root, "scripts/ci/pytest_impact.py", (ROOT / "scripts/ci/pytest_impact.py").read_text())
    write(root, "scripts/git_environment.py", (ROOT / "scripts/git_environment.py").read_text())
    write(root, "scripts/ci/pytest-impact-map.json", json.dumps(mapping))
    write(root, "tests/test_alpha.py", "def test_selected():\n    assert True\n")
    write(root, "tests/test_unrelated.py", "def test_unrelated():\n    assert False, 'full-suite sentinel'\n")
    base = commit(root, "runner baseline")
    write(root, "src/hyprial/alpha.py", "# eligible modification\n")
    head = commit(root)
    payload = root / "event.json"
    payload.write_text(json.dumps(event(base, head)))
    env = {**os.environ, "GITHUB_EVENT_PATH": str(payload), "GITHUB_EVENT_NAME": "pull_request"}
    env.pop("GITHUB_STEP_SUMMARY", None)
    env.pop("PYTEST_ADDOPTS", None)

    def run():
        return subprocess.run([sys.executable, "scripts/ci/pytest_impact.py", "--", "-q"],
                              cwd=root, env=env, capture_output=True, text=True, timeout=60)

    selected = run()
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert '"mode": "selected"' in selected.stdout
    assert "full-suite sentinel" not in selected.stdout
    # The very same comparison/mapping can never constrain a release push.
    env["GITHUB_EVENT_NAME"] = "push"
    release = run()
    assert release.returncode == 1, release.stdout + release.stderr
    assert '"mode": "full"' in release.stdout
    assert "full-suite sentinel" in release.stdout
    # Failing selected tests must also fail the existing unit gate.
    env["GITHUB_EVENT_NAME"] = "pull_request"
    write(root, "tests/test_alpha.py", "def test_selected():\n    assert False, 'selected failure sentinel'\n")
    failed = run()
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "selected failure sentinel" in failed.stdout



def test_new_consumer_merged_before_pr_invalidates_old_audit(impact, repo, mapping):
    root, _ = repo
    # The new caller is already on the base, so an ordinary PR diff cannot
    # reveal that the old source->test audit is stale.
    write(root, "src/hyprial/new_consumer.py", "from hyprial.alpha import capability\n")
    newer_base = commit(root, "new consumer landed")
    write(root, "src/hyprial/alpha.py", "# current PR changes only eligible source\n")
    result = impact.plan(root, event(newer_base, commit(root)), "pull_request", mapping)
    assert_full(result)
    assert "re-audit" in " ".join(result["reasons"])


def test_missing_audit_revision_cannot_select(impact, repo, mapping):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    mapping.pop("auditCommit")
    assert_full(impact.plan(root, payload, "pull_request", mapping))


@pytest.mark.parametrize("target", ["main", "intg/gui", "intg/gui-next"])
def test_tier_full_gate_cannot_be_downgraded_by_consumer_mapping(impact, repo, mapping, target):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    payload["pull_request"]["base"]["ref"] = target
    result = impact.plan(root, payload, "pull_request", mapping)
    assert_full(result)
    assert "nested consumer selection disabled" in result["reasons"][0]


def test_dev_pr_can_use_current_audited_mapping(impact, repo, mapping):
    root, payload = changed(repo, "src/hyprial/alpha.py")
    payload["pull_request"]["base"]["ref"] = "dev"
    assert impact.plan(root, payload, "pull_request", mapping)["mode"] == "selected"


def test_stages_cover_exact_collection_without_duplicates(impact, tmp_path, monkeypatch):
    import sys

    write(tmp_path, "tests/test_fast.py", "def test_fast(): pass\n")
    write(tmp_path, "tests/test_new.py", "import pytest\n@pytest.mark.parametrize('i',[1,2,3])\ndef test_new(i): pass\n")
    monkeypatch.setattr(impact, "FAST_FILES", ("tests/test_fast.py",))

    def collect(paths):
        result = subprocess.run([sys.executable, "-m", "pytest", *paths, "--collect-only", "-q"],
                                cwd=tmp_path, text=True, capture_output=True, check=True)
        return {line for line in result.stdout.splitlines() if "::" in line}

    complete = collect(["tests/"])
    fast, rest = [collect(paths) for _, paths in impact.stages(tmp_path, ["tests/"])]
    assert len(complete) == 4
    assert fast | rest == complete
    assert not fast & rest
    assert impact.stages(tmp_path, ["tests/test_new.py"]) == [("remaining", ["tests/test_new.py"])]


@pytest.mark.parametrize("first_status,second_status,expected_calls", [(1, 0, 1), (0, 1, 2), (0, 0, 2)])
def test_stage_failure_cannot_be_hidden_by_later_success(impact, tmp_path, monkeypatch, first_status, second_status, expected_calls):
    from types import SimpleNamespace

    write(tmp_path, "tests/test_fast.py", "def test_fast(): pass\n")
    monkeypatch.setattr(impact, "FAST_FILES", ("tests/test_fast.py",))
    calls = []
    statuses = iter([first_status, second_status])

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=next(statuses))

    monkeypatch.setattr(impact.subprocess, "run", run)
    result = impact.execute(tmp_path, ["tests/"], ["-n", "2", "--basetemp=/tmp/ci-stage"], True)
    assert result == (first_status or second_status)
    assert len(calls) == expected_calls
    assert "--basetemp=/tmp/ci-stage-fast" in calls[0]
    if len(calls) == 2:
        assert "--ignore=tests/test_fast.py" in calls[1]
        assert "--basetemp=/tmp/ci-stage-remaining" in calls[1]


def test_execute_runs_tree_tests_without_parent_credentials(impact, tmp_path, monkeypatch):
    evidence = tmp_path / "observed-environment.json"
    test_path = "tests/test_observe_environment.py"
    write(
        tmp_path,
        test_path,
        "import json, os\n"
        "from pathlib import Path\n"
        f"Path({str(evidence)!r}).write_text(json.dumps(dict(os.environ)))\n"
        "def test_clean_tree_control(): pass\n",
    )
    credentials = {
        "PR_HYGIENE_GIT_TOKEN": "fake-pr-token",
        "GITHUB_TOKEN": "fake-github-token",
        "FORGEJO_TOKEN": "fake-forgejo-token",
        "PYTEST_PLUGIN_TOKEN_PATH": "/fake/token",
        "SERVICE_SECRET": "fake-secret",
        "DATABASE_PASSWORD": "fake-password",
        "GIT_ASKPASS": "/fake/askpass",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "fake-helper",
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)

    assert impact.execute(
        tmp_path,
        [test_path],
        ["-q", "--noconftest", "-p", "no:cacheprovider"],
        staged=False,
    ) == 0
    observed = json.loads(evidence.read_text())
    assert credentials.keys().isdisjoint(observed)
    assert all(
        not any(marker in name.upper() for marker in ("TOKEN", "SECRET", "PASSWORD"))
        for name in observed
    )
    assert "GIT_ASKPASS" not in observed
    assert not any(name.startswith("GIT_CONFIG_") for name in observed)


def test_repository_grouped_plan_has_no_retired_fast_test_paths(impact):
    root = Path(__file__).resolve().parents[3]
    for build in (impact.stages, impact.partitions):
        waves = build(root, ["tests/"])
        assert waves[0][0] == "fast"
        assert set(waves[0][1]) == set(impact.FAST_FILES)
        assert all((root / path).is_file() for path in waves[0][1])
    # Semantic test names must retain their inherited diagnostic wave; this
    # provenance does not authorize a consumer subset or an unknown new test.
    for path, original in {
        "tests/shell/cli/agents/codex/test_delivery.py": "tests/shell/cli/agents/test_cli_codex_interactive.py",
    }.items():
        assert (root / path).is_file()
        assert impact.partition_name(path) == impact.partition_name(original)
    assert impact.partition_name("tests/future/new_subject/test_new_case.py") == "remaining"


# --- test-control environment passthrough (regression: CI sandbox/wait controls were dropped) ---

DECLARED_CONTROL_VARS = {
    "HYPRIAL_BIN": "/work/.venv/bin/hyprial",
    "HYPRIAL_CI_NO_USERNS_CODEX_SANDBOX": "1",
    "HYPRIAL_CI_PROFILE": "1",
    "HYPRIAL_REQUIRE_PI_SDK": "1",
    "HYPRIAL_TEST_TMP_ROOT": "/tmp/hyprial-test-tmp",
    "HYPRIAL_TEST_WAIT_SECONDS": "10",
}


def test_control_environment_is_forwarded_and_credentials_stay_out(impact):
    """The six HYPRIAL_* controls declared by tests.yml reach the pytest child env."""
    source = {**DECLARED_CONTROL_VARS, "GITHUB_TOKEN": "ghs_secret", "AWS_SECRET_ACCESS_KEY": "x", "PATH": "/usr/bin"}
    child = impact.pytest_subprocess_environment(source)
    for name, value in DECLARED_CONTROL_VARS.items():
        assert child.get(name) == value
    assert "GITHUB_TOKEN" not in child
    assert "AWS_SECRET_ACCESS_KEY" not in child
    assert child["PATH"] == "/usr/bin"


def test_generic_allowlist_was_not_widened(impact):
    """The fix must not be 'add HYPRIAL_* to the shared allowlist'."""
    from scripts import git_environment

    for name in DECLARED_CONTROL_VARS:
        assert name not in git_environment._UNTRUSTED_CODE_ENVIRONMENT_ALLOWLIST


def test_undeclared_hyprial_variable_is_not_forwarded(impact):
    """Passthrough is an allowlist, not a HYPRIAL_* prefix match."""
    source = {**DECLARED_CONTROL_VARS, "HYPRIAL_NOT_A_CONTROL": "x", "HYPRIAL_SECRET_TOKEN": "s"}
    child = impact.pytest_subprocess_environment(source)
    assert "HYPRIAL_NOT_A_CONTROL" not in child
    assert "HYPRIAL_SECRET_TOKEN" not in child


def test_execute_passes_control_environment_to_pytest_child(impact, tmp_path, monkeypatch):
    """execute() must hand the passthrough to the real subprocess call, not only to a helper."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(impact.subprocess, "run", fake_run)
    monkeypatch.setenv("HYPRIAL_TEST_WAIT_SECONDS", "10")
    monkeypatch.setenv("HYPRIAL_CI_NO_USERNS_CODEX_SANDBOX", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    exit_code = impact.execute(tmp_path, ["tests/test_alpha.py"], [], False)
    assert exit_code == 0
    assert captured["args"][:3] == [impact.sys.executable, "-m", "pytest"]
    assert captured["env"]["HYPRIAL_TEST_WAIT_SECONDS"] == "10"
    assert captured["env"]["HYPRIAL_CI_NO_USERNS_CODEX_SANDBOX"] == "1"
    assert "GITHUB_TOKEN" not in captured["env"]


def _pytest_shard_job_block(workflow_text: str) -> str:
    """Return the workflow job block that invokes pytest_impact.py."""
    lines = workflow_text.splitlines()
    anchor = next(i for i, line in enumerate(lines) if "pytest_impact.py --shard" in line)
    start = next(i for i in range(anchor, -1, -1) if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[i]))
    end = next(
        (i for i in range(anchor + 1, len(lines)) if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _declared_control_names(job_block: str) -> set[str]:
    """HYPRIAL_* names the pytest job actually sets or consumes.

    A name counts when it is assigned (`export X=`, `X:` in env, `echo "X=..." >> $GITHUB_ENV`)
    or dereferenced (`${X...}`). A bare mention inside a comment does not count, so prose cannot
    create drift.
    """
    names: set[str] = set()
    for match in re.finditer(r"(?:export\s+|\$\{)(HYPRIAL_[A-Z0-9_]+)", job_block):
        names.add(match.group(1))
    for match in re.finditer(r"^\s*(HYPRIAL_[A-Z0-9_]+):", job_block, re.MULTILINE):
        names.add(match.group(1))
    for match in re.finditer(r"echo\s+\"?(HYPRIAL_[A-Z0-9_]+)=", job_block):
        names.add(match.group(1))
    return names


def test_control_environment_set_matches_tests_yml(impact):
    """PYTEST_CONTROL_ENVIRONMENT must equal what tests.yml actually declares (no drift)."""
    workflow = (ROOT / ".forgejo/workflows/tests.yml").read_text()
    declared = _declared_control_names(_pytest_shard_job_block(workflow))
    forwarded = set(impact.PYTEST_CONTROL_ENVIRONMENT)
    missing = sorted(declared - forwarded)
    extra = sorted(forwarded - declared)
    assert not missing, f"tests.yml declares these but pytest_impact does not forward them: {missing}"
    assert not extra, f"pytest_impact forwards these but tests.yml does not declare them: {extra}"
