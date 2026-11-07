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

Only scalar labels, singleton lists and the literal base-ref selector are
supported. A multi-label list is a conjunction, not alternative runners;
it needs a reviewed execution-mode contract before this audit can model it.
The selector is not evaluated: both possible labels are audited, without
claiming that both images are fetched in one job execution.
"""

import re

CONTAINER_RUNNER_LABELS = frozenset({"ubuntu-latest", "publish-github", "release-ci"})
HOST_RUNNER_LABELS = frozenset({"macos-host", "ci-light"})
_BASE_REF_SELECTOR = re.compile(
    r"\$\{\{\s*startsWith\(\s*github\.base_ref\s*,\s*'release/'\s*\)"
    r"\s*&&\s*'([a-zA-Z0-9_-]+)'\s*\|\|\s*'([a-zA-Z0-9_-]+)'\s*\}\}"
)


def runner_labels(value: object) -> tuple[str, ...]:
    """Return every possible registered label, or reject the whole selector."""
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str):
        raise ValueError(f"unsupported runs-on selection: {value!r}")
    match = _BASE_REF_SELECTOR.fullmatch(value)
    labels = tuple(dict.fromkeys(match.groups())) if match else (value,)
    unknown = set(labels) - CONTAINER_RUNNER_LABELS - HOST_RUNNER_LABELS
    if unknown:
        raise ValueError(f"unregistered or unresolved runs-on selection: {value!r}")
    return labels
