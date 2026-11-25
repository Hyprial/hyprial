"""PR #2137 (37c290c66) image inventory delta; do not rewrite the baseline.

Three publishers left ubuntu-latest for release-ci. The six tests jobs
(nine matrix-expanded callers) retain their ubuntu fallback and gain a
release-ci alternative. This delta is independent of the release-changelog
job introduced on release/v0.5.5.3 and of the desktop-check retirement.

2026-11-03 (Allen): the four skill-impact-class text gates left the shared
ubuntu-latest pool for the ci-light HOST runner (no pre-job image), and
pr-hygiene kept only its release-ci arm on a container pool.

2026-11-14: gate-python.yml's pytest-shard matrix gained the ``isolated``
entry, which runs tests/integration/teleport alone because those modules
pushed a shard past the runner memory limit. It is one more matrix-expanded
caller of the same job on both pools: a new context, not a renamed shard.

2026-11-15: native-conformance.yml gained a ``plan`` job in PR #2821
(b55e09b14) on the release-ci pool -- one more implicit-image context on
the same pool as the W2 gate jobs below.
"""

_RELEASE_PUBLISHERS = {
    (".github/workflows/desktop-release.yml", "publish-reviewed-legacy"),
    (".github/workflows/release-tag.yml", "tag"),
    (".github/workflows/wheel-release.yml", "build-publish"),
}
_RELEASE_TEST_CALLERS = {
    (".github/workflows/tests.yml", job)
    for job in (
        "cli-help-inventory", "tailcat", "pr-hygiene", "checks", "unit",
        *(f"pytest-shard[shard={index}]" for index in range(1, 5)),
    )
}
RETIRED_RELEASE_RUNNER_IMAGE_CONTEXTS = {
    (*caller, "runner-image ubuntu-latest") for caller in _RELEASE_PUBLISHERS
}
INTRODUCED_RELEASE_RUNNER_IMAGE_CONTEXTS = {
    (*caller, "runner-image release-ci")
    for caller in _RELEASE_PUBLISHERS | _RELEASE_TEST_CALLERS
}

_CI_LIGHT_MOVE_JOBS = {
    (".github/workflows/contract-impact.yml", "impact"),
    (".github/workflows/skill-declaration.yml", "declaration"),
    (".github/workflows/release-changelog.yml", "changelog"),
}
RETIRED_CI_LIGHT_MOVE_IMAGE_CONTEXTS = {
    (*caller, "runner-image ubuntu-latest")
    for caller in _CI_LIGHT_MOVE_JOBS | {(".github/workflows/tests.yml", "pr-hygiene")}
}
INTRODUCED_CI_LIGHT_MOVE_IMAGE_CONTEXTS = {
    (".github/workflows/tests.yml", "pr-hygiene", "runner-image release-ci"),
}

# 2026-11-06: desktop-release.yml retired -- every tool it ran (app/scripts/*.cjs,
# app/release/publish-github.py, r15-manifest.json) left with the app tree in
# d9b953805 (2026-10-27). Its only image context was the release-ci arm above;
# the macos-host build-macos job never had one.
RETIRED_DESKTOP_RELEASE_IMAGE_CONTEXTS = {
    (".github/workflows/desktop-release.yml", "publish-reviewed-legacy", "runner-image release-ci"),
}
assert RETIRED_DESKTOP_RELEASE_IMAGE_CONTEXTS <= INTRODUCED_RELEASE_RUNNER_IMAGE_CONTEXTS

# 2026-11-05: cancel-on-pr-close.yml is a new pull_request[closed] workflow whose
# one job runs on the shared pool -- a new context, not a renamed job.
INTRODUCED_CANCEL_ON_PR_CLOSE_IMAGE_CONTEXTS = {
    (".github/workflows/cancel-on-pr-close.yml", "cancel", "runner-image ubuntu-latest"),
}

# 2026-11-06: hosted-agent-e2e.yml renamed to mock-model-harness-e2e.yml (the
# name now says what it runs: a mock model against the real harnesses). Same
# single `hosted` job on the same pool -- a path change, so the old context is
# retired and the new path introduced, never rewritten in the frozen baseline.
RETIRED_HOSTED_RENAME_IMAGE_CONTEXTS = {
    (".github/workflows/hosted-agent-e2e.yml", "hosted", "runner-image ubuntu-latest"),
}
INTRODUCED_HOSTED_RENAME_IMAGE_CONTEXTS = {
    (".github/workflows/mock-model-harness-e2e.yml", "hosted", "runner-image ubuntu-latest"),
}

# 2026-11-06: workflow file names say what they gate (gate-* merge gates, e2e-*,
# release-*, ops-*). tests.yml split into gate-python.yml (its `tailcat` job is
# now `tailcat-go`) and gate-pr-hygiene.yml; the three body/diff declaration
# gates merged into gate-pr-declarations.yml (ci-light host: no image context).
# Every renamed path retires its old context and introduces the new one; the
# frozen baseline is never rewritten. wheel-release.yml, github-publish.yml and
# homepage-changelog-sync.yml keep their names: other workflows dispatch them by
# file name against track tags that predate any rename.
RETIRED_GATE_RENAME_IMAGE_CONTEXTS = {
    (".github/workflows/cancel-on-pr-close.yml", "cancel", "runner-image ubuntu-latest"),
    (".github/workflows/gui.yml", "gui-unit", "runner-image ubuntu-latest"),
    (".github/workflows/gui.yml", "release-package", "runner-image ubuntu-latest"),
    (".github/workflows/install-scripts.yml", "drift", "runner-image ubuntu-latest"),
    (".github/workflows/install-scripts.yml", "publish", "runner-image ubuntu-latest"),
    (".github/workflows/main-release.yml", "dispatch", "runner-image ubuntu-latest"),
    (".github/workflows/nightly-promote.yml", "promote", "runner-image ubuntu-latest"),
    (".github/workflows/tests.yml", "checks", "runner-image release-ci"),
    (".github/workflows/tests.yml", "checks", "runner-image ubuntu-latest"),
    (".github/workflows/tests.yml", "cli-help-inventory", "runner-image release-ci"),
    (".github/workflows/tests.yml", "cli-help-inventory", "runner-image ubuntu-latest"),
    (".github/workflows/tests.yml", "pr-hygiene", "runner-image release-ci"),
    (".github/workflows/tests.yml", "pytest-shard[shard=1]", "runner-image release-ci"),
    (".github/workflows/tests.yml", "pytest-shard[shard=1]", "runner-image ubuntu-latest"),
    (".github/workflows/tests.yml", "pytest-shard[shard=2]", "runner-image release-ci"),
    (".github/workflows/tests.yml", "pytest-shard[shard=2]", "runner-image ubuntu-latest"),
    (".github/workflows/tests.yml", "pytest-shard[shard=3]", "runner-image release-ci"),
    (".github/workflows/tests.yml", "pytest-shard[shard=3]", "runner-image ubuntu-latest"),
    (".github/workflows/tests.yml", "pytest-shard[shard=4]", "runner-image release-ci"),
    (".github/workflows/tests.yml", "pytest-shard[shard=4]", "runner-image ubuntu-latest"),
    (".github/workflows/tests.yml", "tailcat", "runner-image release-ci"),
    (".github/workflows/tests.yml", "tailcat", "runner-image ubuntu-latest"),
    (".github/workflows/tests.yml", "unit", "runner-image release-ci"),
    (".github/workflows/tests.yml", "unit", "runner-image ubuntu-latest"),
}
INTRODUCED_GATE_RENAME_IMAGE_CONTEXTS = {
    (".github/workflows/ops-cancel-on-pr-close.yml", "cancel", "runner-image ubuntu-latest"),
    (".github/workflows/gate-gui.yml", "gui-unit", "runner-image ubuntu-latest"),
    (".github/workflows/gate-gui.yml", "release-package", "runner-image ubuntu-latest"),
    (".github/workflows/release-install-scripts.yml", "drift", "runner-image ubuntu-latest"),
    (".github/workflows/release-install-scripts.yml", "publish", "runner-image ubuntu-latest"),
    (".github/workflows/release-main-dispatch.yml", "dispatch", "runner-image ubuntu-latest"),
    (".github/workflows/release-nightly-promote.yml", "promote", "runner-image ubuntu-latest"),
    (".github/workflows/gate-python.yml", "checks", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "checks", "runner-image ubuntu-latest"),
    (".github/workflows/gate-python.yml", "cli-help-inventory", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "cli-help-inventory", "runner-image ubuntu-latest"),
    (".github/workflows/gate-pr-hygiene.yml", "pr-hygiene", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "pytest-shard[shard=1]", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "pytest-shard[shard=1]", "runner-image ubuntu-latest"),
    (".github/workflows/gate-python.yml", "pytest-shard[shard=2]", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "pytest-shard[shard=2]", "runner-image ubuntu-latest"),
    (".github/workflows/gate-python.yml", "pytest-shard[shard=3]", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "pytest-shard[shard=3]", "runner-image ubuntu-latest"),
    (".github/workflows/gate-python.yml", "pytest-shard[shard=4]", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "pytest-shard[shard=4]", "runner-image ubuntu-latest"),
    (".github/workflows/gate-python.yml", "tailcat-go", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "tailcat-go", "runner-image ubuntu-latest"),
    (".github/workflows/gate-python.yml", "unit", "runner-image release-ci"),
    (".github/workflows/gate-python.yml", "unit", "runner-image ubuntu-latest"),
}

# KP-027 W2 native conformance gate (2026-11-14). Three jobs use the release-ci
# pool's implicit node:22-bookworm image after P8 removed the pykka job; the
# arm64 job declares an explicit manylinux2014_aarch64 container and the darwin
# job runs on a host runner, so neither adds an implicit runner image.
INTRODUCED_NATIVE_CONFORMANCE_IMAGE_CONTEXTS = {
    (".github/workflows/native-conformance.yml", "plan", "runner-image release-ci"),
    (".github/workflows/native-conformance.yml", "reference-abi", "runner-image release-ci"),
    (".github/workflows/native-conformance.yml", "native-linux-amd64", "runner-image release-ci"),
    (".github/workflows/native-conformance.yml", "free-threaded-3-15t", "runner-image release-ci"),
}

# KP-027 five-platform candidate pipeline (2026-11-14). Five jobs take an
# implicit runner image; the two arm64 jobs and the darwin job run on host
# runners and declare their own containers, so they add no implicit image.
INTRODUCED_KP027_CANDIDATE_IMAGE_CONTEXTS = {
    (".github/workflows/kp027-native-candidate.yml", "pin", "runner-image release-ci"),
    (".github/workflows/kp027-native-candidate.yml", "native-linux-amd64", "runner-image ubuntu-latest"),
    (".github/workflows/kp027-native-candidate.yml", "wheel-linux-amd64", "runner-image release-ci"),
    (".github/workflows/kp027-native-candidate.yml", "wheel-linux-arm64", "runner-image release-ci"),
    (".github/workflows/kp027-native-candidate.yml", "assemble", "runner-image release-ci"),
}

INTRODUCED_ISOLATED_LANE_IMAGE_CONTEXTS = {
    (".github/workflows/gate-python.yml", "pytest-shard[shard=isolated]", pool)
    for pool in ("runner-image release-ci", "runner-image ubuntu-latest")
}

INTRODUCED_WIP_PLAN_IMAGE_CONTEXTS = {
    (".github/workflows/mock-model-harness-e2e.yml", "plan", "runner-image ubuntu-latest"),
}

INTRODUCED_NATIVE_SHELL_IMAGE_CONTEXTS = {
    (".github/workflows/gate-native-shell.yml", "launcher", "runner-image ubuntu-latest"),
}

INTRODUCED_GUI_ACCEPTANCE_IMAGE_CONTEXTS = {
    (".github/workflows/gate-gui.yml", "gui-acceptance", "runner-image ubuntu-latest"),
}
