"""Common workflow-tree policy readers for the GitHub merge gate.

Rules 1–9 live here because they inspect generic workflow syntax and runner
hygiene. Rules 10–17 are kept in gate_scope_policy.py, which imports this
module for shared document readers and policy data.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import yaml

import tests.support.ci.ci_workflow_policy as tree
from tests.support.ci_download.runners import GITHUB_HOSTED_VM_LABELS, matrix_runs_on_values

GATE_FILE = "gate.yml"
PR_HYGIENE_FILE = "pr-hygiene.yml"
REQUIRED_CHECK_OWNERS = {"gate": GATE_FILE, "pr-hygiene": PR_HYGIENE_FILE}
STRICT_FILES = frozenset(REQUIRED_CHECK_OWNERS.values())
GITHUB_HOSTED_RUNNER_LABELS = GITHUB_HOSTED_VM_LABELS | {"ubuntu-latest"}
POWERSHELL_SHELLS = frozenset({"pwsh", "powershell", "pwsh.exe", "powershell.exe"})
POSIX_SHELLS = frozenset({"bash", "sh", "bash.exe", "sh.exe"})
WINDOWS_DEFAULT_SHELL = "pwsh"
POSIX_DEFAULT_SHELL = "bash"
ARTIFACT_ACTIONS = frozenset({"actions/upload-artifact", "actions/download-artifact"})
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY")
_PROXY_NAMES_FOLDED = frozenset(name.casefold() for name in PROXY_VARIABLES)
MINIMAL_TOKEN = {"contents": "read"}
_SHA40 = re.compile(r"[0-9a-f]{40}")


class Finding(NamedTuple):
    """A policy violation with its workflow file and line."""

    file: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.message}"


def gate_path() -> Path:
    return tree.WORKFLOW_DIR / GATE_FILE


def pr_hygiene_path() -> Path:
    return tree.WORKFLOW_DIR / PR_HYGIENE_FILE


WORKFLOW_DIR = tree.WORKFLOW_DIR


def workflow_files() -> list[Path]:
    shared = tree.WORKFLOW_DIR
    tree.WORKFLOW_DIR = WORKFLOW_DIR
    try:
        return tree.workflow_files()
    finally:
        tree.WORKFLOW_DIR = shared


def compose(text: str) -> Any:
    return yaml.compose(text)


def load(text: str) -> dict[str, Any]:
    return yaml.safe_load(text) or {}


def mapping(node: Any) -> dict[Any, Any]:
    return {key: value for key, value in node.value} if isinstance(node, yaml.MappingNode) else {}


def line_of(node: Any) -> int:
    return node.start_mark.line + 1 if node is not None else 0


def keyed(node: Any) -> dict[str, Any]:
    return {str(key.value): value for key, value in mapping(node).items()}


def triggers(document: dict[str, Any]) -> Any:
    if "on" in document:
        return document["on"]
    return next((value for key, value in document.items() if key is True), None)


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
    return {str(name): job for name, job in (document.get("jobs") or {}).items() if isinstance(job, dict)}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def runner_labels(runs_on: Any) -> list[str]:
    labels: list[str] = []
    for item in as_list(runs_on):
        if isinstance(item, dict):
            labels.extend(f"{key}={value}" for key, value in item.items())
        else:
            labels.append(str(item))
    return labels


def case_folded_duplicates(env_node: Any) -> list[tuple[str, str, int]]:
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
    return [Finding(name, line, f"env block {where} sets {first!r} and {second!r}: environment variable names are case-insensitive on Windows") for where, node in env_mappings(compose(text)) for first, second, line in case_folded_duplicates(node)]


def artifact_action_major(uses: str) -> int | None:
    if uses.partition("@")[0].strip() not in ARTIFACT_ACTIONS:
        return None
    digits = ""
    for character in uses.partition("@")[2].lstrip("vV"):
        if not character.isdigit():
            break
        digits += character
    return int(digits) if digits else 0


def step_uses(text: str) -> Iterator[tuple[str, int, str]]:
    for job_name, job_node in keyed(keyed(compose(text)).get("jobs")).items():
        steps = keyed(job_node).get("steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue
        for step_node in steps.value:
            uses = keyed(step_node).get("uses")
            if uses is not None:
                yield job_name, line_of(uses), str(uses.value)


def all_uses(document: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for job in jobs_of(document).values():
        if "uses" in job:
            found.append(str(job["uses"]))
        found.extend(str(step["uses"]) for step in job.get("steps") or [] if isinstance(step, dict) and "uses" in step)
    return found


def retired_artifact_offenders(text: str, name: str) -> list[Finding]:
    return [Finding(name, line, f"job {job} uses {uses}: the artifact actions v1 to v3 are shut down on GitHub") for job, line, uses in step_uses(text) if artifact_action_major(uses) in {1, 2, 3}]


def is_local_action(uses: str) -> bool:
    return uses.startswith("./") or uses.startswith("docker://")


def unpinned_action_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    found: list[Finding] = []
    for job_name, job in jobs_of(document).items():
        candidates = ([str(job["uses"])] if "uses" in job else []) + [str(step["uses"]) for step in job.get("steps") or [] if isinstance(step, dict) and "uses" in step]
        for uses in candidates:
            if not is_local_action(uses) and not _SHA40.fullmatch(uses.rpartition("@")[2] if "@" in uses else ""):
                found.append(Finding(name, 0, f"job {job_name} uses {uses!r}: an external action must be pinned to a full 40-character commit SHA"))
    return found


def permissions_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    declared = document.get("permissions")
    if declared is None:
        return [Finding(name, 0, "declares no top-level permissions block")]
    if isinstance(declared, str):
        if declared == "none":
            return []
        if declared == "read-all":
            return [Finding(name, 0, "top-level permissions 'read-all' is a wildcard: name the scopes the workflow reads")]
        return [Finding(name, 0, f"top-level permissions {declared!r} is not a minimal token")]
    if not isinstance(declared, dict):
        return [Finding(name, 0, "top-level permissions has an unreadable shape")]
    return [Finding(name, 0, f"top-level permissions grants {scope}: {level}, not read") for scope, level in declared.items() if not isinstance(level, str) or level not in {"read", "none"}]


def pull_request_target_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    return [Finding(name, 0, "uses the pull_request_target trigger, which runs untrusted pull-request code with the base repository's token")] if "pull_request_target" in trigger_names(document) else []


def unhosted_runner_offenders(document: dict[str, Any], name: str) -> list[Finding]:
    if "pull_request" not in trigger_names(document):
        return []
    found: list[Finding] = []
    for job_name, job in jobs_of(document).items():
        if "uses" in job and "runs-on" not in job:
            continue
        expanded = matrix_runs_on_values(job.get("runs-on"), job)
        labels = list(expanded) if expanded is not None and all(isinstance(item, str) for item in expanded) else runner_labels(job.get("runs-on"))
        found.extend(Finding(name, 0, f"job {job_name} runs-on {label!r}: a workflow triggered by pull_request may only use a GitHub-hosted runner") for label in labels if label not in GITHUB_HOSTED_RUNNER_LABELS)
    return found


def env_mapping(document: dict[str, Any], job_name: str | None) -> dict[str, Any]:
    merged = dict(document.get("env") or {})
    if job_name is not None:
        merged.update(jobs_of(document).get(job_name, {}).get("env") or {})
    return merged


def sets_proxy(env: dict[str, Any]) -> bool:
    return any(str(value or "").strip() for key, value in env.items() if str(key).casefold() in _PROXY_NAMES_FOLDED)


def is_artifact_action(uses: str) -> bool:
    return uses.partition("@")[0].strip() in ARTIFACT_ACTIONS


def artifact_steps(text: str) -> Iterator[tuple[str, int, dict[str, Any]]]:
    for job_name, job in jobs_of(load(text)).items():
        for index, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict) and is_artifact_action(str(step.get("uses") or "")):
                yield job_name, index, step


def proxy_leaking_artifact_steps(text: str, name: str) -> list[Finding]:
    document = load(text)
    found: list[Finding] = []
    for job_name, index, step in artifact_steps(text):
        if not sets_proxy(env_mapping(document, job_name)):
            continue
        step_env = step.get("env") or {}
        by_folded: dict[str, list[Any]] = {}
        for key, value in step_env.items():
            by_folded.setdefault(str(key).casefold(), []).append(value)
        uncleared = [variable for variable in PROXY_VARIABLES if not ((values := by_folded.get(variable.casefold())) and all(value == "" for value in values))]
        if uncleared:
            found.append(Finding(name, 0, f"job {job_name} step[{index}] runs {step.get('uses')} under a workflow or job proxy and does not clear {', '.join(uncleared)}"))
    return found


def is_instrumentation(step: dict[str, Any]) -> bool:
    condition = str(step.get("if") or "").strip()
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = condition[3:-2].strip()
    return condition == "always()"


def _declares_relaxation(block: dict[str, Any]) -> bool:
    value = block.get("continue-on-error")
    return "continue-on-error" in block and value is not False and not (isinstance(value, str) and value.strip().lower() == "false")


def relaxed_gate_surfaces(text: str, name: str) -> list[Finding]:
    strict = name in STRICT_FILES
    found: list[Finding] = []
    for job_name, job in jobs_of(load(text)).items():
        if strict and _declares_relaxation(job):
            found.append(Finding(name, 0, f"job {job_name} is relaxed: {name} owns a required check and nothing in it may carry continue-on-error"))
        for index, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict) and _declares_relaxation(step) and (strict or not is_instrumentation(step)):
                found.append(Finding(name, 0, f"job {job_name} step[{index}] is relaxed with continue-on-error"))
    return found


def effective_shell(step: dict[str, Any], job: dict[str, Any], document: dict[str, Any]) -> str:
    declared = step.get("shell") or ((job.get("defaults") or {}).get("run") or {}).get("shell") or ((document.get("defaults") or {}).get("run") or {}).get("shell")
    if declared:
        return str(declared).split()[0].lower()
    labels = runner_labels(job.get("runs-on"))
    return WINDOWS_DEFAULT_SHELL if any(label.startswith("windows") or (label.startswith("self-hosted") and "windows" in label) for label in labels) else POSIX_DEFAULT_SHELL


def shell_family(shell: str) -> str:
    name = shell.lower().removesuffix(".exe")
    if name in POWERSHELL_SHELLS:
        return "powershell"
    if name in POSIX_SHELLS:
        return "posix"
    return "cmd" if name == "cmd" else "unknown"


def is_powershell_step(step: dict[str, Any], job: dict[str, Any], document: dict[str, Any]) -> bool:
    return "run" in step and shell_family(effective_shell(step, job, document)) == "powershell"
