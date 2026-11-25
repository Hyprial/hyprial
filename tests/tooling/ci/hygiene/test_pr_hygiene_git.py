"""Isolated git repros for rules 1--3 of scripts/ci/check_pr_hygiene.py.

Every repository here is a fresh temporary repository created under pytest's
``tmp_path``; nothing reads the developer's repositories, HOME configuration or
the network. Each test shows the rule's RED state and its GREEN state.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_pr_hygiene.py"
SPEC = importlib.util.spec_from_file_location("check_pr_hygiene_git", SCRIPT)
hygiene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hygiene)

# The rule-2 gates are the REAL files, copied into the temp repo, so the repro
# exercises the repository's structural gates and Node policy tests, not a stub.
GATE_FILES = (
    "scripts/ci/check_structural_limits.py",
    "scripts/ci/check_module_boundaries.py",
    "scripts/ci/check_unbounded_waits.py",
    "scripts/ci/check_import_ratchet.py",
    "scripts/ci/import-ratchet-baseline.json",
    "scripts/ci/ci-scope.mjs",
    "scripts/ci/ci-scope.test.mjs",
    "scripts/ci/ci-plan.mjs",
    "scripts/ci/ci-plan.test.mjs",
    "scripts/ci/git-flow.mjs",
    ".forgejo/git-flow.json",
)
REGISTRY_PATH = "tests/integration/native_harnesses/resident_metadata/native_origins_a.py"
REGISTRY_CONFIG = ROOT / "scripts" / "ci" / "registry-union.json"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [
            "git",
            "-c", "user.email=ci@test.invalid",
            "-c", "user.name=ci-pr-hygiene",
            "-c", "commit.gpgsign=false",
            "-c", "init.defaultBranch=main",
            "-c", "advice.detachedHead=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def seed(tmp_path: Path) -> tuple[Path, Path]:
    """A bare ``origin`` with a pushed ``main``, and a clean ``work`` clone."""

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-q")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    git(work, "remote", "add", "origin", str(origin))
    (work / "README.md").write_text("base\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def advance_main(tmp_path: Path, origin: Path, mutate) -> None:
    """Push one more commit to ``origin/main`` from an independent clone."""

    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    git(other, "remote", "add", "origin", str(origin))
    git(other, "fetch", "-q", "origin", "main")
    git(other, "checkout", "-q", "-b", "main", "FETCH_HEAD")
    mutate(other)
    git(other, "add", "-A")
    git(other, "commit", "-qm", "advance base")
    git(other, "push", "-q", "origin", "main")


def registry_text(entries) -> str:
    body = ", ".join(f'"{entry}": ("resident", ("x",))' for entry in entries)
    return f"NATIVE_ORIGINS_PART = {{{body}}}\n"


def minimal_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo that satisfies the structural gates, with the gate files in it."""

    origin, work = seed(tmp_path)
    for root in (
        "src/hyprial/daemon",
        "src/hyprial/biz",
        "src/hyprial/identity",
        "src/gui",
        "app/src",
        "tests",
    ):
        (work / root).mkdir(parents=True, exist_ok=True)
    (work / "src/hyprial/__init__.py").write_text("", encoding="utf-8")
    (work / "tests/test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    module = work / "src/hyprial/kernel/feature"
    module.mkdir(parents=True)
    for name in ("f1", "f2", "f3", "f4"):
        (module / f"{name}.py").write_text("VALUE = 1\n", encoding="utf-8")
    for relative in GATE_FILES:
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "minimal tree with gates")
    git(work, "push", "-q", "origin", "main")
    return origin, work


def test_git_env_scopes_the_token_to_one_hardened_command(monkeypatch, capsys):
    """The token starts at index zero; inherited git controls are discarded."""

    for key in tuple(os.environ):
        if key.startswith("GIT_") or key == "XDG_CONFIG_HOME":
            monkeypatch.delenv(key)
    monkeypatch.setenv("GITHUB_SERVER_URL", "http://git.internal.hyprial.com:3000/")
    monkeypatch.setenv(hygiene.GIT_TOKEN_ENV, "run-scoped-token")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://attacker.invalid/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "http://git.internal.hyprial.com:3000/")
    monkeypatch.setenv("GIT_DIR", "/attacker/repo")
    monkeypatch.setenv("GIT_ASKPASS", "/attacker/askpass")
    monkeypatch.setenv("GIT_SSH_COMMAND", "attacker-ssh")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/attacker/xdg")

    env = hygiene._git_env()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == (
        "http.http://git.internal.hyprial.com:3000/.extraHeader"
    )
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: token run-scoped-token"
    assert "GIT_DIR" not in env
    assert "GIT_ASKPASS" not in env
    assert "GIT_SSH_COMMAND" not in env
    assert "XDG_CONFIG_HOME" not in env
    warning = capsys.readouterr().err
    assert warning == (
        "warning: ignored inherited git environment: GIT_ASKPASS, "
        "GIT_CONFIG_COUNT, GIT_CONFIG_KEY_0, GIT_CONFIG_VALUE_0, GIT_DIR, "
        "GIT_SSH_COMMAND, XDG_CONFIG_HOME\n"
    )
    for secret in (
        "/attacker/askpass",
        "https://attacker.invalid/",
        "http://git.internal.hyprial.com:3000/",
        "/attacker/repo",
        "attacker-ssh",
        "/attacker/xdg",
    ):
        assert secret not in warning


def test_git_env_with_clean_parent_emits_no_warning(monkeypatch, capsys):
    for key in tuple(os.environ):
        if key.startswith("GIT_") or key == "XDG_CONFIG_HOME":
            monkeypatch.delenv(key)

    hygiene._git_env()

    assert capsys.readouterr().err == ""


def test_git_env_ignores_global_count_and_xdg_attacker_config(
    tmp_path, monkeypatch
):
    """Offline A/B: neither config file nor command-scope injection redirects git."""

    legit = "https://legit.invalid/HyprialOS/harness-bridge.git"
    global_config = tmp_path / "attacker.cfg"
    global_config.write_text(
        "[url \"https://global-attacker.invalid/\"]\n"
        "\tinsteadOf = https://legit.invalid/\n"
        "[http \"https://legit.invalid/\"]\n"
        "\textraHeader = X-Canary: global\n",
        encoding="utf-8",
    )
    xdg = tmp_path / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "config").write_text(
        "[url \"https://xdg-attacker.invalid/\"]\n"
        "\tinsteadOf = https://legit.invalid/\n"
        "[http \"https://legit.invalid/\"]\n"
        "\textraHeader = X-Canary: xdg\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "X-Canary: command")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://legit.invalid")
    monkeypatch.setenv(hygiene.GIT_TOKEN_ENV, "sentinel-token")

    protected = hygiene._git_env()
    resolved = subprocess.run(
        ["git", "ls-remote", "--get-url", legit],
        env=protected,
        capture_output=True,
        text=True,
        check=True,
    )
    assert resolved.stdout.strip() == legit
    headers = subprocess.run(
        ["git", "config", "--get-urlmatch", "http.extraHeader", legit],
        env=protected,
        capture_output=True,
        text=True,
        check=True,
    )
    assert headers.stdout.strip() == "Authorization: token sentinel-token"
    assert "Canary" not in headers.stdout

    monkeypatch.delenv(hygiene.GIT_TOKEN_ENV)
    control = hygiene._git_env()
    assert "GIT_CONFIG_COUNT" not in control
    unchanged = subprocess.run(
        ["git", "ls-remote", "--get-url", legit],
        env=control,
        capture_output=True,
        text=True,
        check=True,
    )
    assert unchanged.stdout.strip() == legit
    absent = subprocess.run(
        ["git", "config", "--get-urlmatch", "http.extraHeader", legit],
        env=control,
        capture_output=True,
        text=True,
        check=False,
    )
    assert absent.returncode == 1
    assert absent.stdout == ""


def test_merge_tree_is_identified_by_value_not_inferred(tmp_path, capsys):
    """Green runs print WHICH refs were measured (the (iii) contract)."""

    origin, work = minimal_repo(tmp_path)
    hygiene.materialise_merge_tree("main", work, tmp_path / "merge-tree")
    out = capsys.readouterr().out
    assert "gate=merge-tree" in out
    assert "base_source=" in out
    assert re.search(r"gate=merge-tree tree=@[0-9a-f]{40} state=merged", out)


CREDENTIAL_ENVIRONMENT = {
    "PR_HYGIENE_GIT_TOKEN": "fake-pr-token",
    "GITHUB_TOKEN": "fake-github-token",
    "FORGEJO_TOKEN": "fake-forgejo-token",
    "SERVICE_TOKEN_FILE": "/fake/token-file",
    "SERVICE_SECRET_HINT": "fake-secret",
    "DATABASE_PASSWORD_FILE": "/fake/password-file",
    "GIT_ASKPASS": "/fake/askpass",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "fake-helper",
}


def _credential_environment_names(environment: dict[str, str]) -> set[str]:
    names = set()
    for name in environment:
        upper = name.upper()
        if (
            any(marker in upper for marker in ("TOKEN", "SECRET", "PASSWORD"))
            or upper == "GIT_ASKPASS"
            or upper.startswith("GIT_CONFIG_")
        ):
            names.add(name)
    return names


@pytest.mark.parametrize("token_set", [False, True], ids=["token-unset", "token-set"])
@pytest.mark.parametrize("tree_code_executes", [False, True], ids=["tree-skipped", "tree-executes"])
def test_merge_tree_code_never_observes_parent_credentials(
    tmp_path, monkeypatch, token_set, tree_code_executes
):
    """Four-cell proof: inspect the child's environment, not the gate verdict."""

    for name in CREDENTIAL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    if token_set:
        for name, value in CREDENTIAL_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)

    evidence = tmp_path / "child-environment.txt"
    command = f"env > {shlex.quote(str(evidence))}"
    commands = (command,) if tree_code_executes else ()
    assert hygiene.merge_tree_gate_errors(
        tmp_path, gates=(), fast_gate_commands=commands
    ) == []

    if not tree_code_executes:
        assert not evidence.exists()
        return

    observed = dict(
        line.split("=", 1)
        for line in evidence.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert _credential_environment_names(observed) == set()
    assert CREDENTIAL_ENVIRONMENT.keys().isdisjoint(observed)


def test_merge_tree_conflict_is_a_clear_red(tmp_path):
    origin, work = seed(tmp_path)
    (work / "conflict.txt").write_text("base\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "conflict file on main")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "feature")
    (work / "conflict.txt").write_text("head\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "head side")
    advance_main(tmp_path, origin, lambda repo: (repo / "conflict.txt").write_text("advanced\n", encoding="utf-8"))

    with pytest.raises(hygiene.RuleFailure) as raised:
        hygiene.materialise_merge_tree("main", work, tmp_path / "merge-tree")
    assert "does not merge cleanly" in str(raised.value)
    # Conflicted, aborted, reported -- never a silent skip.
    assert git(work, "status", "--porcelain").stdout.strip() == ""


def test_merge_tree_only_structural_red_is_green_on_the_head_alone(tmp_path):
    origin, work = minimal_repo(tmp_path)
    module = work / "src/hyprial/kernel/feature"
    git(work, "checkout", "-q", "-b", "feature")
    for name in ("h1", "h2"):
        (module / f"{name}.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "head adds two files")

    # Head alone: six direct files in the module -> the structural gate is green.
    # Only the structural gate runs here: check_module_boundaries.py audits the
    # full five-domain architecture, which a minimal repro cannot satisfy, and
    # the production default (GATE_SCRIPTS) is asserted separately.  The fast
    # commands (Node policy tests + the tree-level pytest gates) are skipped too:
    # this repro is about the structural reader, not about them.
    structural = ("scripts/ci/check_structural_limits.py",)
    assert hygiene.merge_tree_gate_errors(work, structural, fast_gate_commands=()) == []

    def add_base_files(repo: Path) -> None:
        base_module = repo / "src/hyprial/kernel/feature"
        base_module.mkdir(parents=True, exist_ok=True)
        for name in ("b1", "b2"):
            (base_module / f"{name}.py").write_text("VALUE = 3\n", encoding="utf-8")

    advance_main(tmp_path, origin, add_base_files)

    tree = hygiene.materialise_merge_tree("main", work, tmp_path / "merge-tree")
    errors = hygiene.merge_tree_gate_errors(tree, structural, fast_gate_commands=())
    assert errors, "eight direct files only exist in the merge result"
    assert any("check_structural_limits.py" in error for error in errors)


def test_structural_gate_accepts_both_retirement_tree_shapes(tmp_path):
    """The pre-deletion working tree and the post-deletion merge tree both pass.

    ``git`` does not track the empty legacy directories, so the materialised
    merge tree is exactly the post-retirement shape even though the working
    tree still has ``src/gui``/``app/src``.  A gate that only passes one shape
    would either block the cleanup PR or red every later PR.
    """

    origin, work = minimal_repo(tmp_path)
    structural = ("scripts/ci/check_structural_limits.py",)
    assert (work / "src/gui").is_dir()
    assert (work / "app/src").is_dir()
    assert hygiene.merge_tree_gate_errors(work, structural, fast_gate_commands=()) == []

    git(work, "checkout", "-q", "-b", "feature")
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "feature work")
    tree = hygiene.materialise_merge_tree("main", work, tmp_path / "merge-tree")
    assert not (tree / "src/gui").exists()
    assert not (tree / "app/src").exists()
    assert hygiene.merge_tree_gate_errors(tree, structural, fast_gate_commands=()) == []


# --- rule 3: registry union --------------------------------------------------


def test_registry_union_flags_an_entry_the_merge_resolution_drops(tmp_path):
    origin, work = seed(tmp_path)
    path = work / REGISTRY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(registry_text(["a", "c"]), encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "registry with a and c")
    git(work, "push", "-q", "origin", "main")

    git(work, "checkout", "-q", "-b", "feature")
    path.write_text(registry_text(["a", "b"]), encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "head side")

    def base_side(repo: Path) -> None:
        base_path = repo / REGISTRY_PATH
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(registry_text(["a", "c2"]), encoding="utf-8")

    advance_main(tmp_path, origin, base_side)

    git(work, "fetch", "-q", "origin", "main")
    tree = tmp_path / "merge-tree"
    git(work, "worktree", "add", "--detach", "--force", str(tree), "HEAD")
    git(tree, "merge", "--no-commit", "origin/main", check=False)
    merged = tree / REGISTRY_PATH
    assert merged.is_file(), "the registry conflicted, as the repro requires"

    # RED: the resolution keeps only the head side and drops base's entry.
    merged.write_text(registry_text(["a", "b"]), encoding="utf-8")
    errors = hygiene.registry_union_errors("main", work, tree, REGISTRY_CONFIG)
    assert len(errors) == 1
    assert "'c2'" in errors[0] and "superset" in errors[0]

    # GREEN: the resolution restores the union.
    merged.write_text(registry_text(["a", "b", "c", "c2"]), encoding="utf-8")
    assert hygiene.registry_union_errors("main", work, tree, REGISTRY_CONFIG) == []


# --- the gate's OWN identity (2026-10-13 infra red) --------------------------


def _no_identity_env(tmp_path: Path) -> dict[str, str]:
    """An environment shaped like a CI checkout: no user.name/user.email anywhere.

    ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` are emptied and ``HOME`` points at
    an empty directory.  ``user.useConfigOnly=true`` is passed per command where a
    failure is wanted, because git otherwise INVENTS an identity from the GECOS
    field and the hostname -- which is exactly why this defect only appeared in the
    CI container and not on a developer machine.
    """

    home = tmp_path / "no-home"
    home.mkdir(exist_ok=True)
    empty = tmp_path / "no-config"
    empty.write_text("", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        )
    }
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(empty),
            "GIT_CONFIG_SYSTEM": str(empty),
        }
    )
    return env


def test_git_env_supplies_the_fixed_gate_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "attacker")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "attacker@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "attacker")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "attacker@example.invalid")

    env = hygiene._git_env()
    assert env["GIT_AUTHOR_NAME"] == "pr-hygiene"
    assert env["GIT_AUTHOR_EMAIL"] == "pr-hygiene@hyprial.invalid"
    assert env["GIT_COMMITTER_NAME"] == "pr-hygiene"
    assert env["GIT_COMMITTER_EMAIL"] == "pr-hygiene@hyprial.invalid"


def test_the_gates_own_git_operation_carries_its_own_identity(tmp_path, monkeypatch):
    """Red evidence, then the fix, for the 2026-10-13 infrastructure red.

    The condition is real: with no identity anywhere and auto-detection disabled, a
    committing git operation fails.  The gate's own helper then succeeds in the SAME
    environment, because ``_git_env()`` supplies the identity.
    """

    origin, work = minimal_repo(tmp_path)
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "no-home"
    home.mkdir(exist_ok=True)
    empty = tmp_path / "no-config"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))

    control = subprocess.run(
        ["git", "-c", "user.useConfigOnly=true", "commit", "-q", "--allow-empty", "-m", "x"],
        cwd=str(work),
        capture_output=True,
        text=True,
        env=_no_identity_env(tmp_path),
    )
    assert control.returncode != 0, "the identity-free condition must reproduce"

    gate_run = hygiene.git(
        ["-c", "user.useConfigOnly=true", "commit", "-q", "--allow-empty", "-m", "gate"],
        work,
        check=False,
    )
    assert gate_run.returncode == 0, gate_run.stderr


def test_merge_tree_merges_with_the_gate_identity(tmp_path, monkeypatch):
    """The merge the gate performs names the GATE's identity, not the checkout's."""

    origin, work = minimal_repo(tmp_path)
    real_git = hygiene.git
    calls: list[list[str]] = []

    def spy(args, cwd, check=True):
        calls.append(list(args))
        return real_git(args, cwd, check=check)

    monkeypatch.setattr(hygiene, "git", spy)
    hygiene.materialise_merge_tree("main", work, tmp_path / "merge-tree")
    merges = [call for call in calls if "merge" in call and "--no-commit" in call]
    assert merges, calls
    assert all("-c" in call and "user.name=pr-hygiene" in call for call in merges), merges


def test_merge_tree_gate_operation_failure_is_not_reported_as_a_conflict(
    tmp_path, capsys, monkeypatch
):
    """A failure of the GATE's own operation must not read as the author's conflict."""

    origin, work = minimal_repo(tmp_path)
    real_git = hygiene.git

    def flaky(args, cwd, check=True):
        if "merge" in args and "--no-commit" in args:
            return subprocess.CompletedProcess(args, 128, "", "Author identity unknown\n")
        return real_git(args, cwd, check=check)

    monkeypatch.setattr(hygiene, "git", flaky)
    with pytest.raises(hygiene.RuleFailure) as raised:
        hygiene.materialise_merge_tree("main", work, tmp_path / "merge-tree")

    message = str(raised.value)
    assert "GATE's own operation failed" in message
    assert "resolve it and push again" not in message
    assert "state=gate-operation-failed" in capsys.readouterr().out
