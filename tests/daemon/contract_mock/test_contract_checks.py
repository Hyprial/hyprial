"""Coverage, sensitivity and breaking-diff checks for the M-03 artifacts."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors

ROOT = Path(__file__).resolve().parents[3]


def _load(relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


from contract_mock.sensitive import (  # noqa: E402
    SENTINEL,
    SENTINEL_B64,
    SensitiveMaterialError,
    assert_no_sensitive_material,
    scan_value,
)

from .conftest import demo_openapi  # noqa: E402


def test_verifier_passes_a_complete_projection() -> None:
    verifier = _load("scripts/verify/check_agent_contract_examples.py")
    report = verifier.evaluate(
        demo_openapi(), required=("demo.list", "demo.write")
    )
    assert report.verdict == "PASS"
    assert report.failures == []
    assert report.sensitive == []


def test_verifier_blocks_absent_dependency_methods() -> None:
    verifier = _load("scripts/verify/check_agent_contract_examples.py")
    report = verifier.evaluate(demo_openapi())
    assert report.verdict == "BLOCKED"
    assert "agent.list" in report.missing_methods
    assert report.failures == []


def test_verifier_fails_a_missing_scenario() -> None:
    verifier = _load("scripts/verify/check_agent_contract_examples.py")
    document = copy.deepcopy(demo_openapi())
    operation = document["paths"]["/daemon/request/demo.list"]["post"]
    operation["x-hyprial-examples"] = [
        case for case in operation["x-hyprial-examples"] if case["kind"] != "empty"
    ]
    report = verifier.evaluate(document, required=("demo.list",))
    assert report.verdict == "FAIL"
    assert any("empty" in failure for failure in report.failures)


def test_verifier_fails_published_secret_material() -> None:
    verifier = _load("scripts/verify/check_agent_contract_examples.py")
    document = copy.deepcopy(demo_openapi())
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    operation["x-hyprial-examples"][0]["params"]["setupToken"] = "sk-real-secret"
    report = verifier.evaluate(document, required=("demo.write",))
    assert report.verdict == "FAIL"
    assert report.sensitive


def test_sensitive_scanner_allows_only_the_synthetic_sentinel() -> None:
    clean = {
        "setupToken": SENTINEL,
        "files": [{"filename": "auth.json", "contentBase64": SENTINEL_B64}],
        "path": "/tmp/hyprial-example/auth.json",
        "ownerEntityToken": None,
    }
    assert scan_value(clean) == []
    assert_no_sensitive_material(clean)
    for bad in (
        {"setupToken": "real-token"},
        {"files": [{"contentBase64": "cmVhbC1zZWNyZXQ="}]},
        {"note": "sk-live-123"},
        {"note": "-----BEGIN PRIVATE KEY-----"},
        {"note": "/Users/someone/.config/token"},
        {"note": "A" * 80},
        {"ownerEntityToken": "real-identity-token"},
    ):
        assert scan_value(bad)
    with pytest.raises(SensitiveMaterialError):
        assert_no_sensitive_material({"setupToken": "real-token"})


def _minimal_document(result_schema: dict, examples: list[dict]) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "1"},
        "paths": {
            "/daemon/request/demo.list": {
                "post": {
                    "operationId": "demo.list",
                    "x-hyprial-examples": examples,
                }
            }
        },
        "x-hyprial-methods": [
            {
                "name": "demo.list",
                "owner": "daemon.demo",
                "mode": "unary",
                "mutates": False,
                "retry": "safe",
                "audience": ["local-operator"],
                "httpExposed": True,
                "confirmation": "none",
                "paramsSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                "resultSchema": result_schema,
                "errors": [{"code": "INVALID_ARGUMENT", "dataSchema": None}],
            }
        ],
    }


def _base_examples() -> list[dict]:
    return [
        {"name": "ok", "kind": "success", "description": "ok", "params": {}, "result": {"ok": True}},
        {
            "name": "bad",
            "kind": "invalid-params",
            "description": "bad",
            "params": {},
            "error": {"code": "INVALID_ARGUMENT", "message": "bad"},
        },
        {
            "name": "empty",
            "kind": "empty",
            "description": "empty",
            "params": {},
            "result": {"ok": True},
            "reason": "legal",
        },
        {
            "name": "down",
            "kind": "domain-error",
            "description": "down",
            "params": {},
            "error": {"code": "DAEMON_NOT_READY", "message": "down"},
        },
    ]


def test_diff_is_green_for_the_same_document() -> None:
    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    document = demo_openapi()
    assert diff.compare(document, copy.deepcopy(document)) == []


def test_diff_removes_a_method_or_path_as_breaking() -> None:
    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    base = demo_openapi()
    head = copy.deepcopy(base)
    del head["x-hyprial-methods"][0]
    findings = diff.compare(base, head)
    assert any(
        finding.severity == "BREAKING" and "method" in finding.where
        for finding in findings
    )
    assert any(finding.severity == "BREAKING" for finding in findings)


def test_diff_adds_optional_property_as_compatible_and_required_as_breaking() -> None:
    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    schema = {
        "type": "object",
        "properties": {"ok": {"const": True}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    base = _minimal_document(schema, _base_examples())
    optional = copy.deepcopy(base)
    optional["x-hyprial-methods"][0]["resultSchema"]["properties"]["extra"] = {
        "type": "string"
    }
    findings = diff.compare(base, optional)
    assert any(finding.severity == "COMPATIBLE" for finding in findings)
    assert not any(finding.severity == "BREAKING" for finding in findings)
    required = copy.deepcopy(optional)
    required["x-hyprial-methods"][0]["resultSchema"]["required"].append("extra")
    findings = diff.compare(base, required)
    assert any(
        finding.severity == "BREAKING" and "required" in finding.where
        for finding in findings
    )


def test_diff_removes_an_enum_value_as_breaking() -> None:
    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    base = _minimal_document(
        {
            "type": "object",
            "properties": {"mode": {"enum": ["a", "b"]}},
            "required": ["mode"],
            "additionalProperties": False,
        },
        _base_examples(),
    )
    head = copy.deepcopy(base)
    head["x-hyprial-methods"][0]["resultSchema"]["properties"]["mode"]["enum"] = ["a"]
    findings = diff.compare(base, head)
    assert any(
        finding.severity == "BREAKING" and "enum" in finding.where
        for finding in findings
    )


def test_diff_removes_an_example_or_adds_a_gap_as_breaking() -> None:
    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    base = _minimal_document(
        {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        _base_examples(),
    )
    removed = copy.deepcopy(base)
    removed["paths"]["/daemon/request/demo.list"]["post"]["x-hyprial-examples"] = [
        case
        for case in removed["paths"]["/daemon/request/demo.list"]["post"][
            "x-hyprial-examples"
        ]
        if case["name"] != "empty"
    ]
    assert any(
        finding.severity == "BREAKING" and "empty" in finding.where
        for finding in diff.compare(base, removed)
    )
    gap = copy.deepcopy(base)
    gap["paths"]["/daemon/request/demo.list"]["post"]["x-hyprial-example-gaps"] = {
        "empty": "not applicable"
    }
    assert any(
        finding.severity == "BREAKING" and "gaps" in finding.where
        for finding in diff.compare(base, gap)
    )


def test_main_requires_an_explicit_contract_impact(tmp_path: Path) -> None:
    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    base = _minimal_document(
        {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        _base_examples(),
    )
    head = copy.deepcopy(base)
    del head["x-hyprial-methods"][0]
    base_path = tmp_path / "base.json"
    head_path = tmp_path / "head.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    head_path.write_text(json.dumps(head), encoding="utf-8")
    assert diff.main(["--base", str(base_path), "--head", str(head_path)]) == 1
    body = tmp_path / "pr-body.txt"
    body.write_text("Contract-Impact: reviewed removal\n", encoding="utf-8")
    assert (
        diff.main(
            [
                "--base",
                str(base_path),
                "--head",
                str(head_path),
                "--pr-body-file",
                str(body),
            ]
        )
        == 0
    )


def test_verifier_accepts_a_semantic_parameter_error_for_the_error_slot() -> None:
    verifier = _load("scripts/verify/check_agent_contract_examples.py")
    document = copy.deepcopy(demo_openapi())
    operation = document["paths"]["/daemon/request/demo.list"]["post"]
    for case in operation["x-hyprial-examples"]:
        if case["kind"] == "domain-error":
            case["kind"] = "semantic-param-error"
    report = verifier.evaluate(document, required=("demo.list",))
    assert report.verdict == "PASS"
    assert report.failures == []


def test_the_real_five_method_projection_passes_the_gate() -> None:
    from hyprial.daemon import daemon_openapi

    verifier = _load("scripts/verify/check_agent_contract_examples.py")
    report = verifier.evaluate(daemon_openapi())
    assert report.verdict == "PASS"
    assert report.missing_methods == []
    assert report.failures == []
    assert report.sensitive == []
    assert set(report.examples) == {
        "agent.list",
        "agent.secret.list",
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
    }


def test_ci_gate_entrypoints_consume_the_committed_projection() -> None:
    projection = ROOT / "contract/daemon-request/openapi.json"
    verifier = _load("scripts/verify/check_agent_contract_examples.py")
    assert verifier.main(["--openapi", str(projection)]) == 0


def test_diff_checker_rejects_a_removed_method_across_explicit_files(
    tmp_path: Path,
) -> None:
    """Unit test of the checker itself: two explicit documents, no git."""

    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    projection = ROOT / "contract/daemon-request/openapi.json"
    removed = json.loads(projection.read_text(encoding="utf-8"))
    del removed["paths"]["/daemon/request/agent.list"]
    candidate = tmp_path / "removed-agent-list.json"
    candidate.write_text(json.dumps(removed), encoding="utf-8")
    assert diff.main(["--base", str(projection), "--head", str(candidate)]) == 1


def _git_repo_with_divergent_projection(tmp_path: Path, removed: bool) -> Path:
    """A two-branch repo whose merge-base holds the committed projection.

    ``main`` gains a further breaking change after ``feature`` forks, so a
    gate keyed on the base branch tip would report a different breaking set
    than a gate keyed on the merge-base.
    """

    projection = json.loads(
        (ROOT / "contract/daemon-request/openapi.json").read_text(encoding="utf-8")
    )
    repo = tmp_path / "projection-repo"

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        )

    target = repo / "contract" / "daemon-request" / "openapi.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(projection), encoding="utf-8")
    git("init", "-b", "main")
    git("config", "user.email", "gate@example.invalid")
    git("config", "user.name", "gate")
    git("add", ".")
    git("commit", "-m", "base")
    git("checkout", "-b", "feature")
    if removed:
        fork = copy.deepcopy(projection)
        del fork["paths"]["/daemon/request/agent.list"]
        target.write_text(json.dumps(fork), encoding="utf-8")
        git("commit", "-am", "feature head")
    git("checkout", "--quiet", "main")
    del projection["paths"]["/daemon/request/agent.secret.list"]
    target.write_text(json.dumps(projection), encoding="utf-8")
    git("commit", "-am", "main moves on after the fork")
    git("checkout", "--quiet", "feature")
    return repo


def test_ci_gate_diff_compares_against_the_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CI gate entrypoint diffs the committed projection against the
    projection at merge-base(base-ref, HEAD), not against itself and not
    against the base branch tip; an unresolvable base fails closed."""

    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    head_path = "contract/daemon-request/openapi.json"
    clean = _git_repo_with_divergent_projection(tmp_path / "clean", removed=False)
    monkeypatch.chdir(clean)
    assert diff.main(["--base-ref", "main", "--head", str(clean / head_path)]) == 0
    breaking = _git_repo_with_divergent_projection(tmp_path / "breaking", removed=True)
    monkeypatch.chdir(breaking)
    assert diff.main(["--base-ref", "main", "--head", str(breaking / head_path)]) == 1
    rendered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/ci/check_daemon_contract_diff.py"),
            "--base-ref",
            "main",
            "--head",
            str(breaking / head_path),
        ],
        cwd=breaking,
        capture_output=True,
        text=True,
    )
    assert rendered.returncode == 1
    assert "agent.list" in rendered.stdout
    assert "agent.secret.list" not in rendered.stdout
    orphaned = tmp_path / "orphaned"
    orphaned.mkdir()
    (orphaned / head_path).parent.mkdir(parents=True, exist_ok=True)
    (orphaned / head_path).write_text("{}", encoding="utf-8")
    subprocess.run(["git", "-C", str(orphaned), "init", "-b", "main"], check=True)
    monkeypatch.chdir(orphaned)
    assert diff.main(["--base-ref", "main", "--head", str(orphaned / head_path)]) == 2


def test_ci_gate_diff_treats_a_base_without_a_projection_as_first_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first promotion to a branch that never carried the projection
    (main before 0.5.5.3) diffs against an empty projection: every method is
    an addition, so the gate is green without a Contract-Impact line."""

    diff = _load("scripts/ci/check_daemon_contract_diff.py")
    head_path = "contract/daemon-request/openapi.json"
    repo = tmp_path / "first-publication"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "gate@example.invalid")
    git("config", "user.name", "gate")
    (repo / "README").write_text("main before the projection\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "main without a projection")
    git("checkout", "-b", "promotion")
    target = repo / head_path
    target.parent.mkdir(parents=True)
    target.write_text(
        (ROOT / "contract/daemon-request/openapi.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-m", "promotion publishes the projection")
    monkeypatch.chdir(repo)
    assert diff.main(["--base-ref", "main", "--head", str(target)]) == 0


def test_m03_example_modules_have_no_literal_ipc_error_codes() -> None:
    paths = (
        ROOT / "src/hyprial/daemon/impl/request_catalog/agent/examples.py",
        ROOT / "src/hyprial/kernel/impl/contracts/daemon/examples/__init__.py",
    )
    offenders: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in ipc_errors.ALL_CODES
            ):
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:{node.value}"
                )
    assert not offenders, offenders
