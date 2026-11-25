"""Daemon owner settings and target identity policy.

Decision: the owner/mode/issuer record authority moved to
``hyprial.identity.impl.onboarding.identity_record`` and is consumed here
through the identity public face.  This module keeps the daemon's existing
public names as thin aliases so format, validation, and partial-commit
semantics stay identical.  Target classification remains a daemon authority.
"""

from __future__ import annotations

import os
from pathlib import Path

from hyprial.identity import (
    IdentityRecordError,
    node_owner_or_none as node_owner_or_none,
    read_settings_identity as read_settings_identity,
    read_settings_identity_metadata as read_settings_identity_metadata,
    resolve_node_owner as resolve_node_owner,
    resolve_node_user_id,
    settings_path,
    write_settings_identity as write_settings_identity,
    write_settings_owner as write_settings_owner,
)
from hyprial.kernel import uri as _uri

_settings_path = settings_path


def resolve_user_identity(
    *,
    owner: str | None,
    user_id: str | None,
    hyprial_home: Path,
    settings_issuer: str | None,
) -> tuple[str | None, str]:
    """The cell-authority uid of this serving generation and its issuer.

    Resolved once, beside the owner (contract OC0 §1.5): an explicit
    ``user_id`` wins; an explicit ``owner`` without one gives None, since
    the settings file may belong to another account; otherwise
    ``resolve_node_user_id``.  The issuer is the settings issuer only when
    the uid itself came from ``settings.userId``, else the empty string.
    An unusable uid source is None here, so the daemon still serves and only
    the cell runtime refuses (``authority-uid-missing``); ``resolve_node_user_id``
    called directly names the exact record error.
    """

    if user_id is not None:
        return user_id, ""
    if owner is not None:
        return None, ""
    try:
        resolved = resolve_node_user_id(hyprial_home=hyprial_home)
    except IdentityRecordError:
        return None, ""
    if resolved is None or os.environ.get("HYPRIAL_USER_ID", "").strip():
        return resolved, ""
    return resolved, settings_issuer or ""


#: Kinds a sender can actually deliver an actor message to.  ``host`` and
#: ``unknown`` rows are observability, never advertisement.
DELIVERABLE_TARGET_KINDS = frozenset(
    {
        _uri.TARGET_KIND_AGENT,
        _uri.TARGET_KIND_USER,
    }
)


def normalize_agent_recipient(target: str) -> str:
    """Resolve the ``agent:<node>`` display form to the bare node name.

    ``hyprial targets`` used to advertise node targets as ``agent:<node>`` so
    the kind looked explicit, but liveliness, outbox records, and delivery
    keys all use the bare node name.  Normalizing here — before the value
    reaches the inbox service — keeps one persisted recipient form so
    dedup, custody, and the DLQ never split.  Canonical interactive actor
    URIs (``agent:<owner>:<machine>:<agent>``) are real identities
    registered in presence, not display decoration, and pass through
    untouched, as do ``user:`` targets.
    """

    if target.startswith(_uri.AGENT_URI_PREFIX):
        remainder = target.removeprefix(_uri.AGENT_URI_PREFIX)
        if remainder and ":" not in remainder:
            return remainder
    return target


def classify_target_identity(value: str) -> str:
    """Classify one network identity into Allen's object types.

    Rules, in order:

    * ``agent:<owner>:<machine>:<actor>`` (exactly four non-empty segments)
      is a connector -- ``agent``.
    * ``agent:<name>`` (single segment) and bare names with no ``:`` are
      node-id-shaped: every daemon announces its bare ``node_id`` under the
      actor liveliness keyspace, and pre-canonical sessions could persist
      the same shape.  Neither is a verified connector, so both downgrade
      to ``host`` -- never advertised as an agent, never rejected.
    * ``user:<owner>`` is a human recipient -- ``user``.
    * ``adapter:<adapter>:<name>`` reply-bridge addresses and the retired
      ``route:<adapter>:<route>`` (I4b) are NOT delivery targets, so they
      classify ``unknown``.
    * Everything else (``agent:`` with an empty name, three/five-segment
      ``agent:`` shapes, empty segments, unknown schemes, the empty
      string) is ``unknown``.  Unknown is a deliberate, test-covered
      verdict: when in doubt, downgrade the label, do not reject.
    """

    if not value or not value.strip():
        return _uri.TARGET_KIND_UNKNOWN
    if ":" not in value:
        return _uri.TARGET_KIND_HOST
    if _uri.parse_agent_uri(value) is not None:
        return _uri.TARGET_KIND_AGENT
    scheme, _, remainder = value.partition(":")
    if scheme == "agent":
        if not remainder:
            return _uri.TARGET_KIND_UNKNOWN
        if ":" not in remainder:
            return _uri.TARGET_KIND_HOST
        return _uri.TARGET_KIND_UNKNOWN
    if scheme == "user":
        if remainder and ":" not in remainder:
            return _uri.TARGET_KIND_USER
        return _uri.TARGET_KIND_UNKNOWN
    return _uri.TARGET_KIND_UNKNOWN


def target_deliverable(kind: str) -> bool:
    """Can a sender deliver an actor message to a target of this kind?"""

    return kind in DELIVERABLE_TARGET_KINDS
