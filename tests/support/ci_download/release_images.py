"""PR #2137 (37c290c66) image inventory delta; do not rewrite the baseline.

Three publishers left ubuntu-latest for release-ci. The six tests jobs
(nine matrix-expanded callers) retain their ubuntu fallback and gain a
release-ci alternative. This delta is independent of the release-changelog
job introduced on release/v0.5.5.3 and of the desktop-check retirement.

2026-11-03 (Allen): the four skill-impact-class text gates left the shared
ubuntu-latest pool for the ci-light HOST runner (no pre-job image), and
pr-hygiene kept only its release-ci arm on a container pool.
"""

_RELEASE_PUBLISHERS = {
    (".forgejo/workflows/desktop-release.yml", "publish-reviewed-legacy"),
    (".forgejo/workflows/release-tag.yml", "tag"),
    (".forgejo/workflows/wheel-release.yml", "build-publish"),
}
_RELEASE_TEST_CALLERS = {
    (".forgejo/workflows/tests.yml", job)
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
    (".forgejo/workflows/contract-impact.yml", "impact"),
    (".forgejo/workflows/skill-declaration.yml", "declaration"),
    (".forgejo/workflows/release-changelog.yml", "changelog"),
}
RETIRED_CI_LIGHT_MOVE_IMAGE_CONTEXTS = {
    (*caller, "runner-image ubuntu-latest")
    for caller in _CI_LIGHT_MOVE_JOBS | {(".forgejo/workflows/tests.yml", "pr-hygiene")}
}
INTRODUCED_CI_LIGHT_MOVE_IMAGE_CONTEXTS = {
    (".forgejo/workflows/tests.yml", "pr-hygiene", "runner-image release-ci"),
}

# 2026-11-06: desktop-release.yml retired -- every tool it ran (app/scripts/*.cjs,
# app/release/publish-github.py, r15-manifest.json) left with the app tree in
# d9b953805 (2026-10-27). Its only image context was the release-ci arm above;
# the macos-host build-macos job never had one.
RETIRED_DESKTOP_RELEASE_IMAGE_CONTEXTS = {
    (".forgejo/workflows/desktop-release.yml", "publish-reviewed-legacy", "runner-image release-ci"),
}
assert RETIRED_DESKTOP_RELEASE_IMAGE_CONTEXTS <= INTRODUCED_RELEASE_RUNNER_IMAGE_CONTEXTS

# 2026-11-05: cancel-on-pr-close.yml is a new pull_request[closed] workflow whose
# one job runs on the shared pool -- a new context, not a renamed job.
INTRODUCED_CANCEL_ON_PR_CLOSE_IMAGE_CONTEXTS = {
    (".forgejo/workflows/cancel-on-pr-close.yml", "cancel", "runner-image ubuntu-latest"),
}

# 2026-11-06: hosted-agent-e2e.yml renamed to mock-model-harness-e2e.yml (the
# name now says what it runs: a mock model against the real harnesses). Same
# single `hosted` job on the same pool -- a path change, so the old context is
# retired and the new path introduced, never rewritten in the frozen baseline.
RETIRED_HOSTED_RENAME_IMAGE_CONTEXTS = {
    (".forgejo/workflows/hosted-agent-e2e.yml", "hosted", "runner-image ubuntu-latest"),
}
INTRODUCED_HOSTED_RENAME_IMAGE_CONTEXTS = {
    (".forgejo/workflows/mock-model-harness-e2e.yml", "hosted", "runner-image ubuntu-latest"),
}
