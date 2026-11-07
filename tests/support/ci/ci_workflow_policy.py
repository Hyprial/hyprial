"""Readers for the CI workflow policies that more than one test enforces.

Each policy has exactly ONE reader here, so the rule exists once and the
tests that enforce it do not drift apart:

1. ``uses:`` **is allowed, but the ref must be pinned.**  Allen 2026-09-21:
   "改为使用 use 从默认源去取,我们应该尽可能复用社区成熟能力".  The property the
   old blanket ban protected (no runner-preinstalled action is ever implied,
   the source is traceable) only keeps holding while *something checks the
   pin* -- otherwise "pinned" is prose, which is the disease this file exists
   to cure.  A floating ref (``@main`` / ``@HEAD`` / ``@latest`` / no ``@``)
   means "whatever upstream or the runner happens to have today".

2. **The condition-reader tail is mandatory for every runner job that is not
   in the declared exemption table.**  The table is *fail-closed*: a new job is
   required to carry the tail unless someone writes it into the table with a
   reason, so "not in the list" can no longer be read as "not required".  That
   misreading is exactly what the frozen ``len(jobs) == 12`` produced: the
   enumerated contract froze the day it was written while its object (the
   repository's job set) kept growing.

3. **A gate step is one whose effective condition is the job's success**: no
   ``if`` at all, or an ``if`` that starts with ``success()``.  Those steps
   decide whether the job is green, so they must not be relaxed with
   ``continue-on-error``.  Steps that spell ``always()`` are instrumentation
   and may be relaxed (the artifact upload is, on purpose).
"""

from __future__ import annotations

import re
from typing import Any, Iterator, NamedTuple

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = ROOT / ".forgejo" / "workflows"


class JobRef(NamedTuple):
    path: Path
    name: str
    job: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.path.name}::{self.name}"

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self.job.get("steps") or [])


def workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def workflow_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def runner_jobs() -> list[JobRef]:
    """Every job that gets a runner -- the complete current workflow set.

    Kept separate from the instrumented set on purpose: the two used to share
    the name "jobs", which is how `len(jobs) == 12` came to be read as "the
    repository has 12 jobs" when it actually meant "12 jobs carry the tail".
    """

    import yaml

    jobs: list[JobRef] = []
    for path in workflow_files():
        document = yaml.safe_load(workflow_text(path))
        for name, job in (document.get("jobs") or {}).items():
            if isinstance(job, dict) and "runs-on" in job:
                jobs.append(JobRef(path, str(name), job))
    return jobs


# --- policy 1: `uses:` is allowed only with a pinned ref ---------------------

# Step-level directive, bare or list-item form.  Anchored on the key so that a
# comment such as "# artifact action is forbidden here (#503's usual fix uses
# `uses:`)" is not read as a directive.
USES_DIRECTIVE = re.compile(r"^\s*(?:-\s+)?uses\s*:\s*(?P<value>\S+)\s*$")

# Branch-like / moving aliases.  A bare version tag is accepted (`@v3`), a
# branch is not.  See the module docstring for what "pinned" does and does not
# claim: `@v3` can still be moved by the upstream owner, so the property kept is
# "the ref is named and is not a branch/HEAD alias", not "upstream is frozen".
_FLOATING_REFS = frozenset(
    {"main", "master", "trunk", "develop", "head", "latest", "unstable"}
)
_SHA40 = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
# Version-like: `v3`, `v3.1.2`, `8.30.1`, `v3.0.0-beta.1`.  Deliberately narrow --
# a ref that merely CONTAINS a digit (`v3branch`, `node22`) is a moving name, and
# accepting it would make the pin advisory again.
_VERSION_LIKE = re.compile(r"v?\d+(?:\.\d+){0,3}(?:-[0-9A-Za-z.]+)?", re.IGNORECASE)


def uses_directive_value(line: str) -> str | None:
    match = USES_DIRECTIVE.match(line)
    return match.group("value") if match else None


def unpinned_reason(value: str) -> str | None:
    """Return why this ``uses:`` value is not pinned, or None if it is."""

    if "@" not in value:
        return "no @ref at all: the runner resolves whatever the default source has"
    ref = value.rsplit("@", 1)[1]
    if not ref:
        return "empty @ref"
    if ref.lower() in _FLOATING_REFS:
        return f"@{ref} is a moving branch/alias, not a version"
    if _SHA40.fullmatch(ref):
        return None
    if ref.startswith("refs/") or ref.startswith("/"):
        return f"@{ref} is an explicit ref, not a version"
    if not _VERSION_LIKE.fullmatch(ref):
        return f"@{ref} is not version-like (and not a 40-hex commit)"
    return None


def unpinned_uses_offenders_in(text: str, label: str = "<text>") -> list[str]:
    """Text-level reader, so a control can mutate a known-good workflow."""

    offenders: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        value = uses_directive_value(line)
        if value is None:
            continue
        reason = unpinned_reason(value)
        if reason is not None:
            offenders.append(f"{label}:{lineno}: uses: {value} -- {reason}")
    return offenders


def unpinned_uses_offenders() -> list[str]:
    """Every ``uses:`` directive in the tree that is not pinned to a version."""

    offenders: list[str] = []
    for path in workflow_files():
        offenders.extend(unpinned_uses_offenders_in(workflow_text(path), path.name))
    return offenders


def uses_directives() -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    for path in workflow_files():
        for lineno, line in enumerate(workflow_text(path).splitlines(), 1):
            value = uses_directive_value(line)
            if value is not None:
                found.append((path, lineno, value))
    return found


_EXTERNAL_COMMAND = re.compile(
    r"(?:^|[;&(\s])(?:curl|git|gh|wget|uv|python(?:3)?)\b"
)
_SECRET_EXPANSION = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:[^}]*)?\}|"
    r"(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def secret_argv_offenders_in(run: str, label: str = "<run>") -> list[str]:
    """Find secret-named variables expanded in an external command's argv.

    Shell builtins may consume a secret and pipe configuration through stdin;
    that keeps the value out of the external process argv. Backslash-continued
    lines are joined before pipeline segments are inspected so a ``curl`` whose
    ``-H`` argument sits on the next source line cannot escape the gate.
    """

    logical = re.sub(r"\\\n\s*", " ", run)
    offenders: list[str] = []
    for lineno, line in enumerate(logical.splitlines(), 1):
        for segment in re.split(r"(?<!\|)\|(?!\|)", line):
            command = _EXTERNAL_COMMAND.search(segment)
            if command is None:
                continue
            # Prefix assignments (``TOKEN=... git fetch``) are process env,
            # not argv. Inspect from the external command word onward.
            command_argv = segment[command.start():]
            expanded = [
                match.group("braced") or match.group("bare")
                for match in _SECRET_EXPANSION.finditer(command_argv)
            ]
            names = {
                name
                for name in expanded
                if any(
                    marker in name.upper()
                    for marker in ("TOKEN", "SECRET", "PASSWORD")
                )
                or name.upper().endswith("_KEY")
            }
            for name in sorted(names):
                offenders.append(
                    f"{label}:{lineno}: {name} is expanded in external-command argv"
                )
    return offenders


def secret_argv_offenders() -> list[str]:
    """Inspect every parsed workflow ``run:`` block for argv secret leaks."""

    offenders: list[str] = []
    for ref in runner_jobs():
        for index, step in enumerate(ref.steps):
            run = step.get("run")
            if isinstance(run, str):
                offenders.extend(
                    secret_argv_offenders_in(run, f"{ref.key}::step[{index}]")
                )
    return offenders


# --- policy 2: the instrumented tail set, fail-closed ------------------------

START_PATTERN = re.compile(r"bash .*ci-conditions\.sh(?:\"|')? start")
END_PATTERN = re.compile(r"bash .*ci-conditions\.sh(?:\"|')? end")
ARTIFACT_PATTERN = re.compile(r"bash .*ci-conditions\.sh(?:\"|')? artifact")

# `Record end CI conditions` / `Upload CI conditions artifact` / `Report CI
# conditions artifact upload failure`, verbatim -- the shape every instrumented
# job must carry as its LAST three steps.
TAIL_END_IF = "always()"
TAIL_REPORT_IF = "always() && steps.ci_conditions_artifact.outcome == 'failure'"
TAIL_UPLOAD_STEP = {
    "name": "Upload CI conditions artifact",
    "id": "ci_conditions_artifact",
    "if": "always()",
    "continue-on-error": True,
    "timeout-minutes": 3,
    "uses": "actions/upload-artifact@v3",
    "with": {
        "name": "ci-conditions-${{ github.job }}",
        "path": "${{ runner.temp }}/ci-conditions-${{ github.job }}.txt",
        "if-no-files-found": "error",
    },
    # upload-artifact@v3 ignores NO_PROXY; the job proxy must be cleared so
    # the upload reaches the runner's artifact runtime directly (#2152).
    "env": {"HTTP_PROXY": "", "HTTPS_PROXY": "", "http_proxy": "", "https_proxy": ""},
}

def tail_upload_step(job: dict[str, Any]) -> dict[str, Any]:
    """The shared upload step, made unique per instance of a one-key matrix.

    Matrix instances share ``github.job``, so the verbatim step would make
    every instance upload one artifact name and overwrite each other's file
    (2026-10-04, ``gate-python.yml::pytest-shard``). Only the artifact NAME gains the
    matrix value; every other field, and every non-matrix job, stays verbatim.
    """

    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict) or len(matrix) != 1:
        return TAIL_UPLOAD_STEP
    (key,) = matrix
    step = {**TAIL_UPLOAD_STEP, "with": dict(TAIL_UPLOAD_STEP["with"])}
    step["with"]["name"] = f"ci-conditions-${{{{ github.job }}}}-${{{{ matrix.{key} }}}}"
    return step


# Jobs the ticket deliberately leaves uninstrumented.  Fail-closed by design: a
# job that is not listed here MUST carry the tail, so a new job cannot be read
# as "not required" just because nobody remembered it.  Each entry carries its
# class and the fact that this is a deferral, not a judgement that the readings
# are unneeded -- "deliberately out of scope" and "nobody thought about it"
# look identical afterwards, and only the written reason separates them.
TAIL_EXEMPT_JOBS: dict[str, str] = {
    "ops-cancel-on-pr-close.yml::cancel": (
        "class: PR-close API maintenance, one step with per-run HTTP receipts "
        "in the job summary, no suite and no checkout of PR-controlled code. "
        "deferred: host-condition instrumentation would add a source bootstrap "
        "and artifact dependency to the cancellation path"
    ),
    "gate-pr-declarations.yml::impact": (
        "class: PR-metadata gate (pull_request only, one step, no suite). "
        "deferred: the instrumented set is pinned at the 2026-09-14 twelve for "
        "this ticket; instrumenting it is a follow-up, not a verdict that its "
        "readings are unneeded"
    ),
    "github-publish.yml::publish": (
        "class: tag-triggered publish pipeline (no suite; its transport story "
        "is an API comment because #503 log loss, see P3/P4). deferred as above"
    ),
    "gate-gui.yml::gui-unit": "class: independent GUI/Node package job. deferred as above",
    "gate-gui.yml::release-package": (
        "class: separate GUI/Node package job. deferred as above"
    ),
    "release-tag.yml::tag": (
        "class: git tag mover (workflow_dispatch only, no suite). deferred as above"
    ),
    "homepage-changelog-sync.yml::sync": (
        "class: workflow_dispatch-only Homepage release sync (no suite; it "
        "dispatches the trusted guard and derives its evidence from that run). "
        "deferred as above"
    ),
    "gate-pr-declarations.yml::changelog": (
        "class: PR-metadata gate (pull_request only, one python3 step, no suite). "
        "deferred: it exists precisely because the instrumented job set is the "
        "heavy one this gate must not depend on (no uv, no artifact upload); "
        "instrumenting it is a follow-up, not a verdict that its readings are "
        "unneeded"
    ),
}


def carries_reader(job: dict[str, Any]) -> bool:
    """Whether the job's FIRST step records the start conditions.

    This is the shape reader, not the policy: the policy is
    ``runner_jobs() - TAIL_EXEMPT_JOBS`` (see ``missing_tail_jobs``).  Reading
    the shape without also asserting the set is how the old contract turned
    into an enumeration that could not notice a new job.
    """

    steps = list(job.get("steps") or [])
    if not steps:
        return False
    return bool(START_PATTERN.search(str(steps[0].get("run", ""))))


def runner_jobs_in(text: str, path: Path | None = None) -> list[JobRef]:
    """Runner jobs in a workflow document, so a control can mutate one."""

    import yaml

    jobs: list[JobRef] = []
    label = path if path is not None else Path("<synthetic>.yml")
    document = yaml.safe_load(text)
    for name, job in (document.get("jobs") or {}).items():
        if isinstance(job, dict) and "runs-on" in job:
            jobs.append(JobRef(label, str(name), job))
    return jobs


def missing_tail_jobs(
    jobs: list[JobRef] | None = None,
) -> tuple[list[JobRef], list[str]]:
    """(jobs that must carry the tail, structural complaints about them).

    The required set is DERIVED (all runner jobs minus the written exemptions),
    so a job that loses its tail fails by name instead of being silently
    dropped from the set being checked.
    """

    refs = runner_jobs() if jobs is None else jobs
    required: list[JobRef] = []
    complaints: list[str] = []
    for ref in refs:
        if ref.key in TAIL_EXEMPT_JOBS:
            continue
        required.append(ref)
        steps = ref.steps
        first_body = str(steps[0].get("run", "")) if steps else ""
        all_bodies = "\n".join(str(step.get("run", "")) for step in steps)
        if not START_PATTERN.search(first_body):
            complaints.append(f"{ref.key} first step does not record start conditions")
        if len(START_PATTERN.findall(all_bodies)) != 1:
            complaints.append(f"{ref.key} must record start conditions exactly once")
        if len(END_PATTERN.findall(all_bodies)) != 1:
            complaints.append(f"{ref.key} must record end conditions exactly once")
        if len(steps) < 3:
            complaints.append(f"{ref.key} does not carry the three-step tail")
            continue
        end, upload, report = steps[-3:]
        if end.get("if") != TAIL_END_IF or not END_PATTERN.search(
            str(end.get("run", ""))
        ):
            complaints.append(
                f"{ref.key} last three steps must start with the end reader"
            )
        if upload != tail_upload_step(ref.job):
            complaints.append(
                f"{ref.key} upload step is not the shared artifact step verbatim"
            )
        if report.get("if") != TAIL_REPORT_IF:
            complaints.append(
                f"{ref.key} report step is not gated on the upload outcome"
            )
        elif not ARTIFACT_PATTERN.search(str(report.get("run", ""))):
            complaints.append(f"{ref.key} report step does not call the reader")
    return required, complaints


def runner_counts(jobs: list[JobRef]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ref in jobs:
        runner = str(ref.job["runs-on"])
        counts[runner] = counts.get(runner, 0) + 1
    return counts


# --- policy 3: gate steps must not be relaxed --------------------------------


def is_gate_step(step: dict[str, Any]) -> bool:
    condition = step.get("if")
    if condition is None:
        # No `if` means the runner's default status check applies: the step
        # runs only if everything before it succeeded.
        return True
    return str(condition).strip().startswith("success()")


def gate_steps(job: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, step)
        for index, step in enumerate(job.get("steps") or [])
        if isinstance(step, dict) and is_gate_step(step)
    ]


def relaxed_gate_steps(jobs: list[JobRef] | None = None) -> list[str]:
    """Every gate step in the tree that carries ``continue-on-error``."""

    refs = runner_jobs() if jobs is None else jobs
    offenders: list[str] = []
    for ref in refs:
        for index, step in gate_steps(ref.job):
            if step.get("continue-on-error"):
                offenders.append(
                    f"{ref.key} step[{index}] {step.get('name', '?')!r} is a gate step "
                    f"relaxed with continue-on-error"
                )
    return offenders


def relaxed_gate_surfaces(text: str, label: str = "<text>") -> list[str]:
    """Gate relaxations in one workflow document, by property.

    Two surfaces, because a relaxation can be written in either place and the
    whole-file ban that used to stand here could not tell them apart from the
    legitimate `if: always()` instrumentation:

    * a gate step (effective condition = the job's success) with
      ``continue-on-error``;
    * a job-level ``continue-on-error`` -- that removes the whole job's failure
      from the run result, so it is a gate relaxation even though no step is
      annotated.

    A step that spells ``always()`` is instrumentation, not a gate, and may be
    relaxed: the artifact upload is, on purpose (its failure is reported by the
    next step instead).
    """

    refs = runner_jobs_in(text, Path(label))
    offenders = relaxed_gate_steps(refs)
    for ref in refs:
        if ref.job.get("continue-on-error"):
            offenders.append(
                f"{ref.key} job is relaxed with job-level continue-on-error"
            )
    return offenders


def gate_step_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for ref in runner_jobs():
        counts[ref.key] = len(gate_steps(ref.job))
    return counts


# --- the dev-track mover (the audit's ordering key) --------------------------

# Allen 2026-10-28: publication is authorized only by a green dev push.
INTERNAL_MOVE_CONDITION = (
    "success() && github.event_name == 'push' && github.ref == 'refs/heads/dev'"
)


def internal_publication_condition(step: dict[str, Any]) -> bool:
    """Fail closed on any condition other than the approved publication gate."""

    return step.get("if") == INTERNAL_MOVE_CONDITION


def pushes_internal_tag(run: str) -> bool:
    """Semantic identity: the step pushes `refs/tags/internal`.

    The ref is matched as a whitespace TOKEN so a renamed
    `refs/tags/internal-something` cannot match by prefix, and the literal
    command line is NOT pinned (a flag change must red the test named for the
    flag, not this one).
    """

    return "git push" in run and "refs/tags/internal" in run.split()


def internal_tag_movers() -> list[tuple[str, int, dict[str, Any]]]:
    """Every step in the tree that pushes the dev track tag, by identity."""

    movers: list[tuple[str, int, dict[str, Any]]] = []
    for ref in runner_jobs():
        for index, step in enumerate(ref.steps):
            if pushes_internal_tag(str(step.get("run", ""))):
                movers.append((ref.key, index, step))
    return movers


def main_release_policy_errors(document: dict[str, Any]) -> list[str]:
    """The main listener only requests an anchor; it never moves track tags."""

    errors = []
    triggers = document.get("on", document.get(True))
    if triggers != {"push": {"branches": ["main"]}}:
        errors.append("main push must be the only trigger")
    if document.get("concurrency", {}).get("cancel-in-progress") is not False:
        errors.append("publication must not cancel in progress")
    jobs = document.get("jobs", {})
    if len(jobs) != 1:
        errors.append("expected one dispatch job")
    for job in jobs.values():
        if job.get("if") != "github.event_name == 'push' && github.ref == 'refs/heads/main'":
            errors.append("job must require main push")
        runs = "\n".join(step.get("run", "") for step in job.get("steps", []))
        if "release-tag.yml/dispatches" not in runs:
            errors.append("must dispatch the independent release-tag workflow")
        if "git push" in runs or "release_tracks.py" in runs:
            errors.append("listener must not write tags")
    return errors


def wheel_publication_policy_errors(jobs: list[JobRef]) -> list[str]:
    """All three wheel destinations are required gates, never opt-in writes."""

    errors = []
    for script in ("attach_wheel_assets.py", "publish_wheels.py", "uv publish dist/*.whl"):
        matches = [(ref, step) for ref in jobs for step in ref.steps
                   if script in step.get("run", "")]
        if len(matches) != 1:
            errors.append(f"{script}: expected one mandatory publisher")
            continue
        ref, step = matches[0]
        if ref.job.get("if") or ref.job.get("continue-on-error"):
            errors.append(f"{script}: job cannot make publication optional")
        if step.get("if") not in (None, "success()") or step.get("continue-on-error"):
            errors.append(f"{script}: step must require success, not an opt-in switch")
        if any(f"vars.{switch}" in str(ref.job) for switch in
               ("ATTACH_WHEEL_ASSETS", "MIRROR_WHEELS_TO_R2")):
            errors.append(f"{script}: optional distribution variable is forbidden")
    return errors


def iter_steps() -> Iterator[tuple[JobRef, int, dict[str, Any]]]:
    for ref in runner_jobs():
        for index, step in enumerate(ref.steps):
            yield ref, index, step
