"""Casdoor-first membership writes for the org commands (design §2).

Casdoor group membership is the authority; the OrgFS member tables are a
cache the owner keeps in line.  So every membership command writes Casdoor
first and stops there on failure: OrgFS is never ahead of the authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import canonical_user_uri
from hyprial.daemon.impl.org.network.casdoor.client import (
    CasdoorGroupError,
    CasdoorGroups,
    HttpCasdoorGroups,
    casdoor_group,
    casdoor_user,
    load_casdoor_credentials,
)

_GROUP_SETUP_HINT = (
    "the Casdoor group must exist and contain the org owner, and the "
    "hq-group-sync service user must hold a Group permission on it; ask "
    "infra-ops to create the group and grant it (each new org needs both)"
)


def _org_error(code: str, message: str) -> Exception:
    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    return OrgNetworkError(code, message)


class CasdoorMembershipMixin:
    """Casdoor steps the org service runs before touching OrgFS."""

    _casdoor: CasdoorGroups | None
    _owner: str
    _fs: Any
    _orgs: Any
    _require_space: Any
    _require_acl_space: Any
    _log: Any

    def _casdoor_require_group(self, org: str) -> str:
        """``org create``: the group exists and holds the owner, or fail.

        Until 2026-10-20 this only *checked*; creating the group was an
        operator's job ("ask infra-ops to create the group and grant it").  It
        now *ensures* first: a caller who is within their Casdoor quota gets
        the group created by the relay, and the check afterwards is the same
        check as before.

        Ordering matters.  The probe is not an optimisation only: it keeps the
        create path from calling a writer on every ``org create`` when nothing
        needs writing, and it means a Casdoor that is merely *slow* to answer
        ``member_state`` never gets a create request as a side effect.

        ``CASDOOR_GROUP_ABSENT`` is why this order can work at all.  Group
        permissions are matched by group id, so a group that does not exist yet
        matches none of them and the probe answers ``Unauthorized operation`` --
        the very state ``ensure_group`` exists to fix.  Before 2026-10-20 that
        answer was the catch-all ``CASDOOR_GROUP_REFUSED``, so the probe raised
        and the create step below was unreachable: ``org create`` could not
        create its first group.  The catch-all still covers transport,
        credential and genuine refusal, and those still raise here.
        """

        if self._casdoor is None:
            return "not-configured"
        group = casdoor_group(org)
        user = casdoor_user(self._owner)
        try:
            if self._casdoor.member_state(group, user) == "member":
                return "verified"
        except CasdoorGroupError as error:
            # A group that does not exist yet, or a user Casdoor does not know,
            # is exactly the state ``ensure_group`` exists to fix; any other
            # failure (transport, credential, refusal) is not ours to paper over.
            if error.code not in (
                "CASDOOR_NOT_A_MEMBER",
                "CASDOOR_USER_NOT_FOUND",
                "CASDOOR_GROUP_ABSENT",
            ):
                raise _org_error(
                    "CASDOOR_GROUP_UNAVAILABLE", f"{group}: {error}; {_GROUP_SETUP_HINT}"
                ) from error

        try:
            self._casdoor.ensure_group(group, user)
        except CasdoorGroupError as error:
            if error.code == "CASDOOR_ORG_QUOTA_EXCEEDED":
                # Its own code: "out of quota" and "the group is broken" need
                # different answers from the person reading the error.
                raise _org_error(
                    "CASDOOR_ORG_QUOTA_EXCEEDED",
                    f"{user} cannot create another organisation: {error}",
                ) from error
            raise _org_error(
                "CASDOOR_GROUP_UNAVAILABLE", f"{group}: {error}; {_GROUP_SETUP_HINT}"
            ) from error

        try:
            self._casdoor.require_member(group, user)
        except CasdoorGroupError as error:
            raise _org_error(
                "CASDOOR_GROUP_UNAVAILABLE", f"{group}: {error}; {_GROUP_SETUP_HINT}"
            ) from error
        return "verified"

    def _casdoor_add(self, org: str, member: str) -> bool:
        """``org invite``: add to the Casdoor group before any OrgFS write.

        Returns whether this call changed the group (False when the user was
        already in it, or Casdoor is not configured).
        """

        if self._casdoor is None:
            return False
        try:
            return self._casdoor.add_member(casdoor_group(org), casdoor_user(member))
        except CasdoorGroupError as error:
            if error.code == "CASDOOR_USER_NOT_FOUND":
                raise _org_error(
                    "CASDOOR_USER_NOT_FOUND",
                    f"Casdoor has no user {casdoor_user(member)}; the name must be "
                    "the user's login identity, e.g. name@hyprial.com",
                ) from error
            raise _org_error(
                "CASDOOR_GROUP_WRITE_FAILED",
                f"adding {member} to the Casdoor group of {org} failed: {error}",
            ) from error

    def _invite_casdoor_first(self, org: str, space_id: str, acl_space_id: str, member: str) -> tuple[Any, Any]:
        """Add to the group, then to both OrgFS spaces; undo the add on failure.

        A member left in the group by a failed OrgFS write would hold access
        through every other application that trusts the group.
        """

        added = self._casdoor_add(org, member)
        try:
            return (
                self._fs.invite(space_id, member, "rw"),
                self._fs.invite(acl_space_id, member, "ro"),
            )
        except Exception:
            if added and self._casdoor is not None:
                try:
                    self._casdoor.remove_member(casdoor_group(org), casdoor_user(member))
                except CasdoorGroupError as undo:
                    self._log(
                        "error", "org", "org.invite.casdoor-undo-failed",
                        org=org, user=member, code=undo.code,
                    )
            raise

    def _casdoor_remove(self, org: str, member: str) -> None:
        """``org remove`` / an admitted leave: remove from Casdoor first.

        A user Casdoor does not know, or who is no longer in the group, is
        already where the removal wants them: that is success, so a member
        invited under a name Casdoor never had can still be removed.
        """

        if self._casdoor is None:
            return
        try:
            self._casdoor.remove_member(casdoor_group(org), casdoor_user(member))
        except CasdoorGroupError as error:
            if error.code in ("CASDOOR_USER_NOT_FOUND", "CASDOOR_NOT_A_MEMBER"):
                self._log(
                    "info", "org", "org.remove.casdoor-absent",
                    org=org, user=member, code=error.code,
                )
                return
            raise _org_error(
                "CASDOOR_GROUP_WRITE_FAILED",
                f"removing {member} from the Casdoor group of {org} failed: {error}",
            ) from error


    def _casdoor_removal_note(self, org: str) -> str:
        """What ``org remove`` did in Casdoor, for the command's output.

        Removing someone from the org removes them from the org's Casdoor
        group, which other applications may also use (org-improver).
        """

        if self._casdoor is None:
            return "not-configured"
        return f"removed from {casdoor_group(org)}"

    # -- OrgFS primitives the membership actor uses (design §3) -------------

    def _remove_orgfs_member(self, org: str, space_id: str, user: str) -> None:
        """Drop ``user`` from both spaces; an absent membership is success.

        Membership rows key users verbatim (usually ``user:<name>``) while
        protected rows carry the bare owner name, so both forms are tried.
        """

        candidates = [user] if user.startswith("user:") else [user, canonical_user_uri(user)]
        for candidate in candidates:
            try:
                self._fs.remove_member(space_id, candidate)
                self._remove_acl_member(org, candidate)
                break
            except OrgFsError as error:
                # "invalid-argument" means that form was not a member.
                if error.code != "invalid-argument":
                    raise

    def _remove_acl_member(self, org: str, user: str) -> None:
        acl_space = self._require_acl_space(org)
        try:
            self._fs.remove_member(acl_space.space_id, user)
        except OrgFsError as error:
            if error.code != "invalid-argument":
                raise

    def _owned_org_spaces(self) -> list[tuple[str, str]]:
        """``(org, directory space id)`` for each org this node owns."""

        me = canonical_user_uri(self._owner)
        owned: list[tuple[str, str]] = []
        for org in self._orgs():
            try:
                space = self._require_space(org)
            except OrgFsError:
                continue
            if space.owner == me:
                owned.append((org, space.space_id))
        return owned

    def _orgfs_member_users(self, space_id: str) -> list[str]:
        return sorted(member.user for member in self._fs.members(space_id))

    def _orgfs_member_added_at(self, space_id: str) -> dict[str, Any]:
        return {member.user: member.added_at for member in self._fs.members(space_id)}


class _UnusableCasdoor:
    """A configured but unreadable credential: every call fails loudly."""

    def __init__(self, error: CasdoorGroupError) -> None:
        self._error = error

    def require_member(self, group: str, user: str) -> None:
        raise self._error

    def add_member(self, group: str, user: str) -> bool:
        raise self._error

    def remove_member(self, group: str, user: str) -> bool:
        raise self._error

    def member_state(self, group: str, user: str) -> str:
        raise self._error


class CasdoorConfigError(Exception):
    """``settings.json`` names a Casdoor credential that cannot be used."""

    def __init__(self, error: CasdoorGroupError) -> None:
        super().__init__(str(error))
        self.error = error


def casdoor_credential_file(hyprial_home: Path) -> Path | None:
    """``settings.json`` ``orgSync.casdoorCredentialFile``; None when absent.

    Raises :class:`CasdoorConfigError` when the key is present but unusable.
    """

    try:
        record: Any = json.loads((Path(hyprial_home) / "settings.json").read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise CasdoorConfigError(
            CasdoorGroupError("CASDOOR_SETTINGS_UNREADABLE", f"settings.json: {type(error).__name__}")
        ) from error
    sync = record.get("orgSync") if isinstance(record, dict) else None
    path = sync.get("casdoorCredentialFile") if isinstance(sync, dict) else None
    if path is None:
        return None
    if not isinstance(path, str) or not path.strip():
        raise CasdoorConfigError(
            CasdoorGroupError(
                "CASDOOR_CREDENTIAL_UNREADABLE",
                "settings.json orgSync.casdoorCredentialFile must be a path",
            )
        )
    return Path(path).expanduser()


_CLIENTS: dict[tuple[str, int, int], HttpCasdoorGroups] = {}


def casdoor_groups_for_home(hyprial_home: Path) -> CasdoorGroups | None:
    """The client named by ``settings.json`` ``orgSync.casdoorCredentialFile``.

    Absent key: ``None`` (a node that does not own an org's membership).  A
    key whose file cannot be used yields a client that fails every call, so a
    misconfigured owner cannot silently skip the authority.
    """

    try:
        path = casdoor_credential_file(hyprial_home)
    except CasdoorConfigError as config:
        return _UnusableCasdoor(config.error)
    if path is None:
        return None
    resolved = path
    try:
        info = resolved.stat()
        key = (str(resolved), info.st_mtime_ns, info.st_size)
    except OSError:
        key = None
    cached = _CLIENTS.get(key) if key is not None else None
    if cached is not None:
        return cached  # one client per credential file keeps its token cache
    try:
        client = HttpCasdoorGroups(load_casdoor_credentials(resolved))
    except CasdoorGroupError as error:
        return _UnusableCasdoor(error)
    if key is not None:
        _CLIENTS.clear()
        _CLIENTS[key] = client
    return client
