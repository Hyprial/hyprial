"""Trusted caller classification and per-caller/purpose token buckets."""
from __future__ import annotations

from dataclasses import dataclass

from hyprial.identity import UserKey
from hyprial.kernel import canonical_user_uri, ipc_errors


@dataclass(frozen=True)
class Caller:
    classification: str
    actor: str
    orgs: frozenset[str]
    pid: int | None = None
    session_ref: str | None = None
    operator_initiated: bool = False


def authorize(caller: Caller, purpose: str, *, owner: str) -> None:
    if caller.classification == "operator" and caller.actor == canonical_user_uri(owner):
        return
    if caller.classification == "daemon":
        if purpose == "orgfs.commit-as-user":
            return
        if caller.operator_initiated and purpose in {"agent-owner.authorize", "revocation-ledger.append"}:
            return
    raise UserKey.Error(ipc_errors.CALLER_NOT_AUTHORIZED)


class RateLimiter:
    def __init__(self):
        self._buckets = {}
        self.internal_commits = 0

    def consume(self, caller: Caller, purpose: str, now: float) -> None:
        if caller.classification == "daemon" and purpose == "orgfs.commit-as-user":
            self.internal_commits += 1
            return
        capacity, seconds = (60, 3600) if purpose == "revocation-ledger.append" else (30, 60)
        key = (caller.classification, caller.actor, purpose)
        tokens, previous = self._buckets.get(key, (float(capacity), now))
        tokens = min(capacity, tokens + max(0, now - previous) * capacity / seconds)
        if tokens < 1:
            raise UserKey.Error(ipc_errors.RATE_LIMITED)
        self._buckets[key] = (tokens - 1, max(previous, now))
