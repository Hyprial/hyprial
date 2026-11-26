"""Runner selection must not hide pre-job image acquisitions.

PR #2137 introduced singleton lists and a release/base-ref selector. Each
possible container label needs its own image site; unknown syntax or labels
must fail closed. These fixtures do not contact or provision real runners.
"""

from pathlib import Path

import pytest
import yaml

from tests.support.ci import _ci_download_scanner as scanner
from tests.support.ci_download import runners

WF = ".github/workflows/fixture.yml"
SELECTOR = "${{ startsWith(github.base_ref, 'release/') && 'release-ci' || 'ubuntu-latest' }}"


def scan(tmp_path, runs_on, *, matrix=False):
    job = {"runs-on": runs_on, "steps": []}
    if matrix:
        job["strategy"] = {"matrix": {"shard": [1, 2]}}
    return scanner.Scanner(tmp_path).scan({WF: yaml.safe_dump({"jobs": {"build": job}})})


@pytest.mark.parametrize(
    "runs_on,expected",
    [
        ("ubuntu-latest", {"ubuntu-latest"}),
        ("publish-github", {"publish-github"}),
        ("release-ci", {"release-ci"}),
        (["release-ci"], {"release-ci"}),
        (["ubuntu-latest"], {"ubuntu-latest"}),
        (SELECTOR, {"release-ci", "ubuntu-latest"}),
        ([SELECTOR], {"release-ci", "ubuntu-latest"}),
        ("macos-host", set()),
        (["macos-host"], set()),
        (SELECTOR.replace("ubuntu-latest", "macos-host"), {"release-ci"}),
        (SELECTOR.replace("ubuntu-latest", "release-ci"), {"release-ci"}),
    ],
)
def test_each_possible_container_label_emits_one_image(tmp_path, runs_on, expected):
    inventory = scan(tmp_path, runs_on)
    assert not inventory.diagnostics
    assert len(inventory.sites) == len(expected)
    assert {s.command for s in inventory.sites} == {f"runner-image {label}" for label in expected}
    assert all(s.family == "IMAGE" and s.function == "implicit-image" for s in inventory.sites)


@pytest.mark.parametrize(
    "runs_on",
    [
        "unknown-runner",
        ["unknown-runner"],
        [],
        ["ubuntu-latest", "release-ci"],
        {"labels": ["release-ci"]},
        None,
        42,
        SELECTOR.replace("release-ci", "unknown-runner"),
        SELECTOR.replace("ubuntu-latest", "unknown-runner"),
        SELECTOR.replace("github.base_ref", "github.head_ref"),
        SELECTOR.replace("'release-ci'", "matrix.runner"),
        SELECTOR + " || 'macos-host'",
        "${{ matrix.runner }}",
    ],
)
def test_unknown_runner_selection_is_diagnostic(tmp_path, runs_on):
    inventory = scan(tmp_path, runs_on)
    assert any("runs-on" in d.message for d in inventory.diagnostics)
    with pytest.raises(ValueError, match="runs-on"):
        scanner.runner_labels(runs_on)


def test_selector_images_keep_matrix_callers_distinct(tmp_path):
    inventory = scan(tmp_path, SELECTOR, matrix=True)
    assert not inventory.diagnostics
    assert {(s.job, s.command) for s in inventory.sites} == {
        (f"build[shard={shard}]", f"runner-image {label}")
        for shard in (1, 2) for label in ("release-ci", "ubuntu-latest")
    }
    assert len({s.fingerprint for s in inventory.sites}) == 4


def _unregistered_runs_on(workflow_dir: Path) -> list[tuple[str, str, str]]:
    unregistered: list[tuple[str, str, str]] = []
    for path in sorted(workflow_dir.glob("*.y*ml")):
        for name, job in (yaml.safe_load(path.read_text()).get("jobs") or {}).items():
            if not isinstance(job, dict) or "runs-on" not in job:
                continue
            try:
                runners.job_runner_labels(job)
            except ValueError as exc:
                unregistered.append((path.name, name, str(exc)))
    return unregistered


def test_every_runs_on_label_is_registered():
    """The registration gate and scanner use the same fail-closed reader."""
    root = Path(__file__).resolve().parents[4]
    unregistered = _unregistered_runs_on(root / ".github/workflows")
    assert not unregistered, unregistered


@pytest.mark.parametrize(
    "label",
    ["windows-latest", "windows-2025", "windows-11-arm"],
)
def test_github_hosted_windows_vm_labels_are_registered_without_container_image(
    tmp_path, label
):
    """GitHub-hosted Windows VM labels belong to the shared hosted registry."""
    assert label in runners.GITHUB_HOSTED_VM_LABELS
    assert label not in runners.CONTAINER_RUNNER_LABELS
    assert scanner.runner_labels(label) == (label,)
    inventory = scan(tmp_path, label)
    assert not inventory.diagnostics
    assert not inventory.sites


def test_synthetic_windows_latest_workflow_has_no_unregistered_label(tmp_path):
    """A synthetic workflow on disk using runs-on: windows-latest passes discovery and label checks."""
    workflow_dir = tmp_path / ".github/workflows"
    workflow_dir.mkdir(parents=True)
    workflow = workflow_dir / "windows-followup.yml"
    workflow.write_text(
        yaml.safe_dump(
            {
                "name": "windows-followup",
                "on": "pull_request",
                "jobs": {
                    "smoke": {
                        "runs-on": "windows-latest",
                        "steps": [],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    inventory = scanner.Scanner(tmp_path).scan()
    assert not inventory.diagnostics
    assert not inventory.sites
    for name, job in (yaml.safe_load(workflow.read_text()).get("jobs") or {}).items():
        assert scanner.runner_labels(job["runs-on"]) == ("windows-latest",), name


def test_matrix_runs_on_expands_registered_labels_including_include_overrides():
    """Static ``${{ matrix.<key> }}`` expands top-level axes and ``include`` entries."""
    job = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {
            "matrix": {
                "os": ["ubuntu-latest", "windows-latest"],
                "include": [
                    {"os": "windows-2025"},
                    {"os": "windows-11-arm"},
                ],
            }
        },
        "steps": [],
    }
    assert runners.job_runner_labels(job) == (
        "ubuntu-latest",
        "windows-latest",
        "windows-2025",
        "windows-11-arm",
    )


def test_matrix_runs_on_expands_include_only_and_key_override():
    """``include`` entries supplying or overriding the runner key are all validated."""
    job = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {
            "matrix": {
                "leg": ["base", "head"],
                "include": [
                    {"leg": "base", "os": "windows-latest"},
                    {"leg": "head", "os": "windows-2025"},
                ],
            }
        },
        "steps": [],
    }
    assert runners.job_runner_labels(job) == ("windows-latest", "windows-2025")


@pytest.mark.parametrize(
    "matrix",
    [
        {"os": ["windows-latest", "windows-2022"]},
        {"os": ["windows-latest", "macos-26"]},
        {"os": ["windows-latest", {"group": "custom-pool"}]},
        {"os": ["windows-latest"], "include": [{"os": "windows-2022"}]},
        {
            "leg": ["base", "head"],
            "include": [
                {"leg": "base", "os": "windows-latest"},
                {"leg": "head", "os": "macos-26"},
            ],
        },
        {"os": "windows-latest"},
        {"other": ["windows-latest"]},
        {},
    ],
)
def test_matrix_runs_on_with_unregistered_or_unresolved_value_fails_closed(matrix):
    """Unregistered labels, group mappings, and unresolvable matrices still raise ValueError."""
    job = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {"matrix": matrix},
        "steps": [],
    }
    with pytest.raises(ValueError, match="runs-on"):
        runners.job_runner_labels(job)


def test_matrix_runs_on_without_strategy_fails_closed():
    job = {"runs-on": "${{ matrix.os }}", "steps": []}
    with pytest.raises(ValueError, match="runs-on"):
        runners.job_runner_labels(job)


def test_registration_gate_handles_matrix_workflow_on_disk(tmp_path):
    """The workflow-directory registration gate passes hosted matrices and rejects unregistered values."""
    workflow_dir = tmp_path / ".github/workflows"
    workflow_dir.mkdir(parents=True)
    good = workflow_dir / "windows-followup.yml"
    good.write_text(
        yaml.safe_dump(
            {
                "name": "windows-followup",
                "on": "pull_request",
                "jobs": {
                    "matrix-job": {
                        "runs-on": "${{ matrix.os }}",
                        "strategy": {
                            "matrix": {
                                "os": ["windows-latest", "windows-2025"],
                                "include": [{"os": "windows-11-arm"}],
                            }
                        },
                        "steps": [],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert _unregistered_runs_on(workflow_dir) == []

    bad = workflow_dir / "bad-matrix.yml"
    bad.write_text(
        yaml.safe_dump(
            {
                "name": "bad-matrix",
                "on": "pull_request",
                "jobs": {
                    "matrix-job": {
                        "runs-on": "${{ matrix.os }}",
                        "strategy": {
                            "matrix": {
                                "os": ["windows-latest", "windows-2022"],
                            }
                        },
                        "steps": [],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    unregistered = _unregistered_runs_on(workflow_dir)
    assert len(unregistered) == 1
    assert unregistered[0][0] == "bad-matrix.yml"
    assert "windows-2022" in unregistered[0][2]
