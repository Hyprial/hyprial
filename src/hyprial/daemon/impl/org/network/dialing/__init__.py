"""Who dials whom: one dialer per pair of member devices (design §3).

Mutual dialing gives one zenoh transport two links, and zenoh #2806 (open)
then splits the session for good when one side's lease expires first: the
other side merges the reconnect into its stale transport and never re-sends
its declarations, so liveliness stays lost until a restart.  Reproduced 4/4
in an isolated two-node run, never with a single dialer
(notes/research-zenoh-membership-2026-10-08/v0-*).

Rule: the LATER device dials the EARLIER one.  "Earlier" is the member's
``added_at`` in an org's meta (the same value on every replica, written by
the owner at invite and kept on a re-invite), then the device id between the
devices of one user.  The owner is earliest, so every other user's devices
dial the owner's.

⭐ Uncertainty degrades to MUTUAL dialing, never to nobody dialing: mutual is
the old behaviour (with #2806's recoverable risk); nobody dialing is a pair
that stays disconnected.  So this side only accepts when, in EVERY org that
names the peer, both ``added_at`` values are known, timezone-aware, and put
this device strictly first.  A tie, an unknown or naive value, or one org
that disagrees all answer "dial".  Two sides with different partial views
can then both dial, but cannot both accept: an org both of them see gives
both the same order.

Liveness fallback: an accepting side that has not seen the peer's host
liveliness for ``fallback_after_s`` dials as well -- this covers views that
disagree inside one org (a re-invite seen first by one side) and a dialer
holding this node's old address.  It withdraws only after the peer has been
continuously live for the same period; once withdrawn, the peer must be
absent for the full period again before the fallback re-arms (hysteresis,
so a flapping peer does not flap the mapping).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Any

from hyprial.daemon.impl.org.network.binding import resolve_org_directory_space
from hyprial.daemon.impl.orgfs.api import OrgFsError

FALLBACK_AFTER_S = 120.0
_MEMBERSHIP_TTL_S = 30.0


@dataclass
class DialState:
    """Per-daemon memory across reconciles (kept on the application)."""

    answers: dict[str, tuple[str, str]] = field(default_factory=dict)
    last_live: dict[str, float] = field(default_factory=dict)
    live_since: dict[str, float] = field(default_factory=dict)
    fallback: set[str] = field(default_factory=set)
    started: float | None = None
    membership: tuple[float, Any] | None = None


def _added_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive value cannot be ordered against an aware one: unknown.
    return parsed if parsed.tzinfo is not None else None


def _membership(service: Any) -> tuple[dict[str, set[str]], dict[str, dict[str, datetime]]]:
    """Per peer label the orgs naming it, and per org each user's added_at."""

    peer_orgs: dict[str, set[str]] = {}
    added: dict[str, dict[str, datetime]] = {}
    for org in service._orgs():
        try:
            devices = service._store.list_devices(org)
            space, _reason = resolve_org_directory_space(service._fs, org)
            members = service._fs.members(space.space_id) if space is not None else ()
        except (OrgFsError, ValueError):
            continue
        added[org] = {
            member.user.removeprefix("user:"): when
            for member in members
            if (when := _added_at(member.added_at)) is not None
        }
        for device in devices:
            label = service._peer_label(device.owner, device.device_id)
            peer_orgs.setdefault(label, set()).add(org)
    return peer_orgs, added


def _order_answer(
    own: tuple[str, str],
    peer: tuple[str, str],
    orgs: set[str],
    added: dict[str, dict[str, datetime]],
) -> tuple[str, str, list[dict[str, object]]]:
    """``(answer, reason, per-org basis)`` from the org memberships alone."""

    if not orgs:
        return "dial", "no-shared-org", []
    basis: list[dict[str, object]] = []
    verdicts: list[str] = []
    for org in sorted(orgs):
        mine = added.get(org, {}).get(own[0])
        theirs = added.get(org, {}).get(peer[0])
        if mine is None or theirs is None:
            verdict = "unknown"
        elif (mine, own[1]) < (theirs, peer[1]):
            verdict = "earlier"
        elif (mine, own[1]) == (theirs, peer[1]):
            verdict = "tie"
        else:
            verdict = "later"
        verdicts.append(verdict)
        basis.append(
            {
                "org": org,
                "verdict": verdict,
                "own": mine.isoformat() if mine is not None else None,
                "other": theirs.isoformat() if theirs is not None else None,
            }
        )
    if all(verdict == "earlier" for verdict in verdicts):
        return "accept", "this-device-is-earlier", basis
    reason = next(
        (f"added-at-{v}" for v in ("unknown", "tie") if v in verdicts),
        "this-device-is-later",
    )
    return "dial", reason, basis


def _apply_fallback(
    state: DialState, label: str, live: bool, now: float, fallback_after_s: float
) -> bool:
    """Whether an accepting side dials anyway because the peer stays unseen."""

    if live:
        state.last_live[label] = now
        state.live_since.setdefault(label, now)
        if label in state.fallback and now - state.live_since[label] >= fallback_after_s:
            state.fallback.discard(label)
    else:
        state.live_since.pop(label, None)
        started = state.started if state.started is not None else now
        if now - state.last_live.get(label, started) >= fallback_after_s:
            state.fallback.add(label)
    return label in state.fallback


def dial_peers(
    service: Any,
    *,
    state: DialState | None = None,
    log: Callable[..., None] | None = None,
    is_live: Callable[[str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    fallback_after_s: float = FALLBACK_AFTER_S,
) -> dict[str, str]:
    """The peers this node dials, out of ``service.directory_peers()``.

    ``state`` (caller-owned, kept across calls) holds the liveness fallback
    and the last answer per peer, so ``org.dial_direction.resolved`` is
    logged on a change only.  ``is_live(device_id)`` reads host liveliness;
    without it the fallback never arms.
    """

    peers = service.directory_peers()
    if not peers:
        return peers
    now = clock()
    if state is not None and state.started is None:
        state.started = now
    cached = state.membership if state is not None else None
    if cached is not None and now - cached[0] < _MEMBERSHIP_TTL_S:
        peer_orgs, added = cached[1]
    else:
        peer_orgs, added = _membership(service)
        if state is not None:
            state.membership = (now, (peer_orgs, added))
    own = (service._owner, service._node_id)
    chosen: dict[str, str] = {}
    for label, address in peers.items():
        peer_user, _, peer_device = label.partition("/")
        orgs = peer_orgs.get(label, set())
        answer, reason, basis = _order_answer(own, (peer_user, peer_device), orgs, added)
        if answer == "accept" and state is not None and is_live is not None:
            if _apply_fallback(state, label, bool(is_live(peer_device)), now, fallback_after_s):
                answer, reason = "dial", "acceptor-fallback-peer-unseen"
        if answer == "dial":
            chosen[label] = address
        if state is not None and state.answers.get(label) != (answer, reason):
            state.answers[label] = (answer, reason)
            if log is not None:
                log(
                    "info",
                    "org",
                    "org.dial_direction.resolved",
                    peer=label,
                    direction=answer,
                    reason=reason,
                    deviceIds={"own": own[1], "other": peer_device},
                    basis=basis,
                )
    if state is not None:
        for gone in set(state.answers) - set(peers):
            state.answers.pop(gone, None)
            state.last_live.pop(gone, None)
            state.live_since.pop(gone, None)
            state.fallback.discard(gone)
    return chosen
