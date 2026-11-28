"""Readers for the CI workflow policies that more than one test enforces.

Each policy has exactly ONE reader here, so the rule exists once and the
tests that enforce it do not drift apart:

1. ``uses:`` **is allowed, but the ref must be pinned.**  Allen 2026-09-21:
   "改为使用 use 从默认源去取,我们应该尽可能复用社区成熟能力".  The property the
   old blanket ban protected (no runner-preinstalled action is ever implied,
   the source is traceable) only keeps holding while *something checks the
   pin* -- otherwise "pinned" is prose, which is the disease this file exists
   to cure.  A floating ref (``@main`` / ``@HEAD`` / ``@latest`` / no ``@``)
   means "whatever upstream or the runner happens to have today".  The GitHub
   merge-gate policy is stricter for ``gate.yml`` (a full commit SHA) and is
   read by ``tests.support.ci_download.github_gate_policy``.

2. **A secret-named variable is never expanded into the argv of an external
   command.**  An argv value is visible in the process table and in any trace
   of the command line; a shell builtin may consume the secret and hand the
   configuration over through stdin instead.

3. **A gate step is one whose effective condition is the job's success**: no
   ``if`` at all, or an ``if`` that starts with ``success()``.  Those steps
   decide whether the job is green, so they must not be relaxed with
   ``continue-on-error``.  Steps that spell ``always()`` are instrumentation
   and may be relaxed.

The condition-reader tail policy, the dev-track mover policy and the release
and wheel publication policy readers were read from the Forgejo-era workflows
and retired with those workflow files (PR #78); they are no longer here.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = ROOT / ".github" / "workflows"


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
    """Every job that gets a runner -- the complete current workflow set."""

    import yaml

    jobs: list[JobRef] = []
    for path in workflow_files():
        document = yaml.safe_load(workflow_text(path))
        for name, job in (document.get("jobs") or {}).items():
            if isinstance(job, dict) and "runs-on" in job:
                jobs.append(JobRef(path, str(name), job))
    return jobs


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


# --- policy 1: `uses:` is allowed only with a pinned ref ---------------------

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
