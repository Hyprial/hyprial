"""Normalize legacy custody keys without discarding distinct process identities."""
from __future__ import annotations

import json
from dataclasses import replace

from hyprial.kernel import ProcessLivenessProbeError, ProcessLivenessState

from .models import _OrphanEntry


def eviction_key(entry: _OrphanEntry) -> tuple[int, int]:
    """Prefer non-live entries, then the oldest first."""
    return (int(entry.state == ProcessLivenessState.ALIVE.value), entry.first_seen_ms)


def _earliest(*values: int | None) -> int | None:
    return min((value for value in values if value is not None), default=None)


def _merge(key: str, entries: list[_OrphanEntry]) -> _OrphanEntry:
    # Keep probe state/error together from the latest observation. Identity is
    # selected separately: an entirely unknown row cannot erase known custody.
    known = [entry for entry in entries
             if entry.pid is not None or entry.marker is not None]
    latest = max(known or entries, key=lambda entry: (
        entry.last_probe_ms if entry.last_probe_ms is not None else -1,
        entry.first_seen_ms,
    ))
    return replace(
        latest, orphan_id=key,
        observed=any(entry.observed for entry in entries),
        first_seen_ms=min(entry.first_seen_ms for entry in entries),
        orphan_since_ms=_earliest(*(entry.orphan_since_ms for entry in entries)),
        unknown_since_ms=_earliest(*(entry.unknown_since_ms for entry in entries)),
        gc_attempts=sum(entry.gc_attempts for entry in entries),
        probe_failures=max(entry.probe_failures for entry in entries),
        next_probe_ms=max((entry.next_probe_ms for entry in entries
                           if entry.next_probe_ms is not None), default=None),
    )


def load_entries(rows: dict[str, dict]) -> tuple[dict[str, _OrphanEntry], list[str]]:
    """Return normalized rows and old keys to delete in the same durable delta.

    One compatible known identity plus unknown rows uses the harness key. If
    legacy rows name distinct identities, retain each identity separately; the
    one-unidentified-row rule must not erase known process custody. Partial
    identities are kept separate rather than inventing a pid/marker pairing.
    """
    try:
        entries = {key: _OrphanEntry(**item) for key, item in rows.items()}
    except (TypeError, ValueError) as error:
        raise ProcessLivenessProbeError(
            f"orphan process registry contains invalid input: {error}"
        ) from error
    groups: dict[str, list[_OrphanEntry]] = {}
    deleted = []
    for key, entry in tuple(entries.items()):
        canonical = f"{entry.harness_id}:unidentified"
        if key == canonical or key.startswith(canonical + ":"):
            groups.setdefault(canonical, []).append(entries.pop(key))
            deleted.append(key)
    for canonical, group in groups.items():
        identities = {(entry.pid, entry.marker) for entry in group
                      if entry.pid is not None or entry.marker is not None}
        if len(identities) <= 1:
            entries[canonical] = _merge(canonical, group)
            continue
        buckets: dict[str, list[_OrphanEntry]] = {}
        for entry in group:
            if entry.pid is None and entry.marker is None:
                key = canonical
            elif entry.pid is not None and entry.marker is not None:
                key = f"{entry.harness_id}:{entry.pid}:{entry.marker}"
            else:
                key = f"{entry.harness_id}:partial:{json.dumps([entry.pid, entry.marker])}"
            buckets.setdefault(key, []).append(entry)
        for key, bucket in buckets.items():
            if key in entries:
                bucket.append(entries[key])
            entries[key] = _merge(key, bucket)
    return entries, [key for key in deleted if key not in entries]
