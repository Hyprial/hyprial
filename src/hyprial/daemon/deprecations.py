"""Config fields we removed, and what to do about them.

Two audiences, one table, deliberately.

**A person** who still edits a field we dropped gets nothing back today: the
value is read, ignored, and the daemon carries on. "I changed it and nothing
happened" is the same failure this codebase keeps producing in other forms --
a switch that ships dormant, a flag whose only route to the daemon was
removed, a warning nobody can clear. Silence about a field *we* deleted is not
tolerance, it is that failure wearing a config file.

**Migration**, when it exists, needs to know exactly the same thing: which
fields went, when, and what should happen to them. Driving both from one table
means the warning and the rewrite cannot disagree -- and a deprecation cannot
be recorded without stating what migration should do with it, because the
action is not optional in the entry.

⚠️ Note what this is NOT for. A field we have simply never heard of is
tolerated in silence, and must be: it comes from a newer version, and old code
that refused to read a newer config would make every downgrade fatal while
three release tracks run side by side. The distinction is between *"I do not
know this"* and *"I removed this"* -- collapsing them is what makes silence
look like a virtue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class DeprecatedField:
    """One removed field: how to find it, what to say, what to do with it."""

    path: tuple[str, ...]
    """Where it lives in the document, outermost key first."""

    since_schema_version: int
    """The schema version that stopped honouring it."""

    guidance: str
    """What the operator should do instead.

    Required, and phrased as an instruction rather than a description. A
    notice that only says "this no longer works" leaves the reader exactly
    where they started -- still wanting the behaviour, now with no route to
    it.
    """

    action: Literal["drop", "rename"]
    """What migration does with it. Not optional: an entry that says a field
    is gone without saying what becomes of it would leave the two consumers of
    this table free to disagree."""

    rename_to: tuple[str, ...] | None = None
    """Destination for ``rename``; ``None`` for ``drop``."""

    def __post_init__(self) -> None:
        if self.action == "rename" and not self.rename_to:
            raise ValueError(f"{self.path}: rename needs rename_to")
        if self.action == "drop" and self.rename_to:
            raise ValueError(f"{self.path}: drop must not set rename_to")


# Renaming rather than reinterpreting is the house style for semantic changes:
# old code reads the old field, new code reads the new one, and the old name
# lands here. That keeps "same field, two meanings" -- the one shape tolerance
# genuinely cannot absorb -- from arising at all.
DEPRECATED_FIELDS: tuple[DeprecatedField, ...] = (
    DeprecatedField(
        path=("zenoh", "listen"),
        since_schema_version=1,
        guidance=(
            "listen is no longer persisted. The daemon derives this node's "
            "tailnet address on every start, so it cannot go stale. To pin it, "
            "set HYPRIAL_ZENOH_LISTEN in the environment that starts the daemon "
            "(launchd/systemd/shell); `hyprial init --listen` applies to a single "
            "launch."
        ),
        action="drop",
    ),
    DeprecatedField(
        path=("zenoh", "connect"),
        since_schema_version=1,
        guidance=(
            "connect is no longer persisted. Peers come from the tailnet "
            "directory on every start. To pin one, set HYPRIAL_ZENOH_CONNECT in "
            "the environment that starts the daemon; `hyprial init --connect` "
            "applies to a single launch."
        ),
        action="drop",
    ),
)


def _present(record: object, path: tuple[str, ...]) -> bool:
    """Whether the document carries a non-empty value at ``path``.

    Empty counts as absent on purpose: a field left as ``[]`` after a cleanup
    is not somebody still trying to configure something, and warning about it
    would produce a notice that following the advice cannot clear.
    """

    cursor = record
    for key in path:
        if not isinstance(cursor, dict):
            return False
        if key not in cursor:
            return False
        cursor = cursor[key]
    return bool(cursor)


def deprecation_notices(record: object) -> tuple[tuple[DeprecatedField, str], ...]:
    """Every deprecated field this document still carries, with its message.

    Returns entries rather than strings so callers can log structurally --
    and so migration, when it arrives, walks the same list rather than a
    parallel one that can fall behind.
    """

    found: list[tuple[DeprecatedField, str]] = []
    for field in DEPRECATED_FIELDS:
        if not _present(record, field.path):
            continue
        found.append((field, f"{'.'.join(field.path)}: {field.guidance}"))
    return tuple(found)
