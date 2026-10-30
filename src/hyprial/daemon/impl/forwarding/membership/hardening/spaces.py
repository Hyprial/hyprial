"""Which org spaces the one-time directory import must leave alone.

A deleted org, a space the node has left, or a space without a single
accepted device row is not a migration candidate.  Each is reported once as
``membership.migration.skipped`` and never raises: the daemon must start.
"""
from __future__ import annotations

import json
from pathlib import Path

from hyprial.identity import ORG_META_DOC, org_from_space_name

from .left_orgs import read_left_orgs

SKIP_ORG_DELETED = 'org-deleted'
SKIP_LEFT = 'left'
SKIP_NOT_A_MEMBER = 'not-a-member'
SKIP_NO_ROWS = 'no-device-rows'
SKIP_SPACE_MISSING = 'space-missing'


def org_state(fs, space_id: str) -> str | None:
    """The explicit ``directory/org.json`` state, or None when absent/unreadable."""
    try:
        text, _version = fs.read_text(space_id, ORG_META_DOC)
        record = json.loads(text)
    except Exception as error:  # noqa: BLE001 - an unreadable marker is "no marker"
        if getattr(error, 'code', None) not in (None, 'unknown-doc'):
            raise
        return None
    state = record.get('state') if isinstance(record, dict) else None
    return state if isinstance(state, str) else None


def space_skip_reason(fs, space, home: Path, *, local_owner: str) -> str | None:
    """Return the skip reason for ``space`` before any directory row is read."""
    org = org_from_space_name(space.name)
    if org in read_left_orgs(home):
        return SKIP_LEFT
    if org_state(fs, space.space_id) == 'deleted':
        return SKIP_ORG_DELETED
    members = {member.user.removeprefix('user:') for member in fs.members(space.space_id)}
    if local_owner.removeprefix('user:') not in members:
        return SKIP_NOT_A_MEMBER
    return None


def log_skip(app, space, reason: str) -> None:
    log = getattr(app, '_log', None)
    if log is not None:
        log('info', 'zenoh', 'membership.migration.skipped', spaceId=space.space_id,
            org=org_from_space_name(space.name), reason=reason)
