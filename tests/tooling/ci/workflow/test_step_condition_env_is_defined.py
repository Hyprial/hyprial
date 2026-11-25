"""A step `if:` that reads `env.X` must receive X from somewhere.

The #1566 reminder step shipped as `if: env.BASE_REF != ''` with no `env:`
block.  BASE_REF and HEAD_SHA existed only inside *another* step's env, and no
step wrote them to `$GITHUB_ENV`, so a step's `env` context never carried them:
the condition was false on every event, the step never ran, and its nine unit
tests stayed green while the feature did nothing.  A condition that reads an
undefined variable is not a disabled feature, it is a silent one.

So every `env.<NAME>` referenced by a step condition must be defined by
* that step's own `env:`,
* its job's `env:`,
* the workflow's `env:`, or
* an earlier step of the same job writing `NAME=...` to `$GITHUB_ENV`.

The same defect hides one level down: `${BASE_REF}` inside `run:` expands to
the empty string when nothing defines it, so the checker would be asked for a
diff against `origin/`.  The reminder step is therefore also pinned to read
only shell variables its own `env:` block supplies.

The check is static: it proves the names are supplied, not that the values are
right, and it never claims the step ran.  Real proof is a PR CI run whose log
carries the step's own `CI_TIMING stage=inline-comments` line.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github" / "workflows"
REMINDER_STEP = "Remind on new inline comments"
REMINDER_JOB = "pr-hygiene"

ENV_REFERENCE = re.compile(r"\benv\.([A-Za-z_][A-Za-z0-9_]*)")
SHELL_REFERENCE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
SHELL_ASSIGNMENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=")


def _documents() -> list[tuple[str, dict]]:
    documents: list[tuple[str, dict]] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        documents.append((path.name, document))
    return documents


def _tests_yml() -> dict:
    return yaml.safe_load((WORKFLOWS / "gate-pr-hygiene.yml").read_text(encoding="utf-8"))


def _steps(document: dict, job_id: str) -> list[dict]:
    return [step for step in document["jobs"][job_id].get("steps") or [] if isinstance(step, dict)]


def _condition_variables(step: dict) -> set[str]:
    return set(ENV_REFERENCE.findall(str(step.get("if") or "")))


def _run_variables(step: dict) -> set[str]:
    run = str(step.get("run") or "")
    return set(SHELL_REFERENCE.findall(run)) - set(SHELL_ASSIGNMENT.findall(run))


def _written_to_github_env(steps: list[dict]) -> set[str]:
    written: set[str] = set()
    for step in steps:
        run = str(step.get("run") or "")
        if "GITHUB_ENV" not in run:
            continue
        written |= set(SHELL_ASSIGNMENT.findall(run))
    return written


def _defined_before(document: dict, job_id: str, index: int) -> set[str]:
    job = document["jobs"][job_id]
    steps = _steps(document, job_id)
    defined = set(document.get("env") or {})
    defined |= set(job.get("env") or {})
    defined |= set(steps[index].get("env") or {})
    defined |= _written_to_github_env(steps[:index])
    return defined


def _offenders(document: dict, label: str) -> list[str]:
    offenders: list[str] = []
    for job_id, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict) or not step.get("if"):
                continue
            missing = _condition_variables(step) - _defined_before(document, str(job_id), index)
            for name in sorted(missing):
                offenders.append(
                    f"{label}:{job_id} step[{index}] {step.get('name', '?')!r} "
                    f"tests env.{name}, which no env block or $GITHUB_ENV write supplies"
                )
    return offenders


def test_every_step_condition_env_reference_is_defined() -> None:
    offenders = [
        offender
        for label, document in _documents()
        for offender in _offenders(document, label)
    ]
    assert offenders == [], (
        "a step `if:` reading an undefined env var is permanently false, so the "
        f"step silently never runs: {offenders}"
    )


def test_the_scan_sees_the_references_it_is_meant_to_judge() -> None:
    """Vacuity guard: a reader that sees nothing must not read as clean."""

    references = sum(
        len(_condition_variables(step))
        for _label, document in _documents()
        for job in (document.get("jobs") or {}).values()
        if isinstance(job, dict)
        for step in job.get("steps") or []
        if isinstance(step, dict)
    )
    assert references >= 1, f"no step condition reads env.*; the scan proved nothing: {references}"


def _reminder_step(document: dict) -> dict:
    matches = [step for step in _steps(document, REMINDER_JOB) if step.get("name") == REMINDER_STEP]
    assert len(matches) == 1, f"{REMINDER_JOB} must carry exactly one {REMINDER_STEP!r} step"
    return matches[0]


def _undefined_reads(step: dict) -> list[str]:
    env = set(step.get("env") or {})
    reads = _condition_variables(step) | _run_variables(step)
    return sorted(reads - env)


def test_the_reminder_step_defines_every_variable_it_reads() -> None:
    step = _reminder_step(_tests_yml())
    env = step.get("env") or {}

    assert _condition_variables(step), (
        "the reminder must scope itself with a condition instead of running on every event"
    )
    assert _undefined_reads(step) == [], (
        "the reminder step reads variables its own env: block must supply; an "
        "empty ${BASE_REF} asks for a diff against origin/"
    )
    for name in ("BASE_REF", "HEAD_SHA"):
        value = str(env[name])
        assert "github.event.pull_request" in value
        assert "github.event_name == 'pull_request'" in value, (
            f"{name} must stay guarded by the event check; Forgejo expands env "
            "before it evaluates `if:`"
        )


def test_the_old_shape_without_the_step_env_is_reported() -> None:
    """Mutation control: the #1566 shape keeps the condition, drops the env."""

    document = copy.deepcopy(_tests_yml())
    step = _reminder_step(document)
    step.pop("env", None)

    offenders = _offenders(document, "gate-pr-hygiene.yml")

    assert any("env.BASE_REF" in offender for offender in offenders), offenders
    assert _undefined_reads(step) == ["BASE_REF", "HEAD_SHA"], step
