"""Packaged routine templates; no daemon or operator state is read here."""

from importlib.resources import files

import yaml

from hyprial.routine.schema import load_routine_text
from hyprial.uri import parse_agent_uri

BUILTIN_TEMPLATES = ("selfdrive",)


def template_text(name: str) -> str:
    if name not in BUILTIN_TEMPLATES:
        raise ValueError(f"unknown routine template: {name}")
    return files(__package__).joinpath(f"{name}.yaml").read_text(encoding="utf-8")


def render_template(
    template: str,
    *,
    owner: str,
    escalate_to: str,
    name: str | None = None,
    interval: str | None = None,
    source: str | None = None,
    filter_expr: str | None = None,
    idle_threshold: str | None = None,
) -> str:
    """Render registration parameters structurally, preserving PAC placeholders.

    ``owner`` names the routine-owned coordinator. Registration ownership
    remains with the authenticated caller.
    """
    if parse_agent_uri(owner) is None:
        raise ValueError("--for must be a canonical agent:<owner>:<machine>:<actor> URI")
    if not escalate_to.startswith("user:") or not escalate_to[5:].strip() or ":" in escalate_to[5:]:
        raise ValueError("--escalate-to must be user:<owner>")
    document = yaml.safe_load(template_text(template))
    document["produces"] = owner
    document["on_task_timeout"]["escalate_to"] = escalate_to
    for route in document["policy"]["routes"]:
        if route.get("escalate_to") == "{{escalate_to}}":
            route["escalate_to"] = escalate_to
    if name is not None:
        document["name"] = name
    if interval is not None:
        document["schedule"]["interval"] = interval
    if source is not None:
        document["source"] = {"kind": source}
    if document["source"]["kind"] == "taskwarrior":
        if idle_threshold is not None:
            raise ValueError("--idle-threshold requires source pac-journal")
        document["source"]["filter"] = (
            "+selfdrive -blocked" if filter_expr is None else filter_expr
        )
    else:
        if filter_expr is not None:
            raise ValueError("--filter requires --source taskwarrior")
        if idle_threshold is not None:
            document["source"]["idle_threshold"] = idle_threshold
    text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    load_routine_text(text)
    return text
