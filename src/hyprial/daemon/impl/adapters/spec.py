"""Adapter intent and the one legacy harness-to-adapter migration boundary."""
from __future__ import annotations

from dataclasses import dataclass
import json

from hyprial.kernel import DesiredStateError, HarnessLaunchSpec

ADAPTER_PROVIDERS = frozenset({"lark"})
DEFAULT_ADAPTER_PROVIDER = "lark"


def is_adapter_provider(value: object) -> bool:
    return isinstance(value, str) and value in ADAPTER_PROVIDERS


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    provider: str
    name: str
    status: str = "running"
    # Canonical JSON keeps obsolete launch metadata immutable and inert.
    legacy_launch: str = "{}"

    def __post_init__(self) -> None:
        if not is_adapter_provider(self.provider):
            raise DesiredStateError("adapter has an unsupported provider")
        if not isinstance(self.name, str) or not self.name:
            raise DesiredStateError("adapter name must be a non-empty string")
        if not isinstance(self.status, str) or self.status not in {"running", "failed"}:
            raise DesiredStateError("adapter status must be running or failed")
        try:
            metadata = json.loads(self.legacy_launch)
        except (ValueError, TypeError) as error:
            raise DesiredStateError("adapter legacyLaunch must be an object") from error
        if not isinstance(metadata, dict):
            raise DesiredStateError("adapter legacyLaunch must be an object")

    @classmethod
    def from_json(cls, value: object, label: str = "adapter") -> AdapterSpec:
        if not isinstance(value, dict):
            raise DesiredStateError(f"{label} must be an object")
        if "harness" in value:
            raise DesiredStateError(f"{label}.harness is not an adapter field")
        metadata = value.get("legacyLaunch", {})
        if not isinstance(metadata, dict):
            raise DesiredStateError(f"{label}.legacyLaunch must be an object")
        return cls(value.get("provider"), value.get("name"),
                   value.get("status", "running"),
                   json.dumps(metadata, sort_keys=True, separators=(",", ":")))

    def to_json(self) -> dict[str, object]:
        return {"provider": self.provider, "name": self.name,
                **({"status": self.status} if self.status != "running" else {}),
                **({"legacyLaunch": json.loads(self.legacy_launch)}
                   if self.legacy_launch != "{}" else {})}


def split_legacy_launch_rows(rows: list) -> tuple[list, list]:
    """Validate before moving; never discard an unrecognized legacy row."""
    workers, adapters = [], []
    for index, row in enumerate(rows):
        if isinstance(row, dict) and is_adapter_provider(row.get("harness") or row.get("provider")):
            # Validate the historical launch fields without admitting adapters
            # to HarnessLaunchSpec's worker vocabulary.
            normalized = HarnessLaunchSpec.from_json(
                {**row, "harness": "pi", "provider": "pi", "headless": True},
                f"providers[{index}]",
            ).to_json()
            metadata = {k: v for k, v in {**normalized, **row, "headless": True}.items()
                        if k not in {"harness", "provider", "name", "status"}}
            adapters.append(AdapterSpec.from_json({
                "provider": row.get("harness") or row.get("provider"),
                "name": row.get("name"), "status": row.get("status", "running"),
                "legacyLaunch": metadata,
            }).to_json())
        else:
            workers.append(row)
    return workers, adapters
