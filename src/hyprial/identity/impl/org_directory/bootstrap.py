"""Declarative join workflow: the bootstrap steps an invite carries (§4.1).

An invite embeds its workflow as YAML so the invitee executes a list the
inviter declared, and both sides sha256 the exact serialized bytes — the
digest check in ``invite.py`` is only meaningful because this module's
serialization is stable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import yaml
from yaml.constructor import ConstructorError

__all__ = [
    "BOOTSTRAP_STEP_KINDS",
    "BootstrapStep",
    "bootstrap_yaml",
    "default_bootstrap",
    "parse_bootstrap_yaml",
]

StepKind = Literal["connect-inviter", "join-org", "register-device", "connect-members"]

#: The frozen vocabulary; any other ``kind`` is rejected everywhere.
BOOTSTRAP_STEP_KINDS: tuple[str, ...] = (
    "connect-inviter",
    "join-org",
    "register-device",
    "connect-members",
)


@dataclass(frozen=True, slots=True)
class BootstrapStep:
    """One declarative step; execution belongs to the invitee's daemon."""

    kind: StepKind

    def __post_init__(self) -> None:
        if self.kind not in BOOTSTRAP_STEP_KINDS:
            raise ValueError(
                f"unknown bootstrap step kind {self.kind!r}; expected one of "
                f"{BOOTSTRAP_STEP_KINDS}"
            )


def default_bootstrap() -> tuple[BootstrapStep, ...]:
    """The standard join sequence, in execution order (§2.5)."""
    return tuple(BootstrapStep(kind) for kind in BOOTSTRAP_STEP_KINDS)


def bootstrap_yaml(steps: Iterable[BootstrapStep]) -> str:
    """Stable YAML for an invite's ``workflow_yaml`` field.

    ``yaml.safe_dump`` with ``sort_keys`` makes the bytes a pure function of
    the steps, so a sha256 computed by the inviter still matches after the
    link crosses the human channel.
    """
    document = {
        "version": 1,
        "steps": [{"kind": step.kind} for step in steps],
    }
    return yaml.safe_dump(document, sort_keys=True, allow_unicode=True)


def parse_bootstrap_yaml(text: str) -> tuple[BootstrapStep, ...]:
    """Parse ``{version: 1, steps: [{kind: …}]}`` strictly.

    Anything else — a different version, extra keys, steps that are not a
    list, unknown kinds, even duplicate YAML keys — is a ``ValueError``:
    the workflow names operations a daemon will perform on an invitee's
    machine, so the accepted surface stays exactly the documented shape.
    """
    try:
        document = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"bootstrap workflow is not valid YAML: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(
            f"bootstrap workflow must be a mapping, got {type(document).__name__}"
        )
    if set(document) != {"version", "steps"}:
        raise ValueError(
            "bootstrap workflow must have exactly 'version' and 'steps', got "
            f"{sorted(document)}"
        )
    version = document["version"]
    # ``True == 1`` in Python; a YAML ``true`` must not pass as version 1.
    if isinstance(version, bool) or version != 1:
        raise ValueError(f"bootstrap workflow version must be 1, got {version!r}")
    raw_steps = document["steps"]
    if not isinstance(raw_steps, list):
        raise ValueError("bootstrap workflow 'steps' must be a list")
    steps: list[BootstrapStep] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict) or set(item) != {"kind"}:
            raise ValueError(
                f"bootstrap step #{index} must be a mapping with exactly 'kind'"
            )
        kind = item["kind"]
        if not isinstance(kind, str) or kind not in BOOTSTRAP_STEP_KINDS:
            raise ValueError(
                f"bootstrap step #{index} has unknown kind {kind!r}; expected "
                f"one of {BOOTSTRAP_STEP_KINDS}"
            )
        steps.append(BootstrapStep(kind))
    return tuple(steps)


class _UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader that refuses duplicate mapping keys.

    The same fence the org documents use (``daemon/impl/org/document.py``):
    a duplicated ``version:`` key would otherwise let a workflow carry two
    truths and have yaml silently pick one.
    """


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError(
                None, None, f"unhashable key {key!r}", key_node.start_mark
            ) from error
        if duplicate:
            raise ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)
