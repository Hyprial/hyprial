"""Admission tests for the A03 contract gates and the first-ledger seed."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/check_request_contracts.py"
SEED = ROOT / "scripts/ci/a03-contract-seed.json"
SPEC = importlib.util.spec_from_file_location("request_contract_gate", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(GATE)


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a04",
        "GIT_AUTHOR_EMAIL": "a04@example.invalid",
        "GIT_COMMITTER_NAME": "a04",
        "GIT_COMMITTER_EMAIL": "a04@example.invalid",
    }
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def _empty_base(root: Path) -> str:
    tree = _git(root, "hash-object", "-t", "tree", "/dev/null")
    return _git(root, "commit-tree", tree, "-m", "empty test base")


def _base_with_smaller_import_ledger(root: Path, tmp_path: Path) -> str:
    ledger_path = "scripts/ci/import-ratchet-baseline.json"
    document = json.loads((root / ledger_path).read_text(encoding="utf-8"))
    document["edges"] = document["edges"][1:]
    blob = _git(
        root,
        "hash-object",
        "-w",
        "--stdin",
        input_text=json.dumps(document, indent=2) + "\n",
    )
    index = tmp_path / "small-ledger.index"
    env = {
        **os.environ,
        "GIT_INDEX_FILE": str(index),
        "GIT_AUTHOR_NAME": "a04",
        "GIT_AUTHOR_EMAIL": "a04@example.invalid",
        "GIT_COMMITTER_NAME": "a04",
        "GIT_COMMITTER_EMAIL": "a04@example.invalid",
    }

    def indexed(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return result.stdout.strip()

    indexed("read-tree", "HEAD")
    indexed(
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},{ledger_path}",
    )
    tree = indexed("write-tree")
    return indexed("commit-tree", tree, "-m", "smaller import ledger")


def _check(root: Path, base: str, *, base_branch: str, allow_first_seed: bool, seed: Path = SEED):
    return GATE.check(
        root,
        base_ref=base,
        base_branch=base_branch,
        allow_first_seed=allow_first_seed,
        seed_path=seed,
    )


def _kinds(report: dict[str, object]) -> list[str]:
    violations = report.get("violations")
    assert isinstance(violations, list)
    return [str(row.get("kind")) for row in violations]


def test_real_base_ledgers_pass_the_ordinary_comparison() -> None:
    report = _check(
        ROOT,
        "HEAD",
        base_branch="ci/a04-command-surface",
        allow_first_seed=False,
    )

    assert report["status"] == "PASS", report
    assert report["firstSeedAllowed"] is False
    assert report["ledgers"]["importRatchet"]["firstSeedUsed"] is False
    assert report["ledgers"]["legacyMethods"]["firstSeedUsed"] is False


def test_missing_base_without_the_accepted_seed_fails_closed() -> None:
    base = _empty_base(ROOT)

    report = _check(ROOT, base, base_branch="dev", allow_first_seed=False)

    assert report["status"] == "FAIL", report
    assert _kinds(report) == ["missing-base-ledger", "missing-base-ledger"]


def test_seed_digests_are_immutable_and_head_ledger_is_a_reviewed_decrease() -> None:
    """The seed pins the accepted A03 blobs; only the ledgers shrink at head.

    The OpenAPI entry is a historical anchor: it names the snapshot bytes that
    existed at ``acceptedSha``.  Declaring new methods legitimately changes the
    head snapshot, so the head is verified against the deterministic exporter
    instead of being forced back to the historical bytes.
    """

    seed = json.loads(SEED.read_text(encoding="utf-8"))
    assert seed["acceptedSha"] == GATE.ACCEPTED_SHA
    for key in GATE.LEDGER_KEYS:
        entry = seed[key]
        assert entry["sha256"] == GATE.REVIEWED_LEDGER_DIGESTS[key]
    openapi = seed["openapi"]
    assert openapi["sha256"] == GATE.OPENAPI_SHA256

    accepted = seed["acceptedSha"]
    if not GATE._ref_exists(ROOT, accepted):
        # A shallow CI shard has no accepted commit: assert the fail-closed
        # behavior rather than skipping or pretending the blob was verified.
        assert GATE._accepted_problems(
            ROOT, "HEAD", seed
        ) == [{"kind": "accepted-commit-missing", "acceptedSha": accepted}]
        return
    accepted_openapi = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{accepted}:{openapi['path']}"],
        capture_output=True, check=True,
    )
    assert hashlib.sha256(accepted_openapi.stdout).hexdigest() == openapi["sha256"]
    for key in GATE.LEDGER_KEYS:
        entry = seed[key]
        result = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{accepted}:{entry['path']}"],
            capture_output=True,
            check=True,
        )
        assert hashlib.sha256(result.stdout).hexdigest() == entry["sha256"], key
    historical_openapi = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{accepted}:{openapi['path']}"],
        capture_output=True,
        check=True,
    )
    assert hashlib.sha256(historical_openapi.stdout).hexdigest() == openapi["sha256"]
    assert GATE._run_openapi_gate(ROOT)["status"] == "PASS"
    # The head ledger may only shrink from the accepted A03 seed.  It does not
    # have to equal the seed bytes anymore: A05 moved real preload imports to a
    # shell bootstrap and recorded an exact exception manifest separately.
    accepted_doc = json.loads(
        subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{accepted}:scripts/ci/import-ratchet-baseline.json"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    )
    accepted_edges = {
        (row["path"], row["module"], row["symbol"]): row["count"]
        for row in accepted_doc["edges"]
    }
    head_edges = {
        (row["path"], row["module"], row["symbol"]): row["count"]
        for row in json.loads((ROOT / "scripts/ci/import-ratchet-baseline.json").read_text())["edges"]
    }
    assert set(head_edges) <= set(accepted_edges)
    assert all(head_edges[key] <= accepted_edges[key] for key in head_edges)
    assert head_edges != accepted_edges


def _tiny_repo(tmp_path: Path):
    """A complete, isolated repo with the real ledger bytes and a synthetic anchor."""

    repo = tmp_path / "tiny"
    repo.mkdir()
    _git(repo, "init", "-q")
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    # This is a synthetic accepted commit, independent of the checkout's
    # history. Pin the fixture's own starting bytes; production seed constants
    # and missing-real-anchor rejection are tested separately above/below.
    fixture_digests = {}
    for key in GATE.LEDGER_KEYS:
        path = Path(seed[key]["path"])
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, target)
        fixture_digests[key] = hashlib.sha256(target.read_bytes()).hexdigest()
        seed[key]["sha256"] = fixture_digests[key]
    _git(repo, "add", "scripts/ci")
    _git(repo, "commit", "-qm", "accepted fixture")
    accepted = _git(repo, "rev-parse", "HEAD")
    seed["acceptedSha"] = accepted
    seed_path = repo / "scripts/ci/a03-contract-seed.json"
    seed_path.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")
    _git(repo, "add", "scripts/ci/a03-contract-seed.json")
    _git(repo, "commit", "-qm", "head fixture")
    return repo, accepted, seed_path, fixture_digests


def test_first_seed_accepts_only_exact_accepted_ledgers_on_dev(tmp_path: Path) -> None:
    repo, accepted, seed_path, fixture_digests = _tiny_repo(tmp_path)
    base = _empty_base(repo)
    report = GATE._seed_admission(
        repo,
        base_ref=base,
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert report["status"] == "PASS", report
    assert report["firstSeedAllowed"] is True
    assert report["ledgers"]["importRatchet"]["firstSeedUsed"] is True
    assert report["ledgers"]["legacyMethods"]["firstSeedUsed"] is True


def test_first_seed_is_accepted_on_main_for_the_first_promotion(tmp_path: Path) -> None:
    repo, accepted, seed_path, fixture_digests = _tiny_repo(tmp_path)
    report = GATE._seed_admission(
        repo,
        base_ref=_empty_base(repo),
        base_branch="main",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert report["status"] == "PASS", report
    assert report["firstSeedAllowed"] is True
    assert report["ledgers"]["importRatchet"]["firstSeedUsed"] is True
    assert report["ledgers"]["legacyMethods"]["firstSeedUsed"] is True


def _rewrite_import_ledger(repo: Path, mutate) -> None:
    path = repo / "scripts/ci/import-ratchet-baseline.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document["edges"])
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _git(repo, "add", "scripts/ci/import-ratchet-baseline.json")
    _git(repo, "commit", "-qm", "head import ledger change")


def test_first_seed_accepts_a_strict_import_ledger_subset(tmp_path: Path) -> None:
    repo, accepted, seed_path, fixture_digests = _tiny_repo(tmp_path)
    _rewrite_import_ledger(repo, lambda edges: edges.pop())
    report = GATE._seed_admission(
        repo,
        base_ref=_empty_base(repo),
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert report["status"] == "PASS", report
    assert report["ledgers"]["importRatchet"]["firstSeedSubset"] is True


def test_first_seed_refuses_import_ledger_growth(tmp_path: Path) -> None:
    repo, accepted, seed_path, fixture_digests = _tiny_repo(tmp_path)
    _rewrite_import_ledger(
        repo,
        lambda edges: edges.append(
            {
                "path": "src/hyprial/shell/impl/cli/commands/demo.py",
                "module": "hyprial.daemon",
                "symbol": "BusinessStore",
                "count": 1,
            }
        ),
    )
    report = GATE._seed_admission(
        repo,
        base_ref=_empty_base(repo),
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert report["status"] == "FAIL", report
    assert "seed-head-import-growth" in _kinds(report)


def test_first_seed_refuses_growth_of_an_existing_edge_count(tmp_path: Path) -> None:
    repo, accepted, seed_path, fixture_digests = _tiny_repo(tmp_path)

    def grow(edges):
        edges[0]["count"] += 1

    _rewrite_import_ledger(repo, grow)
    report = GATE._seed_admission(
        repo,
        base_ref=_empty_base(repo),
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert report["status"] == "FAIL", report
    assert "seed-head-import-growth" in _kinds(report)


def test_first_seed_does_not_stringify_symbol_none(tmp_path: Path) -> None:
    repo, accepted, seed_path, fixture_digests = _tiny_repo(tmp_path)

    def add_string_none(edges):
        edges.append(
            {
                "path": "src/hyprial/shell/impl/cli/commands/demo.py",
                "module": "hyprial.daemon",
                "symbol": "None",
                "count": 1,
            }
        )

    _rewrite_import_ledger(repo, add_string_none)
    report = GATE._seed_admission(
        repo,
        base_ref=_empty_base(repo),
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert report["status"] == "FAIL", report
    assert "seed-head-import-growth" in _kinds(report)


def test_first_seed_rejects_duplicate_or_wrong_count_types(tmp_path: Path) -> None:
    repo, accepted, seed_path, fixture_digests = _tiny_repo(tmp_path)

    def duplicate(edges):
        edges.append(dict(edges[0]))

    _rewrite_import_ledger(repo, duplicate)
    report = GATE._seed_admission(
        repo,
        base_ref=_empty_base(repo),
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert report["status"] == "FAIL", report
    assert "seed-head-invalid-ledger" in _kinds(report)


def test_first_seed_is_refused_on_a_non_dev_base() -> None:
    base = _empty_base(ROOT)

    report = _check(
        ROOT,
        base,
        base_branch="intg/cli-command-surface",
        allow_first_seed=True,
    )

    assert report["status"] == "FAIL", report
    assert _kinds(report) == ["missing-base-ledger", "missing-base-ledger"]


def test_altered_seed_is_rejected_before_any_ledger_check(tmp_path: Path) -> None:
    base = _empty_base(ROOT)
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    seed["importRatchet"]["sha256"] = "0" * 64
    altered = tmp_path / "seed.json"
    altered.write_text(json.dumps(seed), encoding="utf-8")

    with pytest.raises(GATE.AdmissionError, match="reviewed anchor"):
        _check(
            ROOT,
            base,
            base_branch="dev",
            allow_first_seed=True,
            seed=altered,
        )


def test_altered_openapi_anchor_is_rejected(tmp_path: Path) -> None:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    seed["openapi"]["sha256"] = "0" * 64
    altered = tmp_path / "altered-openapi.json"
    altered.write_text(json.dumps(seed), encoding="utf-8")

    with pytest.raises(GATE.AdmissionError, match="openapi.sha256"):
        GATE._load_seed(altered)


def test_current_projection_drift_is_rejected_by_the_production_gate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    script = repo / GATE.OPENAPI_GATE
    script.parent.mkdir(parents=True)
    shutil.copy2(ROOT / GATE.OPENAPI_GATE, script)
    snapshot = repo / "contract/daemon-request/openapi.json"
    snapshot.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "contract/daemon-request/openapi.json", snapshot)

    assert GATE._run_openapi_gate(repo)["status"] == "PASS"
    snapshot.write_text("{}\n", encoding="utf-8")
    assert GATE._run_openapi_gate(repo)["status"] == "FAIL"


def test_altered_accepted_sha_is_rejected(tmp_path: Path) -> None:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    seed["acceptedSha"] = "0" * 40
    altered = tmp_path / "altered-accepted.json"
    altered.write_text(json.dumps(seed), encoding="utf-8")

    with pytest.raises(GATE.AdmissionError, match="reviewed anchor"):
        GATE._seed_admission(
            ROOT,
            base_ref="HEAD",
            base_branch="ci/a04-command-surface",
            allow_first_seed=False,
            seed_path=altered,
        )


def _commit_without_ledgers(root: Path, parent: str, removed: tuple[str, ...], tmp_path: Path) -> str:
    index = tmp_path / "without-ledgers.index"
    env = {
        **os.environ,
        "GIT_INDEX_FILE": str(index),
        "GIT_AUTHOR_NAME": "a04",
        "GIT_AUTHOR_EMAIL": "a04@example.invalid",
        "GIT_COMMITTER_NAME": "a04",
        "GIT_COMMITTER_EMAIL": "a04@example.invalid",
    }

    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return result.stdout.strip()

    run("read-tree", parent)
    for path in removed:
        run("update-index", "--force-remove", path)
    tree = run("write-tree")
    return run("commit-tree", tree, "-p", parent, "-m", "without ledgers")


def test_seed_already_landed_blocks_a_second_bootstrap(tmp_path: Path) -> None:
    repo, accepted, seed_path, fixture_digests = _tiny_repo(tmp_path)
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    base = _commit_without_ledgers(
        repo,
        accepted,
        (seed["importRatchet"]["path"], seed["legacyMethods"]["path"]),
        tmp_path,
    )
    report = GATE._seed_admission(
        repo,
        base_ref=base,
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert report["status"] == "FAIL", report
    assert "seed-already-landed" in _kinds(report)


def test_partial_base_ledgers_are_not_a_first_seed(tmp_path: Path) -> None:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    base = _commit_without_ledgers(
        ROOT,
        "HEAD",
        (seed["legacyMethods"]["path"],),
        tmp_path,
    )

    report = _check(ROOT, base, base_branch="dev", allow_first_seed=True)

    assert report["status"] == "FAIL", report
    assert "missing-base-ledger" in _kinds(report)
    assert report["firstSeedAllowed"] is False


def test_shallow_checkout_does_not_prove_seed_ancestry(tmp_path: Path) -> None:
    repo, accepted, _seed_path, fixture_digests = _tiny_repo(tmp_path)
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", "--no-local", f"file://{repo}", str(clone)],
        check=True,
        capture_output=True,
    )
    seed_path = clone / "scripts/ci/a03-contract-seed.json"
    base = _empty_base(clone)

    before = GATE._seed_admission(
        clone,
        base_ref=base,
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert before["status"] == "FAIL", before
    assert "ancestry-unproven-in-shallow" in _kinds(before)

    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "fetch",
            "--quiet",
            "--unshallow",
            "origin",
            _git(clone, "rev-parse", "HEAD"),
        ],
        check=True,
        capture_output=True,
    )
    after = GATE._seed_admission(
        clone,
        base_ref=base,
        base_branch="dev",
        allow_first_seed=True,
        seed_path=seed_path,
        accepted_sha=accepted,
        ledger_digests=fixture_digests,
    )
    assert after["status"] == "PASS", after
    assert after["ledgers"]["importRatchet"]["firstSeedUsed"] is True


def test_missing_accepted_commit_in_a_complete_repo_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "base")

    problems = GATE._accepted_problems(repo, "HEAD", json.loads(SEED.read_text(encoding="utf-8")))

    assert problems == [
        {"kind": "accepted-commit-missing", "acceptedSha": GATE.ACCEPTED_SHA}
    ]


def _gate_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "gates"
    repo.mkdir()
    _git(repo, "init", "-q")
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    accepted_sha = GATE.ACCEPTED_SHA
    accepted_available = GATE._ref_exists(ROOT, accepted_sha)
    for key in GATE.LEDGER_KEYS:
        path = Path(seed[key]["path"])
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if accepted_available:
            blob = subprocess.run(
                ["git", "-C", str(ROOT), "show", f"{accepted_sha}:{path}"],
                capture_output=True,
                check=True,
            ).stdout
            target.write_bytes(blob)
        else:
            shutil.copy2(ROOT / path, target)
    seed_path = repo / "scripts/ci/a03-contract-seed.json"
    seed_path.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")
    stubs = {
        "scripts/verify/dump_daemon_openapi.py": (
            "import os, sys\n"
            "from pathlib import Path\n"
            "Path('gate-calls.log').open('a').write('openapi\\n')\n"
            "sys.exit(1 if os.environ.get('A04_TEST_FAIL_GATE') == 'openapi' else 0)\n"
        ),
        "scripts/ci/check_import_ratchet.py": (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path('gate-calls.log').open('a').write('importRatchet\\n')\n"
            "fail = os.environ.get('A04_TEST_FAIL_GATE') == 'importRatchet'\n"
            "print(json.dumps({'status': 'FAIL' if fail else 'PASS', 'violations': [{'kind': 'forced'}] if fail else []}))\n"
            "sys.exit(1 if fail else 0)\n"
        ),
        "scripts/ci/check_method_registration.py": (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path('gate-calls.log').open('a').write('legacyMethods\\n')\n"
            "fail = os.environ.get('A04_TEST_FAIL_GATE') == 'legacyMethods'\n"
            "print(json.dumps({'status': 'FAIL' if fail else 'PASS', 'violations': [{'kind': 'forced'}] if fail else []}))\n"
            "sys.exit(1 if fail else 0)\n"
        ),
    }
    for relative, content in stubs.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "gate fixture")
    return repo


@pytest.mark.parametrize("failing", [None, "openapi", "importRatchet", "legacyMethods"])
def test_check_runs_all_three_gates_and_propagates_any_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str | None
) -> None:
    repo = _gate_fixture(tmp_path)
    if failing is None:
        monkeypatch.delenv("A04_TEST_FAIL_GATE", raising=False)
    else:
        monkeypatch.setenv("A04_TEST_FAIL_GATE", failing)

    report = GATE.check(
        repo,
        base_ref="HEAD",
        base_branch="ci/a04-command-surface",
        allow_first_seed=False,
        seed_path=repo / "scripts/ci/a03-contract-seed.json",
    )

    assert set(report["gates"]) == {"openapi", "importRatchet", "legacyMethods"}
    calls = set((repo / "gate-calls.log").read_text(encoding="utf-8").split())
    assert calls == {"openapi", "importRatchet", "legacyMethods"}
    if failing is None:
        assert report["status"] == "PASS", report
    else:
        assert report["status"] == "FAIL", report
        assert any(
            row.get("kind") == "gate-failed" for row in report["violations"]
        ), report


def test_joint_seed_and_ledger_tamper_fails(tmp_path: Path) -> None:
    worktree = tmp_path / "joint-tamper"
    subprocess.run(
        ["git", "-C", str(ROOT), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True,
        capture_output=True,
    )
    try:
        ledger_path = worktree / "scripts/ci/import-ratchet-baseline.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["edges"] = ledger["edges"][:-1]
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
        digest = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
        seed_path = worktree / "scripts/ci/a03-contract-seed.json"
        seed_path.write_text(SEED.read_text(encoding="utf-8"), encoding="utf-8")
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        seed["importRatchet"]["sha256"] = digest
        seed_path.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")

        with pytest.raises(GATE.AdmissionError, match="reviewed anchor"):
            GATE._seed_admission(
                worktree,
                base_ref="HEAD",
                base_branch="ci/a04-command-surface",
                allow_first_seed=False,
                seed_path=seed_path,
            )
    finally:
        subprocess.run(
            ["git", "-C", str(ROOT), "worktree", "remove", "--force", str(worktree)],
            check=True,
            capture_output=True,
        )


def test_base_ledger_growth_is_not_admitted(tmp_path: Path) -> None:
    base = _base_with_smaller_import_ledger(ROOT, tmp_path)

    report = _check(
        ROOT,
        base,
        base_branch="intg/cli-command-surface",
        allow_first_seed=False,
    )

    assert report["status"] == "FAIL", report
    assert "gate-failed" in _kinds(report)
    gate = report["gates"]["importRatchet"]
    assert any(
        row.get("kind") in {"base-import-edge-growth", "ledger-growth"}
        for row in gate["report"]["violations"]
    ), gate


def test_invalid_seed_shape_fails_closed(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text("{}", encoding="utf-8")

    with pytest.raises(GATE.AdmissionError):
        GATE._seed_admission(
            ROOT,
            base_ref="HEAD",
            base_branch="ci/a04-command-surface",
            allow_first_seed=False,
            seed_path=seed,
        )
