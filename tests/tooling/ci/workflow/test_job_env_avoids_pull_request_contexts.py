"""A job-level env must not dereference pull_request contexts.

Forgejo 16 expands job-level `env` while scheduling, before it evaluates the
job's `if:`. `github.event.pull_request` is null on a push, so a job that keeps
`${{ github.event.pull_request... }}` in its job-level env fails scheduling with
status=failure, task_id=0 and no job log -- it never becomes "skipped".

Live evidence (2026-10-28, powerstation): `pr-hygiene` failed 100% of push (459)
and workflow_dispatch (21) runs while 91 pull_request runs succeeded, and its
job-level env carried BASE_REF/HEAD_SHA from `github.event.pull_request`.

The same values are still allowed on a *step*: a step of a pull_request-only job
is never expanded when the job is skipped.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".forgejo" / "workflows" / "gate-pr-hygiene.yml"
PULL_REQUEST_CONTEXT = "github.event.pull_request"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job_level_offenders(document: dict) -> list[tuple[str, str]]:
    offenders: list[tuple[str, str]] = []
    for job_name, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for key, value in (job.get("env") or {}).items():
            if PULL_REQUEST_CONTEXT in str(value):
                offenders.append((str(job_name), str(key)))
    return offenders


def test_no_job_level_env_dereferences_a_pull_request_context() -> None:
    offenders = _job_level_offenders(_workflow())
    assert offenders == [], (
        "Forgejo expands job-level env before the job's if:, so these fail "
        f"scheduling on push events: {offenders}. Move them onto the steps that "
        "use them (a step of a pull_request-only job is never expanded when the "
        "job is skipped)."
    )


def test_the_guard_still_catches_a_reintroduced_job_level_reference() -> None:
    """Mutation control: the reader must red when a job env regains the context."""

    document = _workflow()
    jobs = document["jobs"]
    first = next(name for name, job in jobs.items() if isinstance(job, dict))
    jobs[first] = dict(jobs[first])
    jobs[first]["env"] = {
        **(jobs[first].get("env") or {}),
        "REGRESSED_REF": "${{ github.event.pull_request.base.ref }}",
    }
    assert _job_level_offenders(document), "the mutation was not detected"


def _unguarded_pull_request_references(document: dict) -> list[tuple[str, str, str]]:
    """Every env value that dereferences pull_request without an event guard."""

    offenders: list[tuple[str, str, str]] = []
    events = document.get("on", document.get(True))
    pull_request_only = isinstance(events, dict) and set(events) == {"pull_request"}
    for job_name, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        entries = [("job", job.get("env") or {})]
        for index, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict):
                entries.append((f"step[{index}]", step.get("env") or {}))
        for where, env in entries:
            for key, value in env.items():
                text = str(value)
                if PULL_REQUEST_CONTEXT not in text:
                    continue
                if "github.event_name == 'pull_request'" in text:
                    continue
                if where != "job" and pull_request_only:
                    continue
                offenders.append((str(job_name), where, str(key)))
    return offenders


def test_every_pull_request_reference_is_guarded_by_the_event_check() -> None:
    """A step needs an event guard when the workflow accepts non-PR events.

    Job-level env remains restricted because its evaluation precedes `if:`.
    """

    offenders = _unguarded_pull_request_references(_workflow())
    assert offenders == [], (
        "these env values dereference github.event.pull_request without a "
        f"`github.event_name == 'pull_request'` guard: {offenders}"
    )


def test_the_guard_check_catches_an_unguarded_step_reference() -> None:
    document = _workflow()
    document[True] = {**document[True], "push": {}}
    job = document["jobs"]["pr-hygiene"]
    job["steps"] = list(job.get("steps") or [])
    job["steps"][0] = dict(job["steps"][0])
    job["steps"][0]["env"] = {
        **(job["steps"][0].get("env") or {}),
        "UNGUARDED": "${{ github.event.pull_request.base.ref }}",
    }
    assert _unguarded_pull_request_references(document)


def _unguarded_step_conditions(document: dict) -> list[tuple[str, int, str]]:
    """Step `if:` expressions that dereference pull_request without the guard.

    A step of a skipped job is not evaluated, so this is "safe but fragile": the
    job's `if:` is what keeps it alive today, and any future change to that
    condition would turn a push run into a scheduling failure. The concurrency
    group is deliberately NOT covered here -- it is not an `if:`, it carries its
    own `|| github.run_id` fallback, and push/dispatch runs demonstrably expand
    it (only pr-hygiene ever failed).
    """

    offenders: list[tuple[str, int, str]] = []
    for job_name, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            condition = step.get("if")
            if condition is None:
                continue
            text = str(condition)
            if PULL_REQUEST_CONTEXT not in text:
                continue
            if "github.event_name == 'pull_request'" in text:
                continue
            offenders.append((str(job_name), index, text))
    return offenders


def test_step_conditions_guard_pull_request_references() -> None:
    offenders = _unguarded_step_conditions(_workflow())
    assert offenders == [], (
        "a step `if:` must not depend on the job's own condition to protect it: "
        f"{offenders}"
    )


def test_the_values_are_still_available_to_the_steps_that_need_them() -> None:
    """Vacuity guard: the fix must not have deleted the values outright."""

    document = _workflow()
    job = document["jobs"]["pr-hygiene"]
    step_envs = [
        str(value)
        for step in job.get("steps") or []
        for value in (step.get("env") or {}).values()
    ]
    assert any(PULL_REQUEST_CONTEXT in value for value in step_envs), (
        "pr-hygiene no longer exposes the pull_request refs to its steps"
    )
