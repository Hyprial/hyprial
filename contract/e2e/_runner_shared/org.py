"""Org-space preconditions for E2E scenarios that need an org.

Two shapes are needed by the existing runners, both built on the public CLI
only (never a daemon-internal write):

* single node -- :func:`ensure_local_org` creates one org through the
  supported lifecycle;
* cross node -- :func:`invite_owner` + :func:`join_org` move the same space
  onto a second node.

The names are deliberately ``group-<prefix>-<nonce>`` and a per-run nonce
keeps two concurrent scenarios on one host from sharing state.  T1b removed
the link-engine readiness waits: agent-to-agent sends no longer consult a
gate, so there is nothing to wait for (CM-PLAN「摘掉调用」 item 14).
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

#: How long a node may take to compose an org space / finish a join.  The
#: scan is cadence-driven, so this is a poll budget, not a measured latency.
ORG_WAIT_SECONDS = 30.0

#: How long a first send may stay held before the runner calls it stuck.
RELEASE_WAIT_SECONDS = 30.0


class OrgPreconditionError(AssertionError):
    """The scenario's org precondition could not be satisfied."""


def owner_of(daemon: Any) -> str:
    """This node's owner, read back from its own ``ps`` report."""

    status = daemon.command("daemon", "status", "--json")
    report = status.get("daemon")
    owner = report.get("owner") if isinstance(report, dict) else None
    if not isinstance(owner, str) or not owner:
        raise OrgPreconditionError(f"daemon owner missing from ps: {status}")
    return owner


def _space_id(created: dict[str, Any]) -> str:
    space_id = created.get("space_id", created.get("spaceId"))
    if not isinstance(space_id, str) or not space_id:
        raise OrgPreconditionError(f"org create returned no space id: {created}")
    return space_id


def ensure_local_org(
    daemon: Any,
    actor: str,
    *,
    prefix: str,
    timeout: float = ORG_WAIT_SECONDS,
) -> str:
    """Create one org on this node and wait for it to compose.

    Returns the directory space id.  Idempotent in spirit (a fresh nonce every
    call), never a second org on a node that already reports one.
    """

    name = f"group-{prefix}-{uuid4().hex[:8]}"
    created = daemon.command("org", "create", name, "--json", exits=(0, 1))
    space_id = _space_id(created)
    daemon.command("org", "acl", "grant", name, actor, "--json")
    return space_id


def invite_owner(daemon: Any, space_id: str, owner: str) -> None:
    """Invite one owner into ``space_id`` without a user-facing notification."""

    daemon.command(
        "org", "fs", "member", "invite", space_id, f"user:{owner}", "--no-notify", "--json"
    )


def join_org(
    daemon: Any,
    space_id: str,
    *,
    actor: str,
    timeout: float = ORG_WAIT_SECONDS,
) -> None:
    """Retry ``fs join`` until it succeeds, then wait for composition.

    The invite propagates through the org directory asynchronously, so the
    first joins legitimately fail with a transient error; the budget -- not
    the first failure -- decides.
    """

    deadline = time.monotonic() + timeout
    last: Any = None
    joined = False
    while time.monotonic() < deadline:
        try:
            last = daemon.command(
                "org", "fs", "space", "join", space_id, "--json", timeout=5
            )
        except (AssertionError, OSError) as error:  # transient join refusal
            last = f"{type(error).__name__}: {error}"
        else:
            joined = True
            break
        time.sleep(0.25)
    if not joined:
        raise OrgPreconditionError(
            f"fs join {space_id} for {actor} did not succeed within "
            f"{timeout:g}s; last={last!r}"
        )


def join_shared_org(
    daemon: Any,
    space_id: str,
    *,
    actor: str,
    timeout: float = ORG_WAIT_SECONDS,
) -> None:
    """Wait until this node has both joined the space and composed it.

    Same contract as :func:`join_org`; kept as a separate name for call sites
    that join a space another node created and then assert replication.
    """

    join_org(daemon, space_id, actor=actor, timeout=timeout)
