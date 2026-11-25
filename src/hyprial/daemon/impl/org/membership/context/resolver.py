"""O2a/R6: explicit org, current org, unique active org, or ORG_REQUIRED.

This resolver only selects a command target. It does not change admission,
agent membership, or the distinct agent-creation fallback policy.

Decisions:
- 2026-11-01 (fix #2217/#2221, design #2078): 0.5.5.3 is an observe-only
  release, so a spaceId not governed by an active organization is passed
  through unchanged with an observe notice instead of being refused
  (``org.observe.space_passthrough``); result ``org`` may be null on that
  passthrough. Refusal stays only for a spaceId that uniquely names a
  different active org than an explicitly supplied one, and for inactive or
  malformed explicit orgs. Enforcement of the unique-active-org rule moves
  to 0.5.5.4.
- 2026-10-31 (round 2, review #2217/#2221): the explicit ``org`` argument is
  validated before any spaceId handling, so a malformed, unknown or inactive
  explicit org returns INVALID_ARGUMENT even when the spaceId would otherwise
  pass through in observe mode (contract org-network/membership.md).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.identity import OrgMembership, org_space_name
from hyprial.kernel import DaemonRequestError, ipc_errors

from .settings import read_current, write_current


class OrgContext:
    def __init__(self, home: Path, listing: Mapping[str, Any], log: Callable[..., None], *, repair_current: bool = True) -> None:
        self.home = Path(home)
        self.log = log
        self.rows = [dict(row) for row in listing["orgs"]]
        self.left = list(listing.get("leftOrgs", []))
        self.notices = list(listing.get("notices", []))
        self.active = {}
        for row in self.rows:
            name, space = row.get("org"), row.get("spaceId")
            try:
                org_space_name(name)
            except (TypeError, ValueError):
                continue
            if row.get("state") == "active" and name not in self.left and isinstance(space, str) and space:
                self.active[name] = row
        self.current = read_current(self.home)
        if self.current is not None and self.current not in self.active:
            previous = self.current
            if repair_current:
                self._change(None, reason="unavailable")
                self.notices.append(f"Cleared unavailable current org {previous}.")
            else:
                self.current = None
                self.notices.append(f"Current org {previous} is unavailable.")

    def _change(self, current: str | None, *, reason: str) -> None:
        if current == self.current:
            return
        previous = self.current
        write_current(self.home, current)
        self.current = current
        self.log("info", "org", "org.current.changed", previous=previous, current=current, reason=reason)

    def _refuse(self, message: str, *, code: str = ipc_errors.INVALID_ARGUMENT) -> None:
        raise DaemonRequestError(code, message + "; candidates: " + (", ".join(sorted(self.active)) or "none"), {"candidates": sorted(self.active), "notices": self.notices})

    def use(self, *, org: str | None = None, clear: bool = False) -> dict[str, Any]:
        if (org is None) == (not clear):
            self._refuse("specify an org or clear=true, not both")
        if not clear and org not in self.active:
            self._refuse("organization is not active on this home")
        self._change(None if clear else org, reason="explicit")
        return {"ok": True, "current": self.current, "notices": self.notices}

    def resolve(self, *, org: str | None = None, space_id: str | None = None) -> dict[str, Any]:
        selected = org
        if selected is not None and selected not in self.active:
            self._refuse("organization is not active on this home")
        if space_id is not None:
            matches = [name for name, row in self.active.items() if row["spaceId"] == space_id]
            if len(matches) == 1:
                if org is not None and org != matches[0]:
                    self._refuse("spaceId must identify the selected active organization")
                selected = matches[0]
            elif org in matches:
                selected = org
            else:
                return self._observe_space(space_id, org=org)
        if selected is None:
            selected = self.current
        if selected is None and len(self.active) == 1:
            selected = next(iter(self.active))
        if selected is None:
            self._refuse("select an organization with --org or org use", code=ipc_errors.ORG_REQUIRED)
        return {"ok": True, "org": selected, "spaceId": self.active[selected]["spaceId"], "notices": self.notices}

    def _observe_space(self, space_id: str, *, org: str | None) -> dict[str, Any]:
        self.notices.append(
            f"spaceId {space_id} is not governed by an active organization; "
            "observe mode keeps it working unchanged."
        )
        self.log("info", "org", "org.observe.space_passthrough", spaceId=space_id, org=org)
        return {"ok": True, "org": org, "spaceId": space_id, "notices": self.notices}

    def listing(self) -> dict[str, Any]:
        return {"ok": True, "orgs": [
            {"authorityDevice": None, **row, "kind": OrgMembership.org_kind(row["org"]), "current": row["org"] == self.current}
            for row in self.rows
        ], "leftOrgs": self.left, "notices": self.notices}


def for_app(app: Any, *, repair_current: bool = True) -> OrgContext:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_list

    listing = ipc_org_list(app, {})
    return OrgContext(Path(app.hyprial_home), listing, app._log, repair_current=repair_current)
