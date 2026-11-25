# Decision notes (phase B, S10; Allen 2026-11-03 "squire is an ordinary agent"):
# - A template is packaged data, read once per process: <name>/template.json
#   (schema hyprial-agent-template/v1) beside its skill file.  Code never
#   names a role; the template's cellParams carry it.
# - An agent created from a template carries the capability fact
#   template=<name> on its local record (the same kind of provenance fact as
#   creationOrigin and S8's custody).  The record is local and OrgFS-free, so
#   the agent cell's template params are always re-derivable from it.
# - template_cell_params(agent) is what the agent-cell registrar seeds: only
#   keys the cell does not hold yet, so an owner edit of the role in the cell
#   is never overwritten.  An unknown or unreadable template seeds nothing.
# - Agents created before templates existed carry no template fact.  A
#   template may name such agents as data (adopts.actor in template.json); an
#   agent without a template fact whose actor matches is read as made from
#   that template.  The match lives in the packaged data, not in code, and is
#   only a fallback: a recorded template fact always wins.  2026-11-20 (S2b):
#   no packaged template declares an adopts.actor any more; the squire
#   template that did is deleted, and the adoption read stays data-driven.
"""Packaged agent templates: name, capability facts, cell params and skill text."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from types import MappingProxyType
from typing import Any

TEMPLATE_SCHEMA = "hyprial-agent-template/v1"
TEMPLATE_CAPABILITY = "template"
_PACKAGE = "hyprial.daemon.impl.agent_templates"
_LOCK = threading.Lock()
_CACHE: dict[str, AgentTemplate] = {}

__all__ = [
    "TEMPLATE_CAPABILITY",
    "TEMPLATE_SCHEMA",
    "AgentTemplate",
    "AgentTemplateError",
    "load_template",
    "template_cell_params",
    "template_name_of",
    "template_names",
]


class AgentTemplateError(ValueError):
    """A template that does not exist or does not parse."""


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """One packaged agent template."""

    name: str
    description: str
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    cell_params: Mapping[str, Any] = field(default_factory=dict)
    skill: str | None = None
    adopts_actor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))
        object.__setattr__(self, "cell_params", MappingProxyType(dict(self.cell_params)))

    def skill_text(self) -> str:
        """The template's skill file, as packaged."""

        if self.skill is None:
            raise AgentTemplateError(f"template {self.name!r} has no skill")
        return files(_PACKAGE).joinpath(self.name).joinpath(self.skill).read_text(
            encoding="utf-8"
        )


def _parse(name: str, raw: object) -> AgentTemplate:
    if not isinstance(raw, dict) or raw.get("schema") != TEMPLATE_SCHEMA:
        raise AgentTemplateError(f"template {name!r} is not {TEMPLATE_SCHEMA}")
    if raw.get("name") != name:
        raise AgentTemplateError(f"template {name!r} names itself {raw.get('name')!r}")
    capabilities = raw.get("capabilities", {})
    cell_params = raw.get("cellParams", {})
    if not isinstance(capabilities, dict) or capabilities.get(TEMPLATE_CAPABILITY) != name:
        raise AgentTemplateError(
            f"template {name!r} must stamp capabilities.{TEMPLATE_CAPABILITY}={name!r}"
        )
    if not isinstance(cell_params, dict) or not all(isinstance(key, str) for key in cell_params):
        raise AgentTemplateError(f"template {name!r} cellParams must be an object")
    skill = raw.get("skill")
    if skill is not None and (not isinstance(skill, str) or "/" in skill or not skill):
        raise AgentTemplateError(f"template {name!r} skill must be a file name")
    adopts = raw.get("adopts", {})
    adopts_actor = adopts.get("actor") if isinstance(adopts, dict) else None
    if adopts_actor is not None and (not isinstance(adopts_actor, str) or not adopts_actor):
        raise AgentTemplateError(f"template {name!r} adopts.actor must be a name")
    description = raw.get("description", "")
    return AgentTemplate(
        name=name,
        description=description if isinstance(description, str) else "",
        capabilities=capabilities,
        cell_params=cell_params,
        skill=skill,
        adopts_actor=adopts_actor,
    )


def load_template(name: str) -> AgentTemplate:
    """The packaged template *name*; raises AgentTemplateError when absent or invalid."""

    if not isinstance(name, str) or not name or not name.replace("-", "").isalnum():
        raise AgentTemplateError(f"invalid template name {name!r}")
    with _LOCK:
        cached = _CACHE.get(name)
        if cached is not None:
            return cached
    resource = files(_PACKAGE).joinpath(name).joinpath("template.json")
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AgentTemplateError(f"template {name!r} is not available: {error}") from error
    template = _parse(name, raw)
    with _LOCK:
        _CACHE[name] = template
    return template


def template_names() -> tuple[str, ...]:
    """The names of every packaged template, sorted."""

    names = []
    for entry in files(_PACKAGE).iterdir():
        if entry.is_dir() and entry.joinpath("template.json").is_file():
            names.append(entry.name)
    return tuple(sorted(names))


def template_name_of(agent: Any) -> str | None:
    """The template *agent* was made from: its template fact, else an adopting template."""

    facts = getattr(agent, "capabilities", None)
    name = facts.get(TEMPLATE_CAPABILITY) if hasattr(facts, "get") else None
    if isinstance(name, str):
        return name
    actor = getattr(agent, "actor", None)
    if not isinstance(actor, str) or not actor:
        return None
    for candidate in template_names():
        try:
            if load_template(candidate).adopts_actor == actor:
                return candidate
        except AgentTemplateError:
            continue
    return None


def template_cell_params(agent: Any) -> Mapping[str, Any]:
    """The cell params an agent's template seeds, or an empty mapping."""

    name = template_name_of(agent)
    if name is None:
        return MappingProxyType({})
    try:
        return load_template(name).cell_params
    except AgentTemplateError:
        return MappingProxyType({})
