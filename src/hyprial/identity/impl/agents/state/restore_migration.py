"""One-way migration from the retired keep-list to restore policy."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hyprial.identity.impl.agents.state.worker_state import (
    RestorePolicy,
    RestorePolicyError,
    _RestorePolicyStore,
)
from hyprial.kernel import atomic_json_write


class LegacyKeepMigrationError(ValueError):
    """The legacy keep-list could not be migrated without losing intent."""


@dataclass(frozen=True, slots=True)
class LegacyKeepMigration:
    migrated: bool
    agents: tuple[str, ...] = ()
    overrode_never: tuple[str, ...] = ()


def migrate_legacy_agent_keep_list(
    keep_path: Path,
    policy_path: Path,
    *,
    normalize: Callable[[str], str],
) -> LegacyKeepMigration:
    """Move every legacy keep entry to ``always`` before deleting the source.

    Policy is replaced atomically before the keep-list is unlinked. A crash or
    unlink failure therefore leaves a retryable source document, and repeating
    the migration writes the same policy.
    """

    keep_path = Path(keep_path)
    if not keep_path.exists():
        return LegacyKeepMigration(False)
    try:
        raw = json.loads(keep_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LegacyKeepMigrationError(
            f"cannot read legacy agent keep-list {keep_path}: {error}"
        ) from error
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise LegacyKeepMigrationError(
            "legacy agent keep-list must be a JSON string array"
        )
    try:
        actors = tuple(sorted({normalize(item) for item in raw}))
    except (ValueError, RuntimeError) as error:
        raise LegacyKeepMigrationError(
            f"invalid legacy agent keep-list entry: {error}"
        ) from error

    store = _RestorePolicyStore(Path(policy_path), normalize=normalize)
    try:
        current = store.load()
        policies = dict(current.agents)
        overrode_never = tuple(
            actor for actor in actors if policies.get(actor) == "never"
        )
        for actor in actors:
            policies[actor] = "always"
        updated = RestorePolicy(
            current.threshold_ms,
            tuple(sorted(policies.items())),
        )
        if updated != current:
            atomic_json_write(store.path, updated.to_json())
        keep_path.unlink()
    except (OSError, RestorePolicyError) as error:
        raise LegacyKeepMigrationError(
            f"cannot migrate legacy agent keep-list {keep_path}: {error}"
        ) from error
    return LegacyKeepMigration(True, actors, overrode_never)


__all__ = [
    "LegacyKeepMigration",
    "LegacyKeepMigrationError",
    "migrate_legacy_agent_keep_list",
]
