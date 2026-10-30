"""Row hygiene before ``membership-init``: refuse locally what the sidecar would refuse.

The Go store check (``validateStore``) rejects a member whose owner/deviceId is
empty, whose keys do not parse as ``nodekey:`` or are equal, and any two
active devices sharing a key (``MEMBER_CONFLICT``).  The 0.5.5 directory
keeps a stale row when a host is renamed, so the same keys appear under two
deviceIds; the newest ``updatedAt`` wins and the loser is recorded, never sent.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

NODEKEY = re.compile(r'^nodekey:[0-9a-f]{64}$')
DUPLICATE_KEY = 'DUPLICATE_KEY'
_OLDEST = (0, datetime.min.replace(tzinfo=UTC))


def validate_row(row: dict) -> str | None:
    """Return the rejection code for one accepted directory row, or None."""
    if not isinstance(row.get('owner'), str) or not row['owner']:
        return 'MISSING_OWNER'
    if not isinstance(row.get('deviceId'), str) or not row['deviceId']:
        return 'MISSING_DEVICE_ID'
    master, server = row.get('masterPublic'), row.get('serverPublic')
    if not isinstance(master, str) or not isinstance(server, str) or not NODEKEY.match(master) or not NODEKEY.match(server):
        return 'INVALID_KEY'
    if master == server:
        return 'KEYS_EQUAL'
    if not isinstance(row.get('address'), str) or not row['address']:
        return 'MISSING_ADDRESS'
    return None


def parse_updated_at(value: object) -> datetime | None:
    """The directory's ISO-8601 UTC timestamp, or None when it is corrupted."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed


def updated_at_rank(value: object) -> tuple:
    """Sort key: unparsable or missing timestamps rank as the oldest."""
    parsed = parse_updated_at(value)
    return _OLDEST if parsed is None else (1, parsed)


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep one row per connected group of rows sharing any key; newest wins.

    Rows are grouped transitively (A shares a master key with B, B shares a
    server key with C) because the sidecar's key table is global per tailnet.
    """
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owners_of_key: dict[str, int] = {}
    for index, row in enumerate(rows):
        for value in (row.get('masterPublic'), row.get('serverPublic')):
            if value in owners_of_key:
                parent[find(index)] = find(owners_of_key[value])
            else:
                owners_of_key[value] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(rows)):
        groups.setdefault(find(index), []).append(index)
    winners: dict[int, int] = {}
    for members in groups.values():
        best = max(members, key=lambda index: (updated_at_rank(rows[index].get('updatedAt')), rows[index]['deviceId']))
        for index in members:
            winners[index] = best
    kept = [row for index, row in enumerate(rows) if winners[index] == index]
    dropped = [{'deviceId': row['deviceId'], 'keptDeviceId': rows[winners[index]]['deviceId'], 'reason': DUPLICATE_KEY}
               for index, row in enumerate(rows) if winners[index] != index]
    return kept, dropped
