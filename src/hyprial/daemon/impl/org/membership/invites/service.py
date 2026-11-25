"""Invite decisions with read-back membership proof and resumable acceptance.

org.list only supplies deliberate leave decisions. It cannot prove a join.
The per-home/invite single flight spans fresh IPC service objects; the local
checkpoint stores progress but never grants membership or stores a bearer.
Declining remains a shell-only decision and never writes Casdoor state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from hyprial.identity import (
    InviteError, InviteView, decode_invite, invite_identifier, invite_link, select_invites,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError

from .account import snapshot_views
from .flight import single_flight
from .journal import AcceptJournal

__all__ = ["InviteService", "account_state"]

_LOG = Callable[..., None]


class InviteService:
    """The org service's invite-facing surface; the org service does the join."""

    def __init__(
        self,
        *,
        orgs: Any,
        join: Callable[[str], Mapping[str, Any]] | None = None,
        log: _LOG | None = None,
    ) -> None:
        self._orgs = orgs
        self._join = join or getattr(orgs, "accept_invite", orgs.join)
        self._log = log or (lambda *args, **kwargs: None)
        home = getattr(orgs, "_home", None)
        self._home = Path(home).resolve() if home is not None else None

    def _journal(self, identifier: str) -> AcceptJournal | None:
        return AcceptJournal(self._home, identifier) if self._home is not None else None

    def _missing(self, link: str) -> str | None:
        check = getattr(self._orgs, "invite_missing_step", None)
        return check(link) if check is not None else "membership-unverified"

    def list(self, rows: Any) -> dict[str, Any]:
        """Settle only verified complete joins; incomplete invites stay pending."""

        views = snapshot_views(rows)
        _, left = account_state(self._orgs.list())
        held: set[str] = set()
        progress: dict[str, dict] = {}
        for view, row in zip(views, rows, strict=True):
            journal = self._journal(view.invite_id)
            record = journal.record if journal is not None else {}
            try:
                missing = self._missing(invite_link(row["payload"]))
            except (InviteError, ValueError):
                missing = "invite-invalid"
            unfinished = record and record.get("state") != "done"
            if missing is None and not unfinished:
                held.add(view.space_id)
            elif record or missing in {"acl-applied", "space-bound", "register-device"}:
                progress[view.invite_id] = {
                    "status": "partial",
                    "missingStep": record.get("missingStep") or missing or record["state"],
                }
        held.difference_update(view.space_id for view in views if view.invite_id in progress)
        ordered = sorted(views, key=lambda view: view.invite_id not in progress)
        selection = select_invites(ordered, held_spaces=held, left_orgs=left)
        if selection.candidates:
            self._log("info", "org", "org.invite.pending",
                      count=len(selection.candidates), orgs=_org_names(selection.candidates))
        return {
            "ok": True,
            "pending": [row | progress.get(row["inviteId"], {})
                        for row in selection.pending_rows()],
            "settled": len(selection.settled),
            "duplicates": len(selection.duplicates),
            "warnings": [],
        }

    def accept(self, *, invite_id: object, link: object) -> dict[str, Any]:
        """Bind confirmation before coordination, then share its exact result."""

        text = _link(link)
        invitation = decode_invite(text)
        expected = invite_identifier(text)
        if invite_id != expected:
            raise ValueError("the confirmed invite does not match the link being redeemed")
        scope = str(self._home) if self._home is not None else self._orgs
        return single_flight((scope, expected), lambda: self._accept(text, invitation, expected))

    def _accept(self, text: str, invitation: Any, expected: str) -> dict[str, Any]:
        _, left = account_state(self._orgs.list())
        if invitation.org in left:
            raise ValueError("this device left the org; use explicit org join to return")
        journal = self._journal(expected)
        record = journal.record if journal is not None else {}
        if self._missing(text) is None and (not record or record.get("state") == "done"):
            if record.get("result"):
                return dict(record["result"])
            accepted = _accepted(invited=invitation, identifier=expected, joined=False, steps=[])
            if journal is not None:
                journal.start()
                journal.done(accepted)
            return accepted
        if record.get("state") == "done":
            raise OrgFsError("not-a-member", {
                "status": "partial", "missingStep": self._missing(text),
                "inviteId": expected,
            })
        if journal is not None:
            journal.start()
        try:
            result = self._join(text)
            if journal is not None:
                journal.record = journal.read()
                missing = self._missing(text)
                if missing is not None:
                    journal.begin(missing)
                    raise OrgFsError("no-holder-online", {"missingStep": missing})
        except Exception as error:
            if journal is not None:
                journal.record = journal.read()
                missing = journal.record.get("missingStep", "join")
                details = dict(getattr(error, "details", {}) or {})
                details.update(status="partial", missingStep=details.get("missingStep", missing),
                               inviteId=expected)
                error.details = details
            raise
        accepted = _accepted(invited=invitation, identifier=expected, joined=True,
                             steps=list(result.get("steps") or []))
        if journal is not None:
            journal.done(accepted)
        return accepted


def _link(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("link must be a non-empty invite link")
    return value


def _accepted(*, invited: Any, identifier: str, joined: bool,
              steps: Sequence[Any]) -> dict[str, Any]:
    return {"ok": True, "inviteId": identifier, "org": invited.org,
            "spaceId": invited.space_id, "joined": joined, "alreadyMember": not joined,
            "steps": list(steps)}


def _org_names(views: Sequence[InviteView]) -> list[str]:
    return list(dict.fromkeys(view.org for view in views))


def account_state(result: Any) -> tuple[set[str], set[str]]:
    """Listings prove leave decisions only, never completed membership."""

    if not isinstance(result, Mapping):
        raise ValueError("org.list returned a non-object result")
    if not isinstance(result.get("orgs", []), (list, tuple)):
        raise ValueError("org.list must return an orgs array")
    return set(), {org for org in result.get("leftOrgs", []) if isinstance(org, str)}
