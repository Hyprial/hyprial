# Decision notes (S3c, 2026-11-24): the users profile is deleted.  The only
# profile data with a real reader and writer, the observed runtime
# capabilities, moves here: one file state/runtime-capabilities.json holding
# version, ownerKey and capabilities, nothing else.  The old users file is never
# read or converted; probe results are re-observed
# (docs/guides/migration-remove-squire.md).  A damaged cache fails loud
# instead of reading as an empty capability set.
"""Observed runtime capabilities per owner, the probe and provider-auth cache."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from hyprial.kernel import ProcessFileLock, atomic_json_write

RUNTIME_HARNESSES = ("claude", "codex", "pi", "agy")
RUNTIME_CAPABILITY_STATUSES = ("available", "unavailable")
STORE_VERSION = 1
CHANGED_FIELD = "runtimeCapabilities"
LOCK_TIMEOUT_SECONDS = 5.0


class RuntimeCapabilityError(ValueError):
    """A capability or the capability cache is invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    """Observed availability of one harness + provider + model combination.

    This records facts only: whether the combination worked on this machine,
    why it failed when it did not, and when it was last probed.  Model
    tiering or dispatch judgement does not belong here.
    """

    harness: str
    model: str
    status: str
    probed_at: str
    provider: str | None = None
    reason: str | None = None
    detail: str | None = None
    retry_at: str | None = None

    @property
    def combo(self) -> tuple[str, str | None, str]:
        return (self.harness, self.provider, self.model)

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        harness = _string(record.get("harness"), f"{label}.harness")
        if harness not in RUNTIME_HARNESSES:
            raise RuntimeCapabilityError(
                f"{label}.harness must be one of {', '.join(RUNTIME_HARNESSES)}"
            )
        provider = _optional_string(record.get("provider"), f"{label}.provider")
        if harness == "pi" and provider is None:
            raise RuntimeCapabilityError(
                f"{label}.provider is required for the pi harness; omitting it "
                "resolves ambiguously"
            )
        status = _string(record.get("status"), f"{label}.status")
        if status not in RUNTIME_CAPABILITY_STATUSES:
            raise RuntimeCapabilityError(
                f"{label}.status must be one of {', '.join(RUNTIME_CAPABILITY_STATUSES)}"
            )
        reason = _optional_string(record.get("reason"), f"{label}.reason")
        retry_at = _optional_string(record.get("retryAt"), f"{label}.retryAt")
        if status == "available" and (reason is not None or retry_at is not None):
            raise RuntimeCapabilityError(
                f"{label}.reason and {label}.retryAt require status unavailable"
            )
        if status == "unavailable" and reason is None:
            raise RuntimeCapabilityError(f"{label}.reason is required when unavailable")
        return cls(
            harness=harness,
            provider=provider,
            model=_string(record.get("model"), f"{label}.model"),
            status=status,
            reason=reason,
            detail=_optional_string(record.get("detail"), f"{label}.detail"),
            retry_at=retry_at,
            probed_at=_string(record.get("probedAt"), f"{label}.probedAt"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "harness": self.harness,
            **({"provider": self.provider} if self.provider is not None else {}),
            "model": self.model,
            "status": self.status,
            **({"reason": self.reason} if self.reason is not None else {}),
            **({"detail": self.detail} if self.detail is not None else {}),
            **({"retryAt": self.retry_at} if self.retry_at is not None else {}),
            "probedAt": self.probed_at,
        }


class RuntimeCapabilityStore:
    """Per-owner capability facts with locked, atomic, no-op-aware writes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def list(self, owner_key: str) -> tuple[RuntimeCapability, ...]:
        with self._lock:
            return self._load().get(owner_key, ())

    def get(
        self, owner_key: str, harness: str, model: str, provider: str | None = None
    ) -> RuntimeCapability | None:
        combo = (harness, provider, model)
        return next(
            (item for item in self.list(owner_key) if item.combo == combo), None
        )

    def set(self, owner_key: str, capability: RuntimeCapability) -> tuple[str, ...]:
        """Upsert one combination; ``("runtimeCapabilities",)`` when written."""

        owner_key = _string(owner_key, "ownerKey")
        checked = RuntimeCapability.from_json(capability.to_json(), "runtimeCapability")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with ProcessFileLock(self._lock_path()) as lock:
                lock.acquire(timeout=LOCK_TIMEOUT_SECONDS)
                owners = dict(self._load())
                current = list(owners.get(owner_key, ()))
                combos = [item.combo for item in current]
                if checked.combo in combos:
                    index = combos.index(checked.combo)
                    if current[index] == checked:
                        return ()
                    current[index] = checked
                else:
                    current.append(checked)
                owners[owner_key] = tuple(current)
                atomic_json_write(self.path, _document(owners))
                return (CHANGED_FIELD,)

    def _lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    def _load(self) -> dict[str, tuple[RuntimeCapability, ...]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise RuntimeCapabilityError(
                f"runtime capability cache is unreadable: {type(error).__name__}"
            ) from error
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise RuntimeCapabilityError("runtime capability cache is not valid JSON") from error
        document = _record(raw, "runtime capability cache")
        if document.get("version") != STORE_VERSION or set(document) != {"version", "owners"}:
            raise RuntimeCapabilityError(
                f"unsupported runtime capability cache; expected version {STORE_VERSION}"
            )
        entries = document.get("owners")
        if not isinstance(entries, list):
            raise RuntimeCapabilityError("owners must be an array")
        owners: dict[str, tuple[RuntimeCapability, ...]] = {}
        for index, entry in enumerate(entries):
            label = f"owners[{index}]"
            record = _record(entry, label)
            if set(record) != {"ownerKey", "capabilities"}:
                raise RuntimeCapabilityError(f"{label} must hold only ownerKey and capabilities")
            owner_key = _string(record.get("ownerKey"), f"{label}.ownerKey")
            if owner_key in owners:
                raise RuntimeCapabilityError("runtime capability cache contains duplicate owner keys")
            values = record.get("capabilities")
            if not isinstance(values, list):
                raise RuntimeCapabilityError(f"{label}.capabilities must be an array")
            capabilities = tuple(
                RuntimeCapability.from_json(value, f"{label}.capabilities[{position}]")
                for position, value in enumerate(values)
            )
            if len({item.combo for item in capabilities}) != len(capabilities):
                raise RuntimeCapabilityError(f"{label}.capabilities contains duplicate combinations")
            owners[owner_key] = capabilities
        return owners


def _document(owners: dict[str, tuple[RuntimeCapability, ...]]) -> dict[str, object]:
    return {
        "version": STORE_VERSION,
        "owners": [
            {"ownerKey": key, "capabilities": [item.to_json() for item in owners[key]]}
            for key in sorted(owners)
        ],
    }


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimeCapabilityError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeCapabilityError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)
