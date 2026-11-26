"""Fail-closed runner selection for the dependency acquisition audit.

Container labels imply a pre-job image; host labels do not. publish-github
is org runner 22, docker://node:22-bookworm (infra-ops and e2e-verifier,
2026-10-26). release-ci is runners 25..30 with docker://node:22-bookworm,
confirmed by infra-ops on 2026-10-31 from registration and job startup logs.
Release routing is prescribed by PR #2137, not an exemption from image
accounting. Runner provenance and deployment config digest are recorded in
docs/notes/ci-dependency-caches-dev-red-2026-10-31.md.
ci-light is an instance-wide host-mode runner on node-hq-01 (label
`ci-light:host`, no per-job image; registered by infra-ops 2026-11-03 for
the skill-impact-class text gates).

Only scalar labels, singleton lists, the literal base-ref selector and static
``${{ matrix.<key> }}`` references (expanded from ``strategy.matrix`` including
``include`` entries) are supported. A multi-label list is a conjunction, not
alternative runners; it needs a reviewed execution-mode contract before this
audit can model it. The selector is not evaluated: both possible labels are
audited, without claiming that both images are fetched in one job execution.

GitHub-hosted virtual machines (2026-11-26): the public repository runs on
GitHub's own runners. A hosted label is a fresh virtual machine image that
GitHub maintains, not a container this repository pulls, so it adds no image
site to the acquisition audit. ``runner_labels`` accepts them.
``GITHUB_HOSTED_VM_LABELS`` is defined here once; the generic pull-request
runner check in ``github_gate_policy`` reads it from here, so the audit and
the check cannot disagree about which labels are hosted. The Windows labels
are ``windows-latest`` and ``windows-2025`` (both Windows Server 2025 x64)
and ``windows-11-arm`` (Windows 11 Enterprise ARM64); none of the hosted
labels is a container image or a runner registered by this repository.
"""

from itertools import product
import re
from typing import Any

CONTAINER_RUNNER_LABELS = frozenset({"ubuntu-latest", "publish-github", "release-ci"})
# release-linux-arm64 is the Linux host that runs the KP-027 arm64 jobs inside
# an explicit manylinux2014_aarch64 container (KP-027-A, runs 56181/56196/56118);
# release-macos-arm64 is the darwin/arm64 host (run 56118). Neither has a
# per-job default image, so both are host labels, not container labels.
HOST_RUNNER_LABELS = frozenset(
    {"macos-host", "ci-light", "release-linux-arm64", "release-macos-arm64"}
)
GITHUB_HOSTED_VM_LABELS = frozenset(
    {
        "windows-latest",
        "windows-2025",
        "windows-11-arm",
        "macos-latest",
        "macos-15",
        "macos-14",
        "macos-13",
        "ubuntu-24.04",
        "ubuntu-22.04",
    }
)
_BASE_REF_SELECTOR = re.compile(
    r"\$\{\{\s*startsWith\(\s*github\.base_ref\s*,\s*'release/'\s*\)"
    r"\s*&&\s*'([a-zA-Z0-9_-]+)'\s*\|\|\s*'([a-zA-Z0-9_-]+)'\s*\}\}"
)
_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


def matrix_runs_on_values(runs_on: object, job: dict[str, Any]) -> list[Any] | None:
    """Expand a single ``${{ matrix.<key> }}`` expression against ``strategy.matrix``.

    Returns the deduplicated values that ``matrix.<key>`` takes across all
    matrix combinations (after ``exclude`` and ``include``), or ``None`` when
    ``runs_on`` is not a matrix reference or cannot be statically resolved.
    """
    target = runs_on[0] if isinstance(runs_on, list) and len(runs_on) == 1 else runs_on
    if not isinstance(target, str):
        return None
    match = _MATRIX_REF.fullmatch(target.strip())
    if not match:
        return None
    key = match.group(1)
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict) or not matrix:
        return None
    axes = {k: v for k, v in matrix.items() if k not in {"include", "exclude"}}
    if any(not isinstance(v, list) or not v for v in axes.values()):
        return None
    rows: list[dict[str, Any]] = (
        [dict(zip(axes, combo)) for combo in product(*axes.values())]
        if axes
        else []
    )
    excludes = matrix.get("exclude", [])
    if not isinstance(excludes, list) or any(not isinstance(e, dict) for e in excludes):
        return None
    rows = [
        row
        for row in rows
        if not any(all(row.get(k) == v for k, v in entry.items()) for entry in excludes)
    ]
    includes = matrix.get("include", [])
    if not isinstance(includes, list) or any(not isinstance(i, dict) for i in includes):
        return None
    for entry in includes:
        matching = [
            row
            for row in rows
            if all(row.get(k) == v for k, v in entry.items() if k in axes)
        ]
        if matching:
            for row in matching:
                row.update(entry)
        else:
            rows.append(dict(entry))
    if not rows or any(key not in row for row in rows):
        return None
    values: list[Any] = []
    for row in rows:
        value = row[key]
        if value not in values:
            values.append(value)
    return values


def runner_labels(value: object) -> tuple[str, ...]:
    """Return every possible registered label, or reject the whole selector."""
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str):
        raise ValueError(f"unsupported runs-on selection: {value!r}")
    match = _BASE_REF_SELECTOR.fullmatch(value)
    labels = tuple(dict.fromkeys(match.groups())) if match else (value,)
    unknown = (
        set(labels)
        - CONTAINER_RUNNER_LABELS
        - HOST_RUNNER_LABELS
        - GITHUB_HOSTED_VM_LABELS
    )
    if unknown:
        raise ValueError(f"unregistered or unresolved runs-on selection: {value!r}")
    return labels


def job_runner_labels(job: dict[str, Any]) -> tuple[str, ...]:
    """Return every possible registered label for a job, expanding static matrix refs."""
    runs_on = job["runs-on"]
    expanded = matrix_runs_on_values(runs_on, job)
    if expanded is None:
        return runner_labels(runs_on)
    labels: list[str] = []
    for value in expanded:
        labels.extend(runner_labels(value))
    return tuple(dict.fromkeys(labels))
