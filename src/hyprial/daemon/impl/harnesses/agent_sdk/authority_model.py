"""Launch-policy digest shared by the daemon and the isolated Agent SDK worker.

Decision notes (I6-5a, relocated from ``authority_set/model.py``):

- The worker runs under ``uv run --isolated`` without the daemon's
  dependencies and executes this file by path, so it imports the standard
  library only and nothing from ``hyprial``.
- The daemon side (``cells/reconcile/policy.py``) imports the same function,
  so the digest a worker echoes in its ready frame and the digest reconcile
  expects have one definition.
- Protocol 1: the ready frame carries ``authority = {enabled, protocol,
  policyDigest, version}``; no rule text is echoed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

AUTHORITY_PROTOCOL_VERSION = 1


def policy_digest(
    *,
    harness: str,
    headless: bool,
    allow: Iterable[str],
    deny: Iterable[str],
) -> str:
    """Canonical, rule-order-independent digest of one effective launch policy."""

    payload = {
        "allow": sorted(allow),
        "deny": sorted(deny),
        "harness": harness,
        "headless": headless,
    }
    canonical = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


__all__ = ["AUTHORITY_PROTOCOL_VERSION", "policy_digest"]
