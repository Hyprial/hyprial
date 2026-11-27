# Decision notes (service grant S5, 2026-11-26):
# - A derived source contributes catalog entries and protected devices the way
#   a hand-placed catalog entry plus a protected record would, without either
#   being written: the source re-derives them from the org directory and keeps
#   the result in memory only.
# - Source precedence is fixed and the existing rule is untouched: a
#   hand-placed OrgFS entry beats a shipped declaration, and both beat a
#   derived entry. A derived entry that collides with an occupied service name
#   takes its alternative name; one that still collides is not merged at all,
#   and the source is told so it can report the grant.
# - A hand-placed protected device beats a derived device with the same device
#   id. Two derived devices that disagree about one device id would be a
#   conflict with no fair winner: the source never contributes both (it
#   rejects every grant that depends on such a device instead).
# - A derived entry only stands in for a home whose protected record is
#   missing. A record that exists but cannot be read or validated keeps
#   failing closed, exactly as before. With no record and a source, the
#   access snapshot is the derived devices even when there are none: an
#   empty one still lets a reconciliation withdraw the connections whose
#   grants are gone, where "no protected record" alone would stop earlier.
# - A connection is "derived" when the source remembers its name. When its
#   entry is gone, or its name now belongs to a hand-placed entry, the
#   manager withdraws it through the ordinary disconnect path instead of
#   leaving the failed row a vanished hand-placed entry gets.
# - No event name is added to the frozen service.connect.* set: a failing
#   source is diagnosed by the source itself, and service-connect only makes
#   sure one failure does not stop reconciliation.
# - Without a source nothing changes: every function takes the source as an
#   argument and answers "nothing to do" for None, so a manager constructed
#   the old way behaves exactly as before.
# - S5 fix (2026-11-27, critic 4): the alternative name always differs from
#   the name and is a valid service name. ``<name>-<grantor slug>`` is kept
#   when it fits; otherwise the name is shortened to make room for a suffix
#   of at most 16 characters. A grantor with no slug character gets an
#   8-hex-digit SHA-256 digest of its owner instead, and a shortened name
#   that would read back as the name itself falls back to that digest.
"""Second source for service-connect: entries and devices derived from grants."""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Collection
from pathlib import Path
from typing import Protocol

from .models import AccessSnapshot, CatalogEntry, ProtectedDevice, RegistrySnapshot


_SLUG = re.compile(r"[^a-z0-9_-]+")
_NAME_LIMIT = 63
_SUFFIX_LIMIT = 16
_DIGEST_LENGTH = 8


class DerivedSource(Protocol):
    """What service-connect needs from a second, non-persistent source."""

    def refresh(self) -> None:
        """Re-derive after the directory changed; bounded, and never raises."""

    def entries(self) -> tuple[DerivedEntry, ...]:
        """The catalog entries this source currently contributes."""

    def devices(self) -> tuple[ProtectedDevice, ...]:
        """The protected devices this source currently contributes."""

    def connected(self, name: str) -> None:
        """A connection now maps the derived entry called ``name``."""

    def forget(self, name: str) -> None:
        """The connection called ``name`` is gone; stop remembering it."""

    def retain(self, names: Collection[str]) -> None:
        """Keep remembering only these connections; every other one is gone."""

    def remembers(self, name: str) -> bool:
        """Whether ``name`` is a connection this source derived, now or earlier."""

    def unresolved(self, names: tuple[str, ...]) -> None:
        """Derived entries that could not be merged because both names are taken."""


@dataclasses.dataclass(frozen=True, slots=True)
class DerivedEntry:
    """One derived catalog entry with the name it takes when its own is occupied."""

    name: str
    alternative: str
    entry: CatalogEntry


def alternative_name(name: str, grantor: str) -> str:
    """``<name>-<grantor>`` as a valid service name that always differs from ``name``."""

    digest = hashlib.sha256(grantor.encode("utf-8", "surrogatepass")).hexdigest()[:_DIGEST_LENGTH]
    slug = _SLUG.sub("-", grantor.lower()).strip("-")
    for suffix in (slug, digest):
        if not suffix:
            continue
        if len(name) + 1 + len(suffix) <= _NAME_LIMIT:
            return f"{name}-{suffix}"
        suffix = suffix[:_SUFFIX_LIMIT].rstrip("-") or digest
        shortened = f"{name[: _NAME_LIMIT - 1 - len(suffix)]}-{suffix}"
        if shortened != name:
            return shortened
    return name[:-1] + ("0" if name[-1] != "0" else "1")


def merge_entries(
    snapshot: RegistrySnapshot, source: DerivedSource
) -> tuple[RegistrySnapshot, frozenset[str]]:
    """Add the derived entries to a loaded catalog under the fixed precedence.

    Returns the merged catalog and the final names under which the derived
    entries were merged. ``conflicts`` keeps its meaning (OrgFS document over
    a shipped declaration) and is passed through untouched: a renamed derived
    entry is not such a conflict.
    """

    entries = {entry.name: entry for entry in snapshot.entries}
    unresolved: list[str] = []
    merged: set[str] = set()
    for item in source.entries():
        name = next((value for value in (item.name, item.alternative) if value not in entries), None)
        if name is None:
            unresolved.append(item.name)
            continue
        entries[name] = dataclasses.replace(item.entry, name=name)
        merged.add(name)
    if unresolved:
        source.unresolved(tuple(unresolved))
    return RegistrySnapshot(
        tuple(entries[key] for key in sorted(entries)),
        snapshot.source,
        snapshot.space_id,
        snapshot.owner,
        snapshot.error,
        snapshot.cache,
        snapshot.conflicts,
    ), frozenset(merged)


def with_derived_devices(access: AccessSnapshot, source: DerivedSource) -> AccessSnapshot:
    """Hand-placed devices plus derived ones; the hand-placed one wins per device id."""

    derived = source.devices()
    if not derived:
        return access
    devices = {device.device_id: device for device in access.devices}
    for device in derived:
        devices.setdefault(device.device_id, device)
    return AccessSnapshot(access.catalog_trust, tuple(devices.values()))


def record_absent(path: Path) -> bool:
    """Whether the protected record is definitely missing (not merely unreadable)."""

    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def derived_access(source: DerivedSource | None, path: Path) -> AccessSnapshot | None:
    """The access snapshot of a home with no protected record, when a source is configured.

    ``None`` whenever there is no source or the record exists (whatever its
    state): the caller then keeps the existing fail-closed verdict. With a
    source and no record the answer is the derived devices, even when there
    are none: an empty snapshot lets reconciliation reach the loop that
    withdraws connections whose grant is gone, instead of stopping at "no
    protected record" and leaving them failed forever.
    """

    if source is None or not record_absent(path):
        return None
    return AccessSnapshot(None, source.devices())


def is_withdrawn(
    source: DerivedSource | None, names: Collection[str], name: str, entry: CatalogEntry | None
) -> bool:
    """Whether ``name`` is a derived connection whose entry the source no longer provides."""

    if source is None or not source.remembers(name):
        return False
    return entry is None or name not in names


def refresh_quietly(source: DerivedSource | None) -> None:
    """Run the source's maintenance; one failure must not stop the caller."""

    if source is None:
        return
    try:
        source.refresh()
    except Exception:
        pass


__all__ = [
    "DerivedEntry",
    "DerivedSource",
    "alternative_name",
    "derived_access",
    "is_withdrawn",
    "merge_entries",
    "record_absent",
    "refresh_quietly",
    "with_derived_devices",
]
