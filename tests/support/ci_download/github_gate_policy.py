"""Readers for the workflow-tree policies of the GitHub merge gate.

One reader per rule, so the tests that enforce a rule cannot drift apart. The
tree is discovered (``*.yml`` and ``*.yaml``); no reader opens a workflow by
name to decide whether it is in scope. ``gate.yml`` is the merge gate today,
and a reader that only opened it would stop checking the moment a second
workflow was added.

Rules, with the reason each one exists:

1. An ``env:`` mapping must not repeat a name in another letter case. Windows
   environment variable names are case-insensitive, so ``Path`` and ``PATH``
   in one block are two variables on Linux and one on Windows. This is read
   from the composed node tree: ``yaml.safe_load`` keeps only the last of two
   equal keys and would hide the duplicate.
2. The artifact actions v1 to v3 are shut down on GitHub.
3. An external action is pinned to a full 40-character commit SHA.
4. A workflow states its token explicitly and keeps it minimal. With no
   top-level ``permissions:`` the token takes the repository default.
5. ``pull_request_target`` is never used: it runs untrusted pull-request code
   with the base repository's token.
6. A workflow triggered by ``pull_request`` runs on GitHub-hosted runners
   only. No self-hosted runner is registered to this repository. The hosted
   labels are defined once in ``runners`` and read here, so the acquisition
   audit and this check cannot disagree about which labels are hosted.
   ``ubuntu-latest`` is added here because it is a hosted VM; the audit keeps
   its older meaning of it, the internal container pool, which is why it is
   in both of that module's sets.
7. An artifact step clears the proxy variables when a workflow or job above
   it sets a proxy. With none set there is nothing to clear.
8. ``continue-on-error``: no job and no step of the merge gate is relaxed,
   because the gate is what a branch rule requires. Another workflow may relax
   a whole job; it may not relax a step.
9. The effective shell of a ``run`` step is the step's ``shell``, else the
   job's ``defaults.run.shell``, else the workflow's, else the platform
   default. A PowerShell script is not POSIX shell.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import yaml

import tests.support.ci.ci_workflow_policy as tree
from tests.support.ci_download.runners import GITHUB_HOSTED_VM_LABELS

GATE_FILE = "gate.yml"


def gate_path() -> Path:
    """The merge gate's path, resolved when called so a redirected tree is honoured."""

    return tree.WORKFLOW_DIR / GATE_FILE


def workflow_files() -> list[Path]:
    """Every workflow file of the tree this module is pointed at.

    Redirected by setting ``WORKFLOW_DIR`` on THIS module, which is what the
    tests do. The shared reader in ``ci_workflow_policy`` owns the glob, so
    it is lent this directory for the call and given its own back afterwards:
    leaving it changed would redirect every other reader in the process.
    """

    shared = tree.WORKFLOW_DIR
    tree.WORKFLOW_DIR = WORKFLOW_DIR
    try:
        return tree.workflow_files()
    finally:
        tree.WORKFLOW_DIR = shared


WORKFLOW_DIR = tree.WORKFLOW_DIR

GITHUB_HOSTED_RUNNER_LABELS = GITHUB_HOSTED_VM_LABELS | {"ubuntu-latest"}
POWERSHELL_SHELLS = frozenset({"pwsh", "powershell", "pwsh.exe", "powershell.exe"})
POSIX_SHELLS = frozenset({"bash", "sh", "bash.exe", "sh.exe"})
WINDOWS_DEFAULT_SHELL = "pwsh"
POSIX_DEFAULT_SHELL = "bash"
ARTIFACT_ACTIONS = frozenset(
    {"actions/upload-artifact", "actions/download-artifact"}
)
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
MINIMAL_TOKEN = {"contents": "read"}

_SHA40 = re.compile(r"[0-9a-f]{40}")
_LOCAL_ACTION = re.compile(r"\./")


class Finding(NamedTuple):
    """A policy violation: file, one-based line, and what is wrong."""

    file: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.message}"


def compose(text: str) -> Any:
    """The node tree of one workflow document."""

    return yaml.compose(text)


def load(text: str) -> dict[str, Any]:
    """The data of one workflow document."""

    return yaml.safe_load(text) or {}


def mapping(node: Any) -> dict[Any, Any]:
    """A mapping node as a plain dict keyed by its key NODES."""

    if isinstance(node, yaml.MappingNode):
        return {key: value for key, value in node.value}
    return {}


def line_of(node: Any) -> int:
    return node.start_mark.line + 1 if node is not None else 0


def keyed(node: Any) -> dict[str, Any]:
    """A mapping node as a dict keyed by key text."""

    return {str(key.value): value for key, value in mapping(node).items()}


def triggers(document: dict[str, Any]) -> Any:
    """The ``on:`` block. A bare ``on`` key parses as the boolean True."""

    return document.get("on", document.get(True))


def trigger_names(document: dict[str, Any]) -> set[str]:
    value = triggers(document)
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, dict):
        return {str(key) for key in value}
    return set()


def jobs_of(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(name): job
        for name, job in (document.get("jobs") or {}).items()
        if isinstance(job, dict)
    }


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def runner_labels(runs_on: Any) -> list[str]:
    """Every label a ``runs-on`` value selects, or one marker for a dynamic value.

    A mapping form (``runs-on: label: x``) and an expression are returned as
    their text, so a reader that expects known labels rejects them instead of
    silently seeing nothing.
    """

    labels: list[str] = []
    for item in as_list(runs_on):
        if isinstance(item, dict):
            labels.extend(f"{key}={value}" for key, value in item.items())
        else:
            labels.append(str(item))
    return labels


def case_folded_duplicates(env_node: Any) -> list[tuple[str, str, int]]:
    """(first key, later key, line) for keys that collide after casefold."""

    seen: dict[str, tuple[str, int]] = {}
    found: list[tuple[str, str, int]] = []
    for key in mapping(env_node):
        folded = str(key.value).casefold()
        if folded in seen:
            found.append((seen[folded][0], str(key.value), line_of(key)))
        else:
            seen[folded] = (str(key.value), line_of(key))
    return found


def env_mappings(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Every ``env:`` mapping node in the tree, with the path that leads to it."""

    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            name = str(key.value)
            child = f"{path}/{name}" if path else name
            if name == "env" and isinstance(value, yaml.MappingNode):
                yield child, value
            yield from env_mappings(value, child)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from env_mappings(item, f"{path}[]")


def case_duplicate_env_offenders(text: str, name: str) -> list[Finding]:
    found: list[Finding] = []
    for where, env_node in env_mappings(compose(text)):
        for first, second, line in case_folded_duplicates(env_node):
            found.append(
                Finding(
                    name,
                    line,
                    f"env block {where} sets {first!r} and {second!r}: environment "
                    "variable names are case-insensitive on Windows",
                )
            )
    return found


def artifact_action_major(uses: str) -> int | None:
    """The major version of an artifact action, or None when it is not one."""

    action = uses.partition("@")[0].strip()
    if action not in ARTIFACT_ACTIONS:
        return None
    ref = uses.partition("@")[2]
    digits = ""
    for character in ref.lstrip("vV"):
        if not character.isdigit():
            break
        digits += character
    return int(digits) if digits else 0


def step_uses(text: str) -> Iterator[tuple[str, int, str]]:
    """(job, line, uses) for every step that runs an action."""

    for job_name, job_node in keyed(keyed(compose(text)).get("jobs")).items():
        steps = keyed(job_node).get("steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue
        for step_node in steps.value:
            uses = keyed(step_node).get("uses")
            if uses is not None:
                yield job_name, line_of(uses), str(uses.value)


def all_uses(document: dict[str, Any]) -> list[str]:
    """Every ``uses:`` value, at job level (a reusable workflow call) and step level."""

    found: list[str] = []
    for job in jobs_of(document).values():
        if "uses" in job:
            found.append(str(job["uses"]))
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "uses" in step:
                found.append(str(step["uses"]))
    return found


def retired_artifact_offenders(text: str, name: str) -> list[Finding]:
    found: list[Finding] = []
    for job, line, uses in step_uses(text):
        if artifact_action_major(uses) in {1, 2, 3}:
            found.append(
                Finding(
                    name,
                    line,
                    f"job {job} uses {uses}: the artifact actions v1 to v3 are "
                    "shut down on GitHub",
                )
            )
    return found


def is_local_action(uses: str) -> bool:
    """``./path`` is a local action; ``docker://`` is an image, not a repository."""

    return uses.startswith("./") or uses.startswith("docker://")


def unpinned_action_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    found: list[Finding] = []
    for job_name, job in jobs_of(document).items():
        candidates = [str(job["uses"])] if "uses" in job else []
        candidates += [
            str(step["uses"])
            for step in job.get("steps") or []
            if isinstance(step, dict) and "uses" in step
        ]
        for uses in candidates:
            if is_local_action(uses):
                continue
            ref = uses.rpartition("@")[2] if "@" in uses else ""
            if not _SHA40.fullmatch(ref):
                found.append(
                    Finding(
                        name,
                        0,
                        f"job {job_name} uses {uses!r}: an external action must be "
                        "pinned to a full 40-character commit SHA",
                    )
                )
    return found


def permissions_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    found: list[Finding] = []
    declared = document.get("permissions")
    if declared is None:
        return [Finding(name, 0, "declares no top-level permissions block")]
    if isinstance(declared, str):
        if declared != "read-all" and declared != "none":
            found.append(
                Finding(
                    name,
                    0,
                    f"top-level permissions {declared!r} is not a minimal token",
                )
            )
        elif declared == "read-all":
            found.append(
                Finding(
                    name,
                    0,
                    "top-level permissions 'read-all' is a wildcard: name the "
                    "scopes the workflow reads",
                )
            )
        return found
    if not isinstance(declared, dict):
        return [Finding(name, 0, "top-level permissions has an unreadable shape")]
    for scope, level in declared.items():
        if not isinstance(level, str) or level not in {"read", "none"}:
            found.append(
                Finding(
                    name, 0, f"top-level permissions grants {scope}: {level}, not read"
                )
            )
    return found


def pull_request_target_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    if "pull_request_target" in trigger_names(document):
        return [
            Finding(
                name,
                0,
                "uses the pull_request_target trigger, which runs untrusted "
                "pull-request code with the base repository's token",
            )
        ]
    return []


def unhosted_runner_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    if "pull_request" not in trigger_names(document):
        return []
    found: list[Finding] = []
    for job_name, job in jobs_of(document).items():
        if "uses" in job and "runs-on" not in job:
            continue
        for label in runner_labels(job.get("runs-on")):
            if label in GITHUB_HOSTED_RUNNER_LABELS:
                continue
            found.append(
                Finding(
                    name,
                    0,
                    f"job {job_name} runs-on {label!r}: a workflow triggered by "
                    "pull_request may only use a GitHub-hosted runner",
                )
            )
    return found


def env_mapping(document: dict[str, Any], job_name: str | None) -> dict[str, Any]:
    """The effective ``env:`` of a job: the workflow's, then the job's on top."""

    merged: dict[str, Any] = dict(document.get("env") or {})
    if job_name is not None:
        merged.update(jobs_of(document).get(job_name, {}).get("env") or {})
    return merged


def sets_proxy(env: dict[str, Any]) -> bool:
    """Whether an env block sets a proxy variable to a non-empty value."""

    return any(str(env.get(name) or "").strip() for name in PROXY_VARIABLES)


def is_artifact_action(uses: str) -> bool:
    return uses.partition("@")[0].strip() in ARTIFACT_ACTIONS


def artifact_steps(text: str) -> Iterator[tuple[str, int, dict[str, Any]]]:
    """(job, step index, step data) for every step that runs an artifact action."""

    for job_name, job in jobs_of(load(text)).items():
        for index, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict) and is_artifact_action(str(step.get("uses") or "")):
                yield job_name, index, step


def proxy_leaking_artifact_steps(text: str, name: str) -> list[Finding]:
    """Artifact steps that inherit a proxy set above them and do not clear it.

    Only a workflow-level or job-level proxy counts. A proxy set on another
    step is not inherited, so it imposes nothing on the artifact step.
    """

    document = load(text)
    found: list[Finding] = []
    for job_name, index, step in artifact_steps(text):
        if not sets_proxy(env_mapping(document, job_name)):
            continue
        step_env = step.get("env") or {}
        uncleared = [
            variable
            for variable in PROXY_VARIABLES
            if step_env.get(variable) != ""
        ]
        if uncleared:
            found.append(
                Finding(
                    name,
                    0,
                    f"job {job_name} step[{index}] runs {step.get('uses')} under a "
                    f"workflow or job proxy and does not clear {', '.join(uncleared)}",
                )
            )
    return found


def is_instrumentation(step: dict[str, Any]) -> bool:
    """A step whose ``if:`` runs it regardless of the job's result."""

    condition = str(step.get("if") or "").strip().lstrip("${{").rstrip("}").strip()
    return condition.startswith("always()")


def _declares_relaxation(block: dict[str, Any]) -> bool:
    """True when ``continue-on-error`` is present and not a literal false."""

    if "continue-on-error" not in block:
        return False
    value = block["continue-on-error"]
    if value is False:
        return False
    if isinstance(value, str) and value.strip().lower() == "false":
        return False
    return True


def relaxed_gate_surfaces(text: str, name: str) -> list[Finding]:
    """Where ``continue-on-error`` is used in one workflow, and whether it is allowed.

    Any ``continue-on-error`` key whose value is not a literal ``false``
    (case-insensitive), including GitHub Actions expressions such as
    ``${{ ... }}``, counts as a declared relaxation. Nothing in the merge
    gate (``GATE_FILE``) may declare one at job or step level. Elsewhere a
    whole job may carry it, and a step may carry it only when it is
    instrumentation (``if: always()``); a step that decides the job's result
    may never be relaxed.
    """

    is_gate = name == GATE_FILE
    found: list[Finding] = []
    document = load(text)
    for job_name, job in jobs_of(document).items():
        if is_gate and _declares_relaxation(job):
            found.append(
                Finding(
                    name, 0, f"job {job_name} is relaxed: {GATE_FILE} is the merge "
                    "gate and nothing in it may carry continue-on-error"
                )
            )
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict) or not _declares_relaxation(step):
                continue
            if not is_gate and is_instrumentation(step):
                continue
            found.append(
                Finding(
                    name,
                    0,
                    f"job {job_name} step[{index}] is relaxed with continue-on-error",
                )
            )
    return found


def effective_shell(
    step: dict[str, Any], job: dict[str, Any], document: dict[str, Any]
) -> str:
    """The shell the runner uses for a ``run`` step, resolved in precedence order.

    The step's ``shell`` wins, then the job's ``defaults.run.shell``, then the
    workflow's ``defaults.run.shell``; with none of them the runner's default
    is bash on Linux and macOS and pwsh on Windows.
    """

    declared = step.get("shell")
    if not declared:
        job_run = (job.get("defaults") or {}).get("run") or {}
        declared = job_run.get("shell")
    if not declared:
        workflow_run = (document.get("defaults") or {}).get("run") or {}
        declared = workflow_run.get("shell")
    if declared:
        return str(declared).split()[0].lower()
    labels = runner_labels(job.get("runs-on"))
    on_windows = any(
        label.startswith("windows") or label.startswith("self-hosted") and "windows" in label
        for label in labels
    )
    return WINDOWS_DEFAULT_SHELL if on_windows else POSIX_DEFAULT_SHELL


def shell_family(shell: str) -> str:
    """``powershell``, ``posix``, ``cmd`` or ``unknown``."""

    name = shell.lower().removesuffix(".exe")
    if name in POWERSHELL_SHELLS:
        return "powershell"
    if name in POSIX_SHELLS:
        return "posix"
    if name == "cmd":
        return "cmd"
    return "unknown"


def is_powershell_step(step: dict[str, Any], job: dict[str, Any], document: dict[str, Any]) -> bool:
    if "run" not in step:
        return False
    return shell_family(effective_shell(step, job, document)) == "powershell"
