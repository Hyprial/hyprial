"""Credential and source hygiene of every workflow in ``.github/workflows``.

These are directory scans over the workflow files that exist in the tree, so
they follow the surviving gate (``gate.yml``) instead of the Forgejo-era
workflows that first motivated them (spec-ci-internal-origin-2026-09-15,
#531).  The invariants kept here do not depend on that topology:

- no workflow reaches the retired public forge host over an ``http(s)`` URL;
- no remote URL carries a token in its userinfo;
- a step that authenticates git (askpass / ``extraHeader`` / ``GIT_CONFIG_VALUE``)
  carries its own step-level ``GITHUB_TOKEN`` env, because those read the
  variable at invocation time -- an env on an earlier step is an empty token
  on this one;
- an ``INSPECTION_ONLY_STEPS`` exemption (a step that proves a credential is
  absent, so it must not be handed one) never runs git itself;
- every ``uses:`` names a pinned ref.

The internal-origin routing of the retired Forgejo jobs (container versus
macos-host remotes, the step-scoped PR-hygiene fetch token, the runner token
probe) was tested against those workflow files and retired with them.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.support.ci.ci_workflow_policy import unpinned_reason, unpinned_uses_offenders, unpinned_uses_offenders_in

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))

PUBLIC_HOST = "code.hyprial.com"

# User-visible links (echoes of download URLs etc.) may still name the public
# host: they are text for humans, not access paths. Each entry must name its
# file and the exact URL substring; an entry that stops occurring is stale
# and fails test_whitelist_entries_are_live below. Empty today on purpose:
# every current access was converted, so a nonempty whitelist is the
# exception that must argue for itself.
USER_VISIBLE_LINK_WHITELIST: tuple[tuple[str, str], ...] = ()

_SCHEME_URL_TO_PUBLIC_HOST = re.compile(r"https?://[^\s\"']*code\.hyprial\.com")
# URL userinfo, e.g. https://x-access-token:TOKEN@host/... -- the askpass
# scripts legitimately PRINT the bare word as the username answer, so the
# token-in-URL shape is what must be absent, not the string itself.
_TOKEN_IN_URL = re.compile(r"x-access-token:[^\s\"']*@")

# Steps that only INSPECT the credential names (checkers, not authenticators).
# Keyed ``<job>/<step name>``; every entry must argue for itself, and
# ``test_inspection_only_exemptions_never_run_git`` keeps an exempted step from
# becoming a place an authenticating command hides: a checker that has to PROVE
# a credential absent cannot be handed that credential to satisfy the heuristic.
INSPECTION_ONLY_STEPS: dict[str, str] = {}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load(path: Path) -> dict:
    data = yaml.safe_load(_text(path))
    assert isinstance(data, dict) and "jobs" in data, f"{path.name}: no jobs"
    return data


def _iter_steps():
    """Yield (workflow, job_name, runs_on, step) for every step in every job."""

    for workflow in WORKFLOWS:
        jobs = _load(workflow)["jobs"]
        for job_name, job in jobs.items():
            runs_on = job.get("runs-on", "")
            for step in job.get("steps", []):
                yield workflow, job_name, runs_on, step


def _is_host_job(runs_on) -> bool:
    if isinstance(runs_on, list):
        return any("macos" in str(label) for label in runs_on)
    return "macos" in str(runs_on)


def test_no_workflow_reaches_the_public_host_over_http() -> None:
    """Any scheme-URL to code.hyprial.com in a workflow is a tunnel trip."""

    offenders: list[str] = []
    for workflow in WORKFLOWS:
        for lineno, line in enumerate(_text(workflow).splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for match in _SCHEME_URL_TO_PUBLIC_HOST.finditer(line):
                if (workflow.name, match.group(0)) in USER_VISIBLE_LINK_WHITELIST:
                    continue
                offenders.append(f"{workflow.name}:{lineno}: {match.group(0)}")
    assert offenders == [], (
        "workflow git/API access still points at the Cloudflare tunnel host:\n"
        + "\n".join(offenders)
    )


def test_whitelist_entries_are_live() -> None:
    for filename, url in USER_VISIBLE_LINK_WHITELIST:
        path = WORKFLOW_DIR / filename
        assert path.exists() and url in _text(path), (
            f"whitelist entry {filename}:{url} no longer occurs; drop it"
        )


def test_no_run_token_in_any_remote_url() -> None:
    offenders = [
        f"{workflow.name}:{lineno}"
        for workflow in WORKFLOWS
        for lineno, line in enumerate(_text(workflow).splitlines(), 1)
        if _TOKEN_IN_URL.search(line)
    ]
    assert offenders == [], (
        "run token embedded in a remote URL (echoed on any git error):\n"
        + "\n".join(offenders)
    )


def test_host_jobs_never_use_the_container_origin() -> None:
    """${GITHUB_SERVER_URL} is http://tailscale:3000/ here, and `tailscale`
    does not resolve on macos-host -- this is the exact provider-calibration
    failure shape, so it is guarded by runner kind, not by memory."""

    bad: list[str] = []
    for workflow, job_name, runs_on, step in _iter_steps():
        if not _is_host_job(runs_on):
            continue
        script = step.get("run") or ""
        for line in script.splitlines():
            if line.strip().startswith("#"):
                continue
            if "GITHUB_SERVER_URL" in line:
                bad.append(f"{workflow.name}:{job_name}: {line.strip()}")
    assert bad == [], "host job uses the container origin:\n" + "\n".join(bad)


def test_every_authenticated_git_step_carries_its_own_token_env() -> None:
    """#531: askpass/extraHeader read GITHUB_TOKEN at invocation time; an env
    on an earlier step is an empty token on this one."""

    missing: list[str] = []
    exempted: list[str] = []
    for workflow, job_name, _runs_on, step in _iter_steps():
        script = step.get("run") or ""
        if not any(
            marker in script
            for marker in ("extraHeader", "GIT_ASKPASS", "GIT_CONFIG_VALUE")
        ):
            continue
        key = f"{job_name}/{step.get('name', '?')}"
        if key in INSPECTION_ONLY_STEPS:
            exempted.append(key)
            continue
        env = step.get("env") or {}
        if "GITHUB_TOKEN" not in env:
            missing.append(f"{workflow.name}:{job_name}:{step.get('name', '?')}")
    assert sorted(exempted) == sorted(INSPECTION_ONLY_STEPS), (
        "inspection-only exemptions did not all match a real step; stale: "
        f"{sorted(set(INSPECTION_ONLY_STEPS) - set(exempted))}"
    )
    assert missing == [], (
        "step authenticates git but has no step-level GITHUB_TOKEN env:\n"
        + "\n".join(missing)
    )

# A git command, not the substring: the askpass file is legitimately named
# ``hyprial-git-askpass-<run>`` ("git" between dashes).  HEURISTIC, not a proof:
# the widened leading class covers quotes, backticks and ``$(`` so the common
# wrappers (`sh -c 'git fetch …'`, `eval "git fetch"`, `$(git fetch …)`) are
# caught, but a constructed command can still slip through -- so the exemption
# list below must stay small and reviewed by name.
GIT_COMMAND = re.compile(r"""(?:^|[;&|()\s"'`]|\$\()(?:sudo\s+|env\s+)?git\s+\w""")


def test_inspection_only_exemptions_never_run_git() -> None:
    """An exempted step may inspect credential names, never use them."""

    for workflow, job_name, _runs_on, step in _iter_steps():
        if f"{job_name}/{step.get('name', '?')}" not in INSPECTION_ONLY_STEPS:
            continue
        script = step.get("run") or ""
        for line in script.splitlines():
            command = line.strip()
            if not command or command.startswith("#"):
                continue
            assert not GIT_COMMAND.search(command), (
                f"{workflow.name}:{job_name}:{step.get('name')} is exempt from "
                f"carrying a token but runs git: {command}"
            )


def test_git_command_heuristic_catches_the_common_wrappers() -> None:
    """The widened class, exercised on the shapes that used to slip through."""

    for script in (
        "git fetch origin",
        "sh -c 'git fetch origin'",
        'eval "git fetch origin"',
        "T=$(git fetch origin)",
        "`git fetch origin`",
    ):
        assert GIT_COMMAND.search(script), script
    # The askpass filename must NOT be read as a command.
    assert not GIT_COMMAND.search("cat hyprial-git-askpass-1")


def test_container_jobs_with_a_proxy_bypass_the_internal_origin() -> None:
    """git honours HTTP(S)_PROXY for http:// remotes; without a NO_PROXY entry
    the internal fetch would be handed to the host proxy, which resolves
    `tailscale` to something that is not the instance (measured: fast 502)."""

    missing: list[str] = []
    for workflow in WORKFLOWS:
        data = _load(workflow)
        workflow_env = data.get("env") or {}
        for job_name, job in data["jobs"].items():
            if _is_host_job(job.get("runs-on", "")):
                continue
            env = {**workflow_env, **(job.get("env") or {})}
            proxied = any(
                str(env.get(key, "")).strip()
                for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
            )
            if not proxied:
                continue
            no_proxy = str(env.get("NO_PROXY", "") or env.get("no_proxy", ""))
            if "tailscale" not in no_proxy.split(","):
                missing.append(f"{workflow.name}:{job_name}")
    assert missing == [], (
        "container job sets a proxy but does not NO_PROXY the internal origin:\n"
        + "\n".join(missing)
    )


def test_every_uses_directive_pins_its_version() -> None:
    """Allen 2026-09-21: `uses:` is allowed -- but the ref must be pinned.

    The old rule here was a blanket ban, justified as: "checkouts are
    hand-rolled; `uses:` stays at zero so no runner-preinstalled action is ever
    implied".  The property the ruling keeps is *the source is named and is not
    a moving branch*; it survives only while something checks the pin, so the
    check is a test and the prose in the workflows says "must be pinned" rather
    than "forbidden".  A floating ref (`@main` / `@HEAD` / `@latest` / no `@`)
    means "whatever upstream or the runner happens to have today".
    """

    offenders = unpinned_uses_offenders()
    assert offenders == [], "unpinned `uses:` refs in the tree:\n" + "\n".join(
        offenders
    )

# A known-good workflow, so the controls do not depend on the tree's state.
_PINNED_CONTROL = (
    "name: control\n"
    "jobs:\n"
    "  sample:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - name: fetch from the default source\n"
    "        uses: actions/upload-artifact@v3\n"
)


def test_pinned_uses_positive_control_passes() -> None:
    """⛔ 否则那就是"禁 uses"换了个名字."""

    assert unpinned_uses_offenders_in(_PINNED_CONTROL) == []
    # A full commit SHA is the strongest legal form and must also pass.
    sha = "actions/upload-artifact@" + "a" * 40
    assert unpinned_reason(sha) is None
    assert unpinned_reason("owner/repo@v3.1.2") is None


def test_floating_uses_mutation_is_rejected() -> None:
    """The destructive half: each floating form must go red."""

    for ref in ("main", "HEAD", "latest", "v3branch"):
        mutated = _PINNED_CONTROL.replace("@v3", f"@{ref}")
        assert mutated != _PINNED_CONTROL
        assert unpinned_uses_offenders_in(mutated) != [], ref
    no_ref = _PINNED_CONTROL.replace("@v3", "")
    assert unpinned_uses_offenders_in(no_ref) != []


def test_the_positive_control_is_not_weakened() -> None:
    """A weakened positive control and a real one both go green.

    So the control is checked by breaking the property it is supposed to be
    sensitive to: the SAME text with a floating ref must be rejected.  Asking
    only "does the positive control pass" cannot tell the two apart.
    """

    assert unpinned_uses_offenders_in(_PINNED_CONTROL) == []
    broken = _PINNED_CONTROL.replace("uses: actions/upload-artifact@v3", "uses: actions/upload-artifact@main")
    assert broken != _PINNED_CONTROL, "the mutation must apply, or this proves nothing"
    assert unpinned_uses_offenders_in(broken) != []
