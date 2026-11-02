"""Validated diagnostic thresholds. These values cannot trigger execution."""
from dataclasses import dataclass, replace
import json
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping
from hyprial.kernel import parse_agent_uri


@dataclass(frozen=True, slots=True)
class RunPolicy:
    turn_long_after_ms: int | None = 1_800_000
    permission_long_after_ms: int | None = 300_000
    revision: int = 1

    def __post_init__(self):
        for value in (self.turn_long_after_ms, self.permission_long_after_ms):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("cell thresholds must be positive integer milliseconds or null")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("cell policy revision must be a positive integer")


_FIELDS = {"turnLongAfterMs": "turn_long_after_ms", "permissionLongAfterMs": "permission_long_after_ms"}


def _override(base, values):
    if not isinstance(values, dict) or set(values) - _FIELDS.keys():
        raise ValueError("unsupported cell observation policy fields")
    return replace(base, **{_FIELDS[key]: value for key, value in values.items()})


@dataclass(frozen=True)
class RunPolicies:
    default: RunPolicy
    agents: Mapping[str, RunPolicy]

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or set(payload) - {"defaults", "agents", "revision"}:
            raise ValueError("invalid cell observation configuration")
        default = _override(RunPolicy(revision=payload.get("revision", 1)), payload.get("defaults", {}))
        agents = payload.get("agents", {})
        if not isinstance(agents, dict) or any(not isinstance(key, str) or parse_agent_uri(key) is None
                                              for key in agents):
            raise ValueError("cell policy agents must be keyed by canonical actor URI")
        return cls(default, MappingProxyType({key: _override(default, value) for key, value in agents.items()}))

    @classmethod
    def load(cls, home: Path):
        """Read once at composition, never once per agent or status query."""
        path = home / "cell-observation.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {}
        return cls.from_payload(payload)

    def for_actor(self, actor):
        return self.agents.get(actor, self.default)
