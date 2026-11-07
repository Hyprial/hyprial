from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
RUNNER_PATH = ROOT / "contract" / "e2e" / "run-upgrade-continuity.py"
SCENARIO_INVENTORY = ROOT / "contract" / "e2e" / "scenarios.json"
def load_runner():  # type: ignore[no-untyped-def]
    scenario_dir = str(RUNNER_PATH.parent)
    if scenario_dir not in sys.path:
        sys.path.insert(0, scenario_dir)
    spec = importlib.util.spec_from_file_location(
        "upgrade_continuity_e2e_runner", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def test_git_config_rewrites_every_upgrade_source(tmp_path: Path) -> None:
    # Property test: after the upgrade the NEW code resolves the OFFICIAL
    # GitHub URL (updates.installation_git_url since 6c832a2a), so the
    # scenario's private git config must redirect every known upgrade
    # source to the seed -- never the public network.
    import hyprial.daemon.impl.updates as updates

    runner = load_runner()
    config = tmp_path / "gitconfig"
    runner.git_config(config, tmp_path / "seed")
    text = config.read_text(encoding="utf-8")

    assert updates.UPGRADE_SOURCES, "UPGRADE_SOURCES must not be empty"
    for url in updates.UPGRADE_SOURCES.values():
        assert f"\n\tinsteadOf = {url}\n" in f"\n{text}", (
            f"git_config() does not rewrite upgrade source {url}"
        )


def test_materialize_revision_replaces_the_entire_source_tree(tmp_path: Path) -> None:
    runner = load_runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "E2E test")
    git(repo, "config", "user.email", "e2e@example.invalid")
    (repo / "shared.txt").write_text("old\n", encoding="utf-8")
    (repo / "old-only.txt").write_text("remove me\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "old")
    old_revision = git(repo, "rev-parse", "HEAD")

    (repo / "shared.txt").write_text("candidate\n", encoding="utf-8")
    (repo / "old-only.txt").unlink()
    (repo / "candidate-only.txt").write_text("add me\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "candidate")
    candidate_revision = git(repo, "rev-parse", "HEAD")

    runner.materialize_revision(repo, old_revision, dict(os.environ))
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "old\n"
    assert (repo / "old-only.txt").is_file()
    assert not (repo / "candidate-only.txt").exists()

    runner.materialize_revision(repo, candidate_revision, dict(os.environ))
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "candidate\n"
    assert not (repo / "old-only.txt").exists()
    assert (repo / "candidate-only.txt").is_file()


def test_channel_actor_uses_the_old_daemon_local_identity() -> None:
    runner = load_runner()
    assert runner.local_channel_actor(
        {"owner": "alice", "nodeId": "node-1"}
    ) == "agent:alice:node-1:channel-probe"
    for invalid in (
        {"owner": "alice"},
        {"owner": "foreign:owner", "nodeId": "node-1"},
        {"owner": "alice", "nodeId": "foreign:node"},
    ):
        try:
            runner.local_channel_actor(invalid)
        except AssertionError:
            pass
        else:
            raise AssertionError(f"invalid daemon identity was accepted: {invalid}")


def _repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A work repo whose `origin` is a bare repo, like a CI checkout."""

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "dev", str(work)], check=True)
    git(work, "config", "user.name", "t")
    git(work, "config", "user.email", "t@example.invalid")
    git(work, "remote", "add", "origin", str(origin))
    return origin, work


def _commit_channel_contract(work: Path, protocol: int, message: str) -> str:
    contract = work / "src" / "hyprial" / "contracts" / "channel.py"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        f"# {message}\nCHANNEL_PROTOCOL_VERSION = {protocol}\n", encoding="utf-8"
    )
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    return git(work, "rev-parse", "HEAD")


def test_upgrade_from_reads_the_promoted_tag_from_the_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = load_runner()
    _origin, work = _repo_with_origin(tmp_path)
    promoted = _commit_channel_contract(work, 2, "promoted")
    git(work, "tag", "v0.5.1", promoted)
    git(work, "push", "-q", "origin", "dev", "refs/tags/v0.5.1")
    _commit_channel_contract(work, 2, "candidate")
    # The remote's tag is the authority: a stale local tag must not win.
    git(work, "tag", "-f", "v0.5.1", "HEAD")
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", "--no-local", str(work), str(seed)], check=True)
    monkeypatch.setattr(runner, "ROOT", work)
    monkeypatch.delenv(runner.UPGRADE_FROM_ENV, raising=False)

    source, commit = runner.resolve_upgrade_from(seed, dict(os.environ))

    assert (source, commit) == ("v0.5.1", promoted)


def test_no_upgrade_delta_when_the_promoted_tree_is_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = load_runner()
    _origin, work = _repo_with_origin(tmp_path)
    candidate = _commit_channel_contract(work, 2, "only")
    git(work, "tag", "v0.5.1", candidate)
    git(work, "push", "-q", "origin", "dev", "refs/tags/v0.5.1")
    monkeypatch.setattr(runner, "ROOT", work)
    monkeypatch.delenv(runner.UPGRADE_FROM_ENV, raising=False)
    root = tmp_path / "scenario"
    root.mkdir()

    with pytest.raises(runner.NoUpgradeDelta, match="no upgrade to exercise"):
        runner.seed_remote(root, dict(os.environ), dict(os.environ))


def test_old_channel_is_judged_by_the_old_trees_protocol(tmp_path: Path) -> None:
    runner = load_runner()
    _origin, work = _repo_with_origin(tmp_path)
    old = _commit_channel_contract(work, 2, "old")
    new = _commit_channel_contract(work, 3, "new")
    env = dict(os.environ)

    assert runner.channel_protocol_version_at(work, old, env) == 2
    assert runner.channel_protocol_version_at(work, new, env) == 3


def test_extracted_kernel_channel_protocol_is_read(tmp_path: Path) -> None:
    runner = load_runner()
    _origin, work = _repo_with_origin(tmp_path)
    _commit_channel_contract(work, 2, "legacy")
    destination = work / "src/hyprial/kernel/impl/contracts/channels/channel.py"
    destination.parent.mkdir(parents=True)
    (work / "src/hyprial/contracts/channel.py").rename(destination)
    destination.write_text("CHANNEL_PROTOCOL_VERSION = 4\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "extracted")
    assert runner.channel_protocol_version_at(work, "HEAD", dict(os.environ)) == 4


def test_desired_state_missing_dependency_does_not_fall_back_to_legacy() -> None:
    import builtins

    runner = load_runner()
    original_import = builtins.__import__

    def missing_dependency(name, *args, **kwargs):
        if name == "hyprial.daemon.impl.desired_state.sqlite":
            raise ModuleNotFoundError("missing nested dependency", name="nested_dependency")
        return original_import(name, *args, **kwargs)

    with pytest.raises(ModuleNotFoundError) as caught:
        exec(runner._READ_DESIRED_STATE, {"__builtins__": {**vars(builtins), "__import__": missing_dependency}})
    assert caught.value.name == "nested_dependency"


def test_desired_state_legacy_parent_uses_legacy_reader(capsys, monkeypatch, tmp_path) -> None:
    import builtins
    from types import SimpleNamespace

    runner = load_runner()
    monkeypatch.setattr("sys.argv", ["read-desired-state", str(tmp_path / "state.sqlite")])
    original_import = builtins.__import__
    calls = []

    def legacy_import(name, *args, **kwargs):
        calls.append(name)
        if name == "hyprial.daemon.impl.desired_state.sqlite":
            raise ModuleNotFoundError("old package has no impl", name="hyprial.daemon.impl")
        if name == "hyprial.daemon.desired_state_sqlite":
            return SimpleNamespace(DesiredStateSqliteShadow=lambda db: SimpleNamespace(read_document=lambda: {"providers": []}))
        if name == "hyprial.daemon.state_db":
            return SimpleNamespace(StateDatabase=lambda path: path)
        return original_import(name, *args, **kwargs)

    exec(runner._READ_DESIRED_STATE, {"__builtins__": {**vars(builtins), "__import__": legacy_import}})
    assert "hyprial.daemon.desired_state_sqlite" in calls
    assert json.loads(capsys.readouterr().out) == {"providers": []}


def test_a_tree_without_a_channel_protocol_is_an_error(tmp_path: Path) -> None:
    runner = load_runner()
    _origin, work = _repo_with_origin(tmp_path)
    contract = work / "src" / "hyprial" / "contracts" / "channel.py"
    contract.parent.mkdir(parents=True)
    contract.write_text("# no constant here\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "bare")

    with pytest.raises(AssertionError, match="declares no CHANNEL_PROTOCOL_VERSION"):
        runner.channel_protocol_version_at(work, "HEAD", dict(os.environ))


def test_isolated_environment_keeps_the_scenario_off_the_host() -> None:
    # The macos-host runner is a production host: the throwaway daemon must
    # not join its mesh, and an inherited PYTHONPATH (which outranks
    # site-packages) must not let the installed OLD daemon import the
    # candidate's source.
    entry = RUNNER_PATH.read_text(encoding="utf-8")
    assert "return _scenario_impl.main(" in entry
    source = (RUNNER_PATH.parent / "_upgrade_continuity/scenario.py").read_text(encoding="utf-8")

    assert '"HYPRIAL_PEER_DISCOVERY": "0"' in source
    leaks = source[source.index("for leak in ("): source.index("env.pop(leak, None)")]
    assert '"PYTHONPATH"' in leaks


def test_upgrade_scenarios_publish_tag_only_contracts() -> None:
    inventory = json.loads(SCENARIO_INVENTORY.read_text(encoding="utf-8"))
    scenarios = {item["id"]: item for item in inventory["scenarios"]}

    for scenario_id in ("E2E-005", "E2E-010"):
        assertions = " ".join(scenarios[scenario_id]["assertions"])
        assert "v0.4.0 tag" in assertions
        assert "dev commit" not in assertions

    upgrade_assertions = " ".join(scenarios["E2E-010"]["assertions"])
    assert "automatically restarts" in upgrade_assertions
    assert "before and after restart" in upgrade_assertions
    assert "without restarting" not in upgrade_assertions
