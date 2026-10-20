"""Group-scoped Casdoor membership client (design-casdoor-group-sync §4).

The owner node holds a non-admin service user whose only power is the
patched ``/api/hyprial/group-member`` endpoint for the groups it was granted:
read one user who is in the group, add one user to it, remove one user from
it.  It cannot create groups and cannot read anyone outside the group.

Credentials come from one ``KEY=VALUE`` file (0600, outside HYPRIAL_HOME)
named by the ``orgSync.casdoorCredentialFile`` key of ``settings.json``.  The
values never reach a log line or an error message.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

from hyprial.kernel import user_agent, with_user_agent

CASDOOR_ORGANIZATION = "hyprial"
REQUEST_TIMEOUT_SECONDS = 15.0
_TOKEN_MARGIN_SECONDS = 120.0
USER_AGENT = user_agent("daemon")
"""Sent on every call: the Cloudflare edge in front of the issuer rejects
urllib's default ``Python-urllib`` agent with 403 / error code 1010."""
_NOT_A_MEMBER_MSG = "the user is not a member of the group"
_UNAUTHORIZED_MSG = "Unauthorized operation"
"""What the patched endpoint answers when **no permission binds** for this
``group``.  Permissions are granted per group id, so a group that does not
exist yet matches nothing and every member call on it answers this.  See
``member_state`` for why that is read as "absent" rather than as a refusal."""
_CREDENTIAL_KEYS = (
    "CASDOOR_GROUP_SYNC_ENDPOINT",
    "CASDOOR_GROUP_SYNC_CLIENT_ID",
    "CASDOOR_GROUP_SYNC_CLIENT_SECRET",
    "CASDOOR_GROUP_SYNC_USERNAME",
    "CASDOOR_GROUP_SYNC_PASSWORD",
)

Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Any]]


class CasdoorGroupError(RuntimeError):
    """A group-membership call that did not succeed; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CasdoorGroups(Protocol):
    """What the org commands need from Casdoor: one user, one group, at a time."""

    def require_member(self, group: str, user: str) -> None: ...

    def member_state(self, group: str, user: str) -> str: ...

    def add_member(self, group: str, user: str) -> bool: ...

    def remove_member(self, group: str, user: str) -> bool: ...

    def ensure_group(self, group: str, user: str) -> dict[str, Any]: ...


def casdoor_user(member: str) -> str:
    """``user:<owner>`` (or a bare owner) -> ``hyprial/<owner>`` unchanged.

    Measured on production 2026-10-08 (infra-ops): Casdoor's ``user.name`` is
    the full ``lin.yilun@hyprial.com`` (the OIDC preferred_username), and
    ``hyprial/lin.yilun`` is "not found".
    """

    return f"{CASDOOR_ORGANIZATION}/{member.removeprefix('user:')}"


def casdoor_group(org: str) -> str:
    return f"{CASDOOR_ORGANIZATION}/{org}"


@dataclass(frozen=True)
class CasdoorCredentials:
    endpoint: str
    client_id: str
    client_secret: str
    username: str
    password: str


def read_credential_values(path: Path) -> dict[str, str]:
    """The ``KEY=VALUE`` pairs of the credential file; errors name no value.

    The file must be readable by its owner only (0600 or stricter): it holds a
    password and a client secret on a host that runs many agents.
    """

    try:
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CasdoorGroupError(
                "CASDOOR_CREDENTIAL_PERMISSIONS",
                f"the Casdoor credential file {path} must not be readable by group or others "
                f"(mode {stat.S_IMODE(mode):o}); chmod 600 it",
            )
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise CasdoorGroupError(
            "CASDOOR_CREDENTIAL_UNREADABLE",
            f"cannot read the Casdoor credential file {path}: {type(error).__name__}",
        ) from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_casdoor_credentials(path: Path) -> CasdoorCredentials:
    """Read the ``KEY=VALUE`` file; the error names keys, never values."""

    values = read_credential_values(path)
    missing = [key for key in _CREDENTIAL_KEYS if not values.get(key)]
    if missing:
        raise CasdoorGroupError(
            "CASDOOR_CREDENTIAL_INCOMPLETE",
            f"the Casdoor credential file {path} lacks: {', '.join(missing)}",
        )
    return CasdoorCredentials(
        endpoint=values["CASDOOR_GROUP_SYNC_ENDPOINT"].rstrip("/"),
        client_id=values["CASDOOR_GROUP_SYNC_CLIENT_ID"],
        client_secret=values["CASDOOR_GROUP_SYNC_CLIENT_SECRET"],
        username=values["CASDOOR_GROUP_SYNC_USERNAME"],
        password=values["CASDOOR_GROUP_SYNC_PASSWORD"],
    )


def _urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, Any]:
    """One request, no proxy (the endpoint is a tailnet host), 15 s ceiling."""

    request_headers = with_user_agent(headers, USER_AGENT)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, payload = error.code, error.read()
    except OSError as error:
        raise CasdoorGroupError(
            "CASDOOR_UNREACHABLE", f"cannot reach Casdoor: {type(error).__name__}"
        ) from error
    try:
        parsed: Any = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return status, parsed


class HttpCasdoorGroups:
    """``CasdoorGroups`` over the patched endpoint with a password-grant token."""

    def __init__(
        self,
        credentials: CasdoorCredentials,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._transport = transport or _urllib_transport
        self._clock = clock
        self._token: tuple[str, float] | None = None

    def _bearer(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._token[1]:
            return self._token[0]
        # Parameters in the POST body, never the query string (which servers
        # and proxies record).
        body = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret,
                "username": self._credentials.username,
                "password": self._credentials.password,
            }
        ).encode("ascii")
        status, parsed = self._transport(
            "POST",
            f"{self._credentials.endpoint}/api/login/oauth/access_token",
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body,
        )
        token = parsed.get("access_token") if isinstance(parsed, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            raise CasdoorGroupError(
                "CASDOOR_TOKEN_FAILED", f"the Casdoor token grant failed (HTTP {status})"
            )
        lifetime = parsed.get("expires_in")
        seconds = float(lifetime) if isinstance(lifetime, (int, float)) else 3600.0
        self._token = (token, now + max(0.0, seconds - _TOKEN_MARGIN_SECONDS))
        return token

    def _call(self, method: str, group: str, user: str) -> dict[str, Any]:
        """One group-member request; failures carry a stable code.

        Casdoor answers HTTP 200 for errors too, so only the JSON ``status``
        and ``msg`` decide.  Three messages are recognised verbatim (controllers/
        group_member.go of the group-scope patch); every other failure is
        CASDOOR_GROUP_REFUSED.
        """

        query = urllib.parse.urlencode({"user": user, "group": group})
        status, parsed = self._transport(
            method,
            f"{self._credentials.endpoint}/api/hyprial/group-member?{query}",
            {"Authorization": f"Bearer {self._bearer()}", "Accept": "application/json"},
            None,
        )
        detail = parsed.get("msg") if isinstance(parsed, dict) else None
        if status == 200 and isinstance(parsed, dict) and parsed.get("status") == "ok":
            data = parsed.get("data", {})
            return data if isinstance(data, dict) else {}
        if detail == _NOT_A_MEMBER_MSG:
            raise CasdoorGroupError("CASDOOR_NOT_A_MEMBER", f"{user} is not in Casdoor group {group}")
        if detail == f"the user: {user} is not found":
            raise CasdoorGroupError("CASDOOR_USER_NOT_FOUND", f"Casdoor has no user {user}")
        if detail == _UNAUTHORIZED_MSG:
            # Not a refusal: nothing was decided about this call.  Group-scoped
            # permissions are matched by group id, so an id that does not exist
            # matches none of them.  Measured 2026-10-20 with the configured
            # service user: an existing group (``hyprial/group-internal``)
            # answers ``member``, a missing one (``hyprial/group-probe-fixed``)
            # answers exactly this.  Callers that must be able to create the
            # group get their own code so they can tell it apart from a real
            # refusal -- and from a revoked ``group-member`` API permission,
            # which would make **every** group answer this, existing ones too.
            raise CasdoorGroupError(
                "CASDOOR_GROUP_ABSENT",
                f"{group} does not exist (or grants no permission to this service user): {detail}",
            )
        if status == 401 or (isinstance(detail, str) and detail.startswith("Access token")):
            self._token = None
        raise CasdoorGroupError(
            "CASDOOR_GROUP_REFUSED",
            f"{method} group-member for {user} in {group} failed "
            f"(HTTP {status}{': ' + str(detail) if detail else ''})",
        )

    def member_state(self, group: str, user: str) -> str:
        """``member`` / ``not-member`` / ``no-user``; anything else raises.

        ``no-user`` is answered before Casdoor's permission check and also by
        a wrong name mapping, so callers must not act on it as on
        ``not-member`` (infra-ops, 2026-10-08).

        ``CASDOOR_GROUP_ABSENT`` is **not** translated here: ``member_state``
        answers about one group's members, and a group that does not exist has
        no members to report.  ``ensure_group`` is the caller that can do
        something about it, so it decides.
        """

        try:
            member = self._call("GET", group, user)
        except CasdoorGroupError as error:
            if error.code == "CASDOOR_NOT_A_MEMBER":
                return "not-member"
            if error.code == "CASDOOR_USER_NOT_FOUND":
                return "no-user"
            raise
        groups = member.get("groups")
        if not isinstance(groups, list) or group not in groups:
            raise CasdoorGroupError(
                "CASDOOR_GROUP_REFUSED", f"group-member for {user} answered without {group}"
            )
        return "member"

    def require_member(self, group: str, user: str) -> None:
        state = self.member_state(group, user)
        if state == "not-member":
            raise CasdoorGroupError("CASDOOR_NOT_A_MEMBER", f"{user} is not in Casdoor group {group}")
        if state == "no-user":
            raise CasdoorGroupError("CASDOOR_USER_NOT_FOUND", f"Casdoor has no user {user}")

    def add_member(self, group: str, user: str) -> bool:
        return bool(self._call("POST", group, user).get("changed"))

    def remove_member(self, group: str, user: str) -> bool:
        return bool(self._call("DELETE", group, user).get("changed"))

    def ensure_group(self, group: str, user: str) -> dict[str, Any]:
        """Ask Casdoor to create the group for ``user`` if it does not exist.

        This is the only call in this client that **creates** something.  It
        goes to ``/api/hyprial/ensure-org-group`` (casdoor-config PR #12),
        which relays into the server-local org-group sidecar; the sidecar is
        the only holder of an admin credential, so this client never needs one.

        The endpoint enforces the quota: a user who already owns their allowed
        number of groups answers ``ORG_QUOTA_EXCEEDED``.  That is translated to
        its own error code here on purpose -- an operator must be able to tell
        "this user is out of quota" apart from "the group is misconfigured",
        because the two have nothing in common operationally.

        ``group`` is the ``org/name`` id this client already uses; the endpoint
        wants the bare name plus the owner id.
        """
        prefix = CASDOOR_ORGANIZATION + "/"
        bare = group[len(prefix):] if group.startswith(prefix) else group
        body = json.dumps({"org": bare, "owner": user}).encode("utf-8")
        status, parsed = self._transport(
            "POST",
            f"{self._credentials.endpoint}/api/hyprial/ensure-org-group",
            {
                "Authorization": f"Bearer {self._bearer()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body,
        )
        if status == 401 or (isinstance(parsed, dict) and parsed.get("status") != "ok"):
            detail = parsed.get("msg") if isinstance(parsed, dict) else None
            if status == 401 or (
                isinstance(detail, str) and detail.startswith("Access token")
            ):
                self._token = None
            text = str(detail or "")
            # The relay renders its own codes as "<CODE>: <detail>".
            if "ORG_QUOTA_EXCEEDED" in text:
                raise CasdoorGroupError(
                    "CASDOOR_ORG_QUOTA_EXCEEDED",
                    f"{user} is out of org quota in Casdoor: {text}",
                )
            raise CasdoorGroupError(
                "CASDOOR_ENSURE_GROUP_REFUSED",
                f"ensure-org-group for {group}/{user} failed "
                f"(HTTP {status}{': ' + text if text else ''})",
            )
        data = parsed.get("data") if isinstance(parsed, dict) else None
        return data if isinstance(data, dict) else {}
