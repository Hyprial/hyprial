"""Exercise layered selection against real Git histories, including fail-closed cases."""
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


LAYER_FILES = (
    "src/hyprial/kernel/core.py", "src/hyprial/identity/ids.py", "src/hyprial/daemon/run.py",
    "src/hyprial/biz/flow.py", "src/hyprial/shell/cli.py", "src/hyprial/cli.py",
    "tests/kernel/test_k.py", "tests/identity/test_i.py", "tests/daemon/test_d.py",
    "tests/biz/test_b.py", "tests/shell/test_s.py", "tests/integration/test_e2e.py",
    "tests/tooling/test_t.py", "tests/scripts/test_sc.py", "tests/support/helper.py",
    "tests/conftest.py", "scripts/ci/gate.py", "scripts/tool.py",
    ".forgejo/workflows/gate-python.yml", "pyproject.toml", "README.md", "docs/guides/g.md",
    "gui/src/app.ts",
)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Impact fixture")
    for path in LAYER_FILES:
        write(tmp_path, path, "# original fixture\n")
    return tmp_path, commit(tmp_path, "base")


def changed(repo, *paths, base_ref="dev"):
    root, base = repo
    for path in paths:
        write(root, path, "# changed fixture\n")
    payload = event(base, commit(root))
    payload["pull_request"]["base"]["ref"] = base_ref
    return root, payload


def assert_full(result):
    assert result["mode"] == "full", result
    assert result["tests"] == ["tests/"]
    assert result["reasons"], "Fallback must explain why selection was unsafe"


def dirs(result):
    assert result["mode"] == "selected", result
    return sorted({"/".join(path.split("/")[:2]) for path in result["tests"]})


@pytest.mark.parametrize("source,expected", [
    ("src/hyprial/shell/cli.py", ["tests/integration", "tests/shell"]),
    ("src/hyprial/biz/flow.py", ["tests/biz", "tests/integration", "tests/shell"]),
    ("src/hyprial/daemon/run.py", ["tests/biz", "tests/daemon", "tests/integration", "tests/shell"]),
    ("src/hyprial/identity/ids.py",
     ["tests/biz", "tests/daemon", "tests/identity", "tests/integration", "tests/shell"]),
    ("src/hyprial/kernel/core.py",
     ["tests/biz", "tests/daemon", "tests/identity", "tests/integration", "tests/kernel", "tests/shell"]),
])
def test_a_layer_change_runs_that_layer_every_layer_above_and_integration(impact, repo, source, expected):
    root, payload = changed(repo, source)
    assert dirs(impact.plan(root, payload, "pull_request")) == expected


@pytest.mark.parametrize("path,expected", [
    ("tests/daemon/test_d.py", ["tests/daemon"]),
    ("tests/integration/test_e2e.py", ["tests/integration"]),
    (".forgejo/workflows/gate-python.yml", ["tests/tooling"]),
    ("scripts/ci/gate.py", ["tests/tooling"]),
    ("scripts/tool.py", ["tests/scripts", "tests/tooling"]),
])
def test_test_and_tooling_changes_run_only_their_own_tests(impact, repo, path, expected):
    root, payload = changed(repo, path)
    assert dirs(impact.plan(root, payload, "pull_request")) == expected


def test_selections_union_across_changed_paths(impact, repo):
    root, payload = changed(repo, "src/hyprial/shell/cli.py", ".forgejo/workflows/gate-python.yml")
    assert dirs(impact.plan(root, payload, "pull_request")) == ["tests/integration", "tests/shell", "tests/tooling"]


@pytest.mark.parametrize("path", [
    "pyproject.toml", "tests/conftest.py", "tests/support/helper.py", "src/hyprial/cli.py",
    "gui/src/app.ts", "src/hyprial/unknown_layer/x.py", "scripts/ci/pytest_impact.py",
])
def test_shared_or_unlayered_change_forces_full(impact, repo, path):
    root, payload = changed(repo, "src/hyprial/shell/cli.py", path)
    assert_full(impact.plan(root, payload, "pull_request"))


def test_added_and_deleted_layer_files_stay_layered(impact, repo):
    root, base = repo
    write(root, "src/hyprial/shell/new_command.py", "# added\n")
    (root / "tests/shell/test_s.py").unlink()
    payload = event(base, commit(root))
    payload["pull_request"]["base"]["ref"] = "dev"
    assert dirs(impact.plan(root, payload, "pull_request")) == ["tests/integration"]


@pytest.mark.parametrize("name", ["push", "workflow_dispatch", "schedule", "pull_request_target", ""])
def test_only_pull_requests_can_select(impact, repo, name):
    root, payload = changed(repo, "src/hyprial/shell/cli.py")
    assert_full(impact.plan(root, payload, name))


@pytest.mark.parametrize("base_ref", ["main", "release/v0.5.5.4", "release/v0.5.5.3.4"])
def test_release_gates_always_run_the_full_suite(impact, repo, base_ref):
    root, payload = changed(repo, "src/hyprial/shell/cli.py", base_ref=base_ref)
    assert_full(impact.plan(root, payload, "pull_request"))


@pytest.mark.parametrize("base_ref", ["dev", "intg/gui-live-0554", "refactor/reconstruct-modules"])
def test_integration_lanes_select_by_layer(impact, repo, base_ref):
    root, payload = changed(repo, "src/hyprial/shell/cli.py", base_ref=base_ref)
    assert impact.plan(root, payload, "pull_request")["mode"] == "selected"


@pytest.mark.parametrize("extra", ["README.md", "docs/guides/g.md"])
def test_documentation_can_accompany_a_layer_change(impact, repo, extra):
    root, payload = changed(repo, "src/hyprial/biz/flow.py", extra)
    assert dirs(impact.plan(root, payload, "pull_request")) == ["tests/biz", "tests/integration", "tests/shell"]


def test_documentation_only_does_not_produce_empty_pytest_command(impact, repo):
    root, payload = changed(repo, "README.md")
    assert_full(impact.plan(root, payload, "pull_request"))


def test_entire_pr_range_is_considered_not_only_latest_commit(impact, repo):
    root, base = repo
    write(root, "src/hyprial/daemon/run.py", "# first commit\n")
    commit(root)
    write(root, "tests/tooling/test_t.py", "# second commit\n")
    payload = event(base, commit(root))
    payload["pull_request"]["base"]["ref"] = "dev"
    assert "tests/daemon" in dirs(impact.plan(root, payload, "pull_request"))


@pytest.mark.parametrize("payload", [{}, {"pull_request": {}}, {"pull_request": {"base": {"sha": "x"}, "head": {"sha": "y"}}}])
def test_missing_event_or_unavailable_history_fails_closed(impact, repo, payload):
    root, _ = repo
    assert_full(impact.plan(root, payload, "pull_request"))


def test_checked_out_head_must_match_event(impact, repo):
    root, payload = changed(repo, "src/hyprial/shell/cli.py")
    write(root, "src/hyprial/shell/cli.py", "# later local commit\n")
    commit(root)
    assert_full(impact.plan(root, payload, "pull_request"))


def test_real_runner_preserves_failures_and_push_runs_unselected_tests(repo):
    import json
    import os
    import sys

    root, _ = repo
    write(root, "scripts/ci/pytest_impact.py", (ROOT / "scripts/ci/pytest_impact.py").read_text())
    write(root, "scripts/git_environment.py", (ROOT / "scripts/git_environment.py").read_text())
    write(root, "tests/shell/test_s.py", "def test_selected():\n    assert True\n")
    write(root, "tests/kernel/test_k.py", "def test_unrelated():\n    assert False, 'full-suite sentinel'\n")
    for path in LAYER_FILES:
        if path.startswith("tests/") and path.endswith(".py") and path not in (
                "tests/shell/test_s.py", "tests/kernel/test_k.py"):
            write(root, path, "def test_ok():\n    assert True\n")
    base = commit(root, "runner baseline")
    write(root, "src/hyprial/shell/cli.py", "# eligible modification\n")
    head = commit(root)
    payload = root / "event.json"
    data = event(base, head)
    data["pull_request"]["base"]["ref"] = "dev"
    payload.write_text(json.dumps(data))
    env = {**os.environ, "GITHUB_EVENT_PATH": str(payload), "GITHUB_EVENT_NAME": "pull_request"}
    env.pop("GITHUB_STEP_SUMMARY", None)
    env.pop("PYTEST_ADDOPTS", None)

    def run():
        return subprocess.run([sys.executable, "scripts/ci/pytest_impact.py", "--", "-q", "-p", "no:cacheprovider"],
                              cwd=root, env=env, capture_output=True, text=True, timeout=120)

    selected = run()
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert '"mode": "selected"' in selected.stdout
    assert "full-suite sentinel" not in selected.stdout
    # The very same comparison can never constrain a push: it runs everything.
    env["GITHUB_EVENT_NAME"] = "push"
    release = run()
    assert release.returncode == 1, release.stdout + release.stderr
    assert '"mode": "full"' in release.stdout
    assert "full-suite sentinel" in release.stdout
    # Failing selected tests must also fail the unit gate.
    env["GITHUB_EVENT_NAME"] = "pull_request"
    write(root, "tests/shell/test_s.py", "def test_selected():\n    assert False, 'selected failure sentinel'\n")
    failed = run()
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "selected failure sentinel" in failed.stdout


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
    """The six HYPRIAL_* controls declared by gate-python.yml reach the pytest child env."""
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
    """PYTEST_CONTROL_ENVIRONMENT must equal what gate-python.yml actually declares (no drift)."""
    workflow = (ROOT / ".forgejo/workflows/gate-python.yml").read_text()
    declared = _declared_control_names(_pytest_shard_job_block(workflow))
    forwarded = set(impact.PYTEST_CONTROL_ENVIRONMENT)
    missing = sorted(declared - forwarded)
    extra = sorted(forwarded - declared)
    assert not missing, f"gate-python.yml declares these but pytest_impact does not forward them: {missing}"
    assert not extra, f"pytest_impact forwards these but gate-python.yml does not declare them: {extra}"
