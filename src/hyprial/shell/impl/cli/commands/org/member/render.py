"""Human rendering for the org membership commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def render_pending(result: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for item in result.get("joined", []):
        already = " (already a member)" if item.get("alreadyMember") else ""
        lines.append(
            f"joined {item['org']} (invited by {item['inviter']}){already}"
        )
    for item in result.get("declined", []):
        lines.append(
            f"declined {item['org']} (invited by {item['inviter']}); "
            "the invite stays on the account"
        )
    for item in result.get("failed", []):
        lines.append(f"FAILED {item['org']}: {item['code']}{_partial(item)}")
    for item in result.get("unusable", []):
        lines.append(
            f"unusable invite for {item['org']}: the account row carries no payload"
        )
    for item in result.get("pending", []):
        lines.append(
            f"pending: {item['org']} (invited by {item['inviter']}, "
            f"accepted {item.get('acceptedAt') or 'unknown'}){_partial(item)}"
        )
    for warning in result.get("warnings", []):
        lines.append(f"warning: {warning}")
    if not lines:
        lines.append("no pending invites")
    return "\n".join(lines)


def _partial(item: Mapping[str, Any]) -> str:
    if item.get("status") != "partial":
        return ""
    return f"; partial (missing step: {item.get('missingStep') or 'unknown'})"
