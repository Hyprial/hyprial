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

The check is static: it proves the names are supplied, not that the values are
right, and it never claims the step ran.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[4] / ".github" / "workflows"

ENV_REFERENCE = re.compile(r"\benv\.([A-Za-z_][A-Za-z0-9_]*)")
SHELL_ASSIGNMENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=")


def _documents() -> list[tuple[str, dict]]:
    documents: list[tuple[str, dict]] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        documents.append((path.name, document))
    return documents


def _steps(document: dict, job_id: str) -> list[dict]:
    return [step for step in document["jobs"][job_id].get("steps") or [] if isinstance(step, dict)]


def _condition_variables(step: dict) -> set[str]:
    return set(ENV_REFERENCE.findall(str(step.get("if") or "")))


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
    documents = _documents()
    assert any(label == "gate.yml" for label, _document in documents), [label for label, _document in documents]
    offenders = [offender for label, document in documents for offender in _offenders(document, label)]
    assert offenders == [], (
        "a step `if:` reading an undefined env var is permanently false, so the "
        f"step silently never runs: {offenders}"
    )


def test_an_undefined_reference_is_reported() -> None:
    """Mutation control: the #1566 shape keeps the condition, drops the env."""

    document = {
        "jobs": {
            "gate": {
                "steps": [
                    {"name": "Remind", "if": "env.BASE_REF != ''", "run": "echo reminder"},
                ]
            }
        }
    }

    offenders = _offenders(document, "synthetic.yml")

    assert len(offenders) == 1, offenders
    assert "synthetic.yml:gate step[0] 'Remind'" in offenders[0]
    assert "tests env.BASE_REF" in offenders[0]


def test_a_reference_supplied_by_an_earlier_github_env_write_is_clean() -> None:
    """The other half of the rule: a ``$GITHUB_ENV`` write does supply the name."""

    document = {
        "jobs": {
            "gate": {
                "steps": [
                    {"name": "Export", "run": "echo \"BASE_REF=origin/dev\" >> \"$GITHUB_ENV\""},
                    {"name": "Remind", "if": "env.BASE_REF != ''", "run": "echo reminder"},
                ]
            }
        }
    }

    assert _offenders(document, "synthetic.yml") == []
