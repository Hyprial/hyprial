"""Rules 10–17 for required checks, selector wiring, and nightly full runs.

This module imports common workflow readers from github_gate_policy.py in one
direction.  Keeping these rules together prevents required-check ownership,
selector behavior, and the scheduled backstop from drifting apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from tests.support.ci_download.github_gate_policy import (
    Finding,
    GATE_FILE,
    PR_HYGIENE_FILE,
    REQUIRED_CHECK_OWNERS,
    STRICT_FILES,
    as_list,
    is_instrumentation,
    jobs_of,
    load,
    trigger_names,
    triggers,
    workflow_files,
)

GATE_PR_TYPES = frozenset({"opened", "synchronize", "reopened"})
PR_HYGIENE_PR_TYPES = GATE_PR_TYPES | {"edited"}
BACKSTOP_BRANCHES = frozenset({"dev", "main"})
TRIGGER_FILTERS = ("paths", "paths-ignore", "branches-ignore")
NIGHTLY_TRIGGER_FILTERS = ("paths", "paths-ignore", "branches", "branches-ignore", "tags", "tags-ignore")
PR_ONLY_CANCEL = "${{ github.event_name == 'pull_request' }}"
REQUIRED_CONCURRENCY_GROUPS = {
    GATE_FILE: "gate-${{ github.event.pull_request.number || github.ref }}",
    PR_HYGIENE_FILE: "pr-hygiene-${{ github.event.pull_request.number }}",
}
JOB_TOKEN_SCOPES = frozenset({"contents", "pull-requests"})
STRICT_PREAMBLE = "set -euo pipefail"
RUN_TOKEN = "${{ github.token }}"
LIVE_PULL_REQUEST_API = "repos/${GITHUB_REPOSITORY}/pulls/"


def check_key(text: Any) -> str:
    """Return a required-check id or display name in canonical form."""

    return str(text or "").strip().casefold()


def reserved_names_of(job_name: str, job: dict[str, Any]) -> set[str]:
    """Return required check names claimed by a job id or display name."""

    claimed = {check_key(job_name), check_key(job.get("name"))}
    return {check for check in REQUIRED_CHECK_OWNERS if check in claimed}


def foreign_required_check_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    """Report a required check claimed outside its owning workflow."""

    found: list[Finding] = []
    for job_name, job in jobs_of(document).items():
        for check in sorted(reserved_names_of(job_name, job)):
            owner = REQUIRED_CHECK_OWNERS[check]
            if name != owner:
                found.append(Finding(name, 0, f"job {job_name!r} uses the reserved check name {check!r}: only {owner} may define that required job"))
    return found


def _condition(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _shell_overrides(job: dict[str, Any], document: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for where, block in (("workflow", document), ("job", job)):
        run_defaults = ((block.get("defaults") or {}).get("run") or {})
        if "shell" in run_defaults:
            found.append(f"{where} defaults.run.shell")
    return found


def owned_job_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    """Report the shape of a required job in its owning workflow."""

    found: list[Finding] = []
    jobs = jobs_of(document)
    if name == PR_HYGIENE_FILE and len(jobs) != 1:
        found.append(Finding(name, 0, f"defines {len(jobs)} jobs {sorted(jobs)}, not exactly one job"))
    for check, owner in REQUIRED_CHECK_OWNERS.items():
        if name != owner:
            continue
        matching = [job_name for job_name in jobs if check_key(job_name) == check]
        if len(matching) != 1:
            found.append(Finding(name, 0, f"defines {len(matching)} jobs with id {check!r}, not exactly one"))
            continue
        job_name = matching[0]
        job = jobs[job_name]
        if "name" in job:
            found.append(Finding(name, 0, f"job {job_name} overrides its check name with name: {job['name']!r}"))
        if "strategy" in job:
            found.append(Finding(name, 0, f"job {job_name} has a strategy: a matrix renames the required check"))
        expected_if = "always()" if check == "gate" else ""
        if _condition(job.get("if")) != expected_if:
            found.append(Finding(name, 0, f"job {job_name} if is {job.get('if')!r}, must be {expected_if or 'absent'!r}"))
        if check != "gate" and "needs" in job:
            found.append(Finding(name, 0, f"job {job_name} has needs: {job['needs']!r}; a skipped dependency skips it"))
        if "environment" in job:
            found.append(Finding(name, 0, f"job {job_name} has environment: {job['environment']!r}"))
        if "concurrency" in job:
            found.append(Finding(name, 0, f"job {job_name} has job-level concurrency"))
        for override in _shell_overrides(job, document):
            found.append(Finding(name, 0, f"job {job_name} runs under a {override} override"))
        steps = [step for step in job.get("steps") or [] if isinstance(step, dict)]
        if check == "gate" and len(steps) != 1:
            found.append(Finding(name, 0, f"job {job_name} has {len(steps)} steps, not exactly one"))
        if not any("run" in step for step in steps):
            found.append(Finding(name, 0, f"job {job_name} has no run step that decides its result"))
        for index, step in enumerate(steps):
            if "if" in step:
                found.append(Finding(name, 0, f"job {job_name} step[{index}] has a step-level if: {step['if']!r}"))
            if "run" not in step:
                continue
            if "shell" in step:
                found.append(Finding(name, 0, f"job {job_name} step[{index}] overrides the shell"))
            if not str(step["run"]).lstrip().startswith(STRICT_PREAMBLE):
                found.append(Finding(name, 0, f"job {job_name} step[{index}] does not start with {STRICT_PREAMBLE!r}"))
    return found


def trigger_config(document: dict[str, Any], trigger: str) -> dict[str, Any] | None:
    block = triggers(document)
    if not isinstance(block, dict):
        return None
    config = block.get(trigger)
    return config if isinstance(config, dict) else None


def trigger_values(document: dict[str, Any], trigger: str, key: str) -> set[str] | None:
    config = trigger_config(document, trigger)
    if config is None:
        return None
    return {str(item) for item in as_list(config.get(key))}


def trigger_filter_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    block = triggers(document)
    found: list[Finding] = []
    if not isinstance(block, dict):
        return [Finding(name, 0, f"top-level 'on' must be a mapping, got {block!r}")]
    for trigger, config in block.items():
        if not isinstance(config, dict):
            continue
        for key in TRIGGER_FILTERS:
            if key in config:
                found.append(Finding(name, 0, f"on.{trigger} defines {key!r}, which can leave a required check pending"))
    return found


def required_trigger_offenders(documents: dict[str, dict[str, Any]]) -> list[Finding]:
    found: list[Finding] = []
    gate = documents.get(GATE_FILE)
    hygiene = documents.get(PR_HYGIENE_FILE)
    for file_name, document in ((GATE_FILE, gate), (PR_HYGIENE_FILE, hygiene)):
        if document is None:
            found.append(Finding(file_name, 0, "the owning file of a required check is missing"))
        else:
            found.extend(trigger_filter_offenders(document, file_name))
    if gate is None or hygiene is None:
        return found
    gate_types = trigger_values(gate, "pull_request", "types")
    if gate_types != GATE_PR_TYPES:
        found.append(Finding(GATE_FILE, 0, f"on.pull_request.types is {sorted(gate_types or [])}, must be exactly {sorted(GATE_PR_TYPES)}"))
    push_branches = trigger_values(gate, "push", "branches") or set()
    if push_branches != BACKSTOP_BRANCHES:
        found.append(Finding(GATE_FILE, 0, f"on.push.branches is {sorted(push_branches)}, must be exactly {sorted(BACKSTOP_BRANCHES)}"))
    gate_branches = trigger_values(gate, "pull_request", "branches")
    if gate_branches != BACKSTOP_BRANCHES:
        found.append(Finding(GATE_FILE, 0, f"on.pull_request.branches is {sorted(gate_branches or [])}, must be {sorted(BACKSTOP_BRANCHES)}"))
    if trigger_names(hygiene) != {"pull_request"}:
        found.append(Finding(PR_HYGIENE_FILE, 0, f"triggers {sorted(trigger_names(hygiene))}, must be only pull_request"))
    hygiene_types = trigger_values(hygiene, "pull_request", "types")
    if hygiene_types != PR_HYGIENE_PR_TYPES:
        found.append(Finding(PR_HYGIENE_FILE, 0, f"on.pull_request.types is {sorted(hygiene_types or [])}, must be exactly {sorted(PR_HYGIENE_PR_TYPES)}"))
    hygiene_branches = trigger_values(hygiene, "pull_request", "branches")
    if hygiene_branches != gate_branches:
        found.append(Finding(PR_HYGIENE_FILE, 0, f"on.pull_request.branches {sorted(hygiene_branches or [])} differ from {GATE_FILE}'s {sorted(gate_branches or [])}"))
    return found


def concurrency_group(block: Any) -> str:
    return str(block.get("group") or "") if isinstance(block, dict) else str(block or "")


def concurrency_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    found: list[Finding] = []
    group = concurrency_group(document.get("concurrency"))
    expected = REQUIRED_CONCURRENCY_GROUPS.get(name)
    if expected is not None and group != expected:
        found.append(Finding(name, 0, f"concurrency group is {group!r}, must be exactly {expected!r}"))
    prefixes = {owner: f"{check}-" for check, owner in REQUIRED_CHECK_OWNERS.items()}
    groups = ([] if expected is not None else [("workflow", group.casefold())]) + [(f"job {job_name}", concurrency_group(job["concurrency"]).casefold()) for job_name, job in jobs_of(document).items() if "concurrency" in job]
    for where, value in groups:
        for owner, prefix in prefixes.items():
            if value.startswith(prefix):
                found.append(Finding(name, 0, f"{where} concurrency group {value!r} uses {owner}'s prefix {prefix!r}"))
    return found


def cancel_policy_offenders(documents: dict[str, dict[str, Any]]) -> list[Finding]:
    gate = documents.get(GATE_FILE) or {}
    block = gate.get("concurrency")
    cancel = block.get("cancel-in-progress") if isinstance(block, dict) else None
    return [] if cancel == PR_ONLY_CANCEL else [Finding(GATE_FILE, 0, f"concurrency.cancel-in-progress is {cancel!r}, must be {PR_ONLY_CANCEL!r}")]


def job_permission_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    found: list[Finding] = []
    for job_name, job in jobs_of(document).items():
        if "permissions" not in job:
            continue
        declared = job["permissions"]
        if not isinstance(declared, dict):
            found.append(Finding(name, 0, f"job {job_name} permissions {declared!r} is not a scope mapping"))
            continue
        for scope, level in declared.items():
            if scope not in JOB_TOKEN_SCOPES or level != "read":
                found.append(Finding(name, 0, f"job {job_name} permissions grants {scope}: {level}; only contents and pull-requests may be read"))
    return found


_EVENT_KEY = r"(?:\bevent|\[\s*['\"]event['\"]\s*\])"
_EVENT_BODY = re.compile(_EVENT_KEY + r"\s*(?:\.\s*pull_request|\[\s*['\"]pull_request['\"]\s*\])\s*(?:\.\s*body\b|\[\s*['\"]body['\"]\s*\])|toJSON\(\s*github\s*(?:\.\s*event|\[\s*['\"]event['\"]\s*\])?\s*(?:\.\s*pull_request|\[\s*['\"]pull_request['\"]\s*\])?\s*\)", re.IGNORECASE)
_ALLOWED_EVENT_READ = re.compile(r"github\s*\.\s*event\s*\.\s*pull_request\s*\.\s*number\b", re.IGNORECASE)
_HYGIENE_EVENT_READS = (re.compile(r"github\s*\.\s*event", re.IGNORECASE), re.compile(r"\bgithub\s*\[", re.IGNORECASE), re.compile(r"toJSON\(\s*github", re.IGNORECASE), re.compile(r"GITHUB_EVENT_PATH", re.IGNORECASE))


def strings_of(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from strings_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_of(item)
    elif isinstance(value, str):
        yield value


def event_body_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    return [Finding(name, 0, f"reads the pull request body from the event payload: {match.group(0)!r}") for text in strings_of(document) for match in _EVENT_BODY.finditer(text)]


def hygiene_event_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    return [Finding(name, 0, f"reaches the event outside github.event.pull_request.number: {match.group(0)!r} in {text!r}") for text in strings_of(document) for pattern in _HYGIENE_EVENT_READS for match in pattern.finditer(_ALLOWED_EVENT_READ.sub("", text))]


def live_body_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    found: list[Finding] = []
    if any("secrets." in text for text in strings_of(document)):
        found.append(Finding(name, 0, "references a secret; the hygiene check uses only the run token"))
    job = jobs_of(document).get("pr-hygiene") or {}
    if job.get("permissions") != {"contents": "read", "pull-requests": "read"}:
        found.append(Finding(name, 0, f"job pr-hygiene permissions are {job.get('permissions')!r}, must read contents and pull-requests"))
    readers = [step for step in job.get("steps") or [] if isinstance(step, dict) and (step.get("env") or {}).get("GH_TOKEN") == RUN_TOKEN and "gh api" in str(step.get("run") or "") and LIVE_PULL_REQUEST_API in str(step.get("run") or "") and "check_pr_hygiene.py" in str(step.get("run") or "") and "submitted-by" in str(step.get("run") or "")]
    if len(readers) != 1:
        found.append(Finding(name, 0, f"{len(readers)} steps read the live pull request with GH_TOKEN={RUN_TOKEN} and check it, not exactly one"))
    return found


def selector_wiring_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    """Validate selector invocation, no hidden step conditions, and S4 checks."""

    found: list[Finding] = []
    pytest_job = jobs_of(document).get("pytest") or {}
    if "if" in pytest_job:
        found.append(Finding(name, 0, "pytest job must not have a job-level if"))
    steps = [step for step in pytest_job.get("steps") or [] if isinstance(step, dict)]
    for index, step in enumerate(steps):
        if "if" in step and not is_instrumentation(step):
            found.append(Finding(name, 0, f"pytest step[{index}] has a non-instrumentation if"))
    checkout = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]
    if not checkout or any((step.get("with") or {}).get("fetch-depth") != 2 for step in checkout):
        found.append(Finding(name, 0, "pytest checkout must set fetch-depth: 2"))
    select = [step for step in steps if step.get("name") == "Select this shard's test files"]
    if len(select) != 1:
        found.append(Finding(name, 0, "pytest must have exactly one selector step"))
    else:
        script = str(select[0].get("run", ""))
        for marker in ("scripts/ci/pytest_impact.py", "--emit-shard", "${{ matrix.shard }}/4", "--out", "--meta", "grep -Eqv"):
            if marker not in script:
                found.append(Finding(name, 0, f"selector step is missing {marker!r}"))
    run = [step for step in steps if step.get("name") == "Run the shard"]
    if len(run) != 1:
        found.append(Finding(name, 0, "pytest must have exactly one shard runner step"))
    else:
        script = str(run[0].get("run", ""))
        if any(marker in script for marker in ("-k", "--ignore", "--deselect", "PYTEST_ADDOPTS")):
            found.append(Finding(name, 0, "shard runner weakens the pytest selection contract"))
        if script.count('-m "not gui_acceptance"') != 1:
            found.append(Finding(name, 0, "shard runner must use exactly one not gui_acceptance marker"))
        for marker in ("PLAN_MODE", "PLAN_TOTAL", "SHARD_INDEX", "PYTEST_SHARD_EMPTY", "expected_total=", "git ls-files -z"):
            if marker not in script:
                found.append(Finding(name, 0, f"shard runner is missing {marker!r}"))
    return found


def nightly_workflow_offenders(documents: dict[str, dict[str, Any]]) -> list[Finding]:
    """Validate the independent scheduled full-suite workflow."""

    name = "nightly-full.yml"
    document = documents.get(name)
    if document is None:
        return [Finding(name, 0, "nightly-full.yml is missing")]
    found: list[Finding] = []
    trigger = triggers(document)
    if not isinstance(trigger, dict) or not isinstance(trigger.get("schedule"), list) or not any(isinstance(item, dict) and item.get("cron") for item in trigger.get("schedule", [])):
        found.append(Finding(name, 0, "nightly workflow must have a schedule cron"))
    if isinstance(trigger, dict) and any(
        isinstance(config, dict) and any(key in config for key in NIGHTLY_TRIGGER_FILTERS)
        for config in trigger.values()
    ):
        found.append(Finding(name, 0, "nightly workflow must not use trigger filters"))
    if concurrency_group(document.get("concurrency")) != "nightly-full-${{ github.ref }}":
        found.append(Finding(name, 0, "nightly concurrency group is not nightly-full-${{ github.ref }}"))
    concurrency = document.get("concurrency") or {}
    if not isinstance(concurrency, dict) or concurrency.get("cancel-in-progress") is not False:
        found.append(Finding(name, 0, "nightly concurrency must not cancel in-progress runs"))
    jobs = jobs_of(document)
    for job_name, job in jobs.items():
        if job_name.casefold() in {"gate", "pr-hygiene"} or str(job.get("name", "")).strip().casefold() in {"gate", "pr-hygiene"}:
            found.append(Finding(name, 0, f"nightly defines reserved job {job_name!r}"))
        if job_name in {"nightly-static", "nightly-pytest", "nightly-summary"} and job.get("runs-on") != "ubuntu-latest":
            found.append(Finding(name, 0, f"job {job_name} must use the GitHub-hosted ubuntu-latest runner"))
        for index, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict) and step.get("continue-on-error") not in {None, False, "false", "False", "FALSE"}:
                found.append(Finding(name, 0, f"job {job_name} step[{index}] declares continue-on-error"))
        if job.get("continue-on-error") not in {None, False, "false", "False", "FALSE"}:
            found.append(Finding(name, 0, f"job {job_name} declares continue-on-error"))
    pytest_job = jobs.get("nightly-pytest") or {}
    if "if" in pytest_job:
        found.append(Finding(name, 0, "nightly-pytest must not have a job-level if"))
    strategy = pytest_job.get("strategy") or {}
    if strategy.get("fail-fast") is not False or (strategy.get("matrix") or {}).get("shard") != [1, 2, 3, 4]:
        found.append(Finding(name, 0, "nightly-pytest must use four non-fail-fast shards"))
    steps = [step for step in pytest_job.get("steps") or [] if isinstance(step, dict)]
    for index, step in enumerate(steps):
        if "if" in step and not (step.get("if") == "always()" and "upload-artifact" in str(step.get("uses", ""))):
            found.append(Finding(name, 0, f"nightly-pytest step[{index}] has an unexpected if"))
    setup_node = [step for step in steps if str(step.get("uses", "")).startswith("actions/setup-node@")]
    if len(setup_node) != 1 or (setup_node[0].get("with") or {}).get("node-version") != "24":
        found.append(Finding(name, 0, "nightly-pytest must install Node 24 exactly once"))
    pi_setup = [step for step in steps if step.get("name") == "Install the pinned Pi SDK"]
    pi_script = str(pi_setup[0].get("run", "")) if len(pi_setup) == 1 else ""
    if len(pi_setup) != 1 or "@earendil-works/pi-coding-agent@0.85.1" not in pi_script or '$(pi --version)" = "0.85.1"' not in pi_script or "HYPRIAL_REQUIRE_PI_SDK=1" not in pi_script:
        found.append(Finding(name, 0, "nightly-pytest must install and require Pi SDK 0.85.1"))
    upload = [step for step in steps if "upload-artifact" in str(step.get("uses", ""))]
    if len(upload) != 1 or upload[0].get("timeout-minutes") != 5:
        found.append(Finding(name, 0, "nightly JUnit upload must have a five-minute timeout"))
    select = [step for step in steps if step.get("name") == "Select this shard's test files"]
    select_script = str(select[0].get("run", "")) if len(select) == 1 else ""
    if len(select) != 1 or "scripts/ci/pytest_impact.py" not in select_script or '--emit-shard "${{ matrix.shard }}/4"' not in select_script:
        found.append(Finding(name, 0, "nightly-pytest must use the shared selector with four shards"))
    run = [step for step in steps if step.get("name") == "Run the shard"]
    run_script = str(run[0].get("run", "")) if len(run) == 1 else ""
    if len(run) != 1:
        found.append(Finding(name, 0, "nightly-pytest must have exactly one shard runner"))
    else:
        forbidden = ("-k", "--keyword", "--ignore", "--deselect")
        if any(marker in run_script for marker in forbidden) or "PYTEST_ADDOPTS" in (run[0].get("env") or {}):
            found.append(Finding(name, 0, "nightly shard runner weakens the full-suite pytest contract"))
        required = (
            'test "${PLAN_MODE}" = full',
            "PLAN_TOTAL",
            "SHARD_INDEX",
            'if test "${SHARD_COUNT}" -eq 0; then',
            "git ls-files -z -- 'tests/**/test_*.py' 'tests/test_*.py' 'tests/**/*_test.py' 'tests/*_test.py'",
            "tr -dc '\\0' | wc -c",
            "expected_shard_count=$(( (expected_total + 4 - SHARD_INDEX) / 4 ))",
        )
        for marker in required:
            if marker not in run_script:
                found.append(Finding(name, 0, f"nightly shard runner is missing {marker!r}"))
        if run_script.count('-m "not gui_acceptance"') != 1:
            found.append(Finding(name, 0, "nightly shard runner must use exactly one not gui_acceptance marker"))
    summary = jobs.get("nightly-summary") or {}
    if summary.get("if") != "always()" or set(as_list(summary.get("needs"))) != {"nightly-static", "nightly-pytest"}:
        found.append(Finding(name, 0, "nightly-summary must always require static and pytest"))
    summary_run = str((summary.get("steps") or [{}])[0].get("run", ""))
    if "STATIC=success" not in summary_run or "PYTEST=success" not in summary_run:
        found.append(Finding(name, 0, "nightly-summary must require success from both jobs"))
    return found


def tree_documents() -> dict[str, dict[str, Any]]:
    return {path.name: load(path.read_text(encoding="utf-8")) for path in workflow_files()}


def gate_scope_offenders(documents: dict[str, dict[str, Any]]) -> list[Finding]:
    """Run rules 10–17 across all discovered workflows."""

    strict_files = STRICT_FILES
    found: list[Finding] = []
    for file_name, document in documents.items():
        found.extend(foreign_required_check_offenders(document, file_name))
        found.extend(owned_job_offenders(document, file_name))
        found.extend(concurrency_offenders(document, file_name))
        found.extend(job_permission_offenders(document, file_name))
        found.extend(event_body_offenders(document, file_name))
    found.extend(required_trigger_offenders(documents))
    found.extend(cancel_policy_offenders(documents))
    if PR_HYGIENE_FILE in documents and PR_HYGIENE_FILE in strict_files:
        found.extend(live_body_offenders(documents[PR_HYGIENE_FILE], PR_HYGIENE_FILE))
        found.extend(hygiene_event_offenders(documents[PR_HYGIENE_FILE], PR_HYGIENE_FILE))
    if GATE_FILE in documents and GATE_FILE in strict_files:
        found.extend(selector_wiring_offenders(documents[GATE_FILE], GATE_FILE))
    found.extend(nightly_workflow_offenders(documents))
    return found
