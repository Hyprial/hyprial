"""Runner selection must not hide pre-job image acquisitions.

PR #2137 introduced singleton lists and a release/base-ref selector. Each
possible container label needs its own image site; unknown syntax or labels
must fail closed. These fixtures do not contact or provision real runners.
"""

from pathlib import Path

import pytest
import yaml

from tests.support.ci import _ci_download_scanner as scanner

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


def test_every_runs_on_label_is_registered():
    """The registration gate and scanner use the same fail-closed reader."""
    root = Path(__file__).resolve().parents[4]
    unregistered = []
    for path in sorted((root / ".github/workflows").glob("*.y*ml")):
        for name, job in (yaml.safe_load(path.read_text()).get("jobs") or {}).items():
            if not isinstance(job, dict) or "runs-on" not in job:
                continue
            try:
                scanner.runner_labels(job["runs-on"])
            except ValueError as exc:
                unregistered.append((path.name, name, str(exc)))
    assert not unregistered, unregistered
