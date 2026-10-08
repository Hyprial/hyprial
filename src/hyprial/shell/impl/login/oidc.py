"""OIDC device-authorization wire protocol for ``hyprial login`` (login U2).

Pure protocol half of :mod:`hyprial.shell.impl.login.flow`: discovery validation,
device authorization, RFC 8628 token polling, UserInfo, and the shared HTTP
helper.  No credential landing, owner gates, or home writes live here; those
stay in ``flow.py``.  The module docstring's T11 anchor and the step
narrative remain with ``flow.py``.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from hyprial import __version__
from hyprial.daemon import NetworkProfile
from hyprial.daemon import url_opener
from hyprial.kernel import is_user_id_segment


class LoginError(Exception):
    """A structured login failure; ``code``/``data`` feed the CLI's JSON."""

    def __init__(
        self, code: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


# RFC 8628 device grant scope: the owner is never taken from email (D1).
LOGIN_SCOPE = "openid profile offline_access"


DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT_TYPE = "refresh_token"


USER_AGENT = f"hyprial-login/{__version__}"


_HTTP_TIMEOUT_S = 30.0


_RFC8628_DEFAULT_INTERVAL_S = 5.0
"""Used ONLY when the server omits ``interval`` (RFC 8628 §3.2's default).
Casdoor always sends it (``DeviceAuthInterval``); a server-provided value,
including 0, always wins."""

_RFC8628_SLOW_DOWN_INCREMENT_S = 5.0


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    """RFC 8628 §3.2 device authorization response."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int | None


@dataclass(frozen=True, slots=True)
class OidcEndpoints:
    """The three same-origin endpoints discovery handed out."""

    device_authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str


def _lark_union_id(payload: Mapping[str, Any], access_token: str) -> str | None:
    """Read the allowlisted Casdoor claim, preferring the issued JWT itself."""

    claims: object = None
    parts = access_token.split(".")
    if len(parts) == 3 and len(parts[1]) <= 65_536:
        try:
            encoded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(encoded))
        except (ValueError, TypeError, json.JSONDecodeError):
            claims = None
    sources = (claims, payload)
    for source in sources:
        properties = source.get("properties") if isinstance(source, dict) else None
        value = (
            properties.get("oauth_Lark_unionId")
            if isinstance(properties, dict)
            else None
        )
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _request_json(
    url: str,
    *,
    data: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, Any]:
    """One HTTP request via ``urllib.request``; returns (status, parsed body).

    ``urllib.error.HTTPError`` is unwrapped: OAuth error bodies arrive on
    4xx with a JSON payload the caller must read (Casdoor ``TokenError``).
    Every request carries ``USER_AGENT`` (this is the only urllib call site).
    """

    body = None
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", USER_AGENT)
    if data is not None:
        body = urllib.parse.urlencode(dict(data)).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=body, headers=request_headers)
    # ALL_PROXY-only shells: urllib alone would go direct (proxy_route).
    open_url, route = url_opener()
    try:
        with open_url(request, timeout=_HTTP_TIMEOUT_S) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    except urllib.error.URLError as error:
        raise LoginError(
            "ISSUER_UNREACHABLE", f"cannot reach {url}: {error.reason} ({route})"
        ) from error
    except OSError as error:
        raise LoginError(
            "ISSUER_UNREACHABLE", f"cannot reach {url}: {error} ({route})"
        ) from error
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"_raw": raw.decode("utf-8", "replace")}


def _status_hint(status: int) -> str:
    """Suffix for statuses an edge filter in front of the issuer produces.

    A 403 is almost never the issuer's own answer (Casdoor replies 200 with
    an error body); name the User-Agent the request carried, the usual
    reason such filters reject, so the report points at the real cause.
    """

    if status == 403:
        return (
            " (HTTP 403 may come from an edge filter in front of the issuer, "
            f"not the issuer itself; the request sent User-Agent {USER_AGENT!r})"
        )
    return ""


# -- discovery and endpoint validation ------------------------------------------


def _origin(url: str) -> tuple[str, str, int] | None:
    split = urlsplit(url)
    if not split.scheme or not split.hostname:
        return None
    default = {"https": 443, "http": 80}.get(split.scheme)
    port = split.port if split.port is not None else default
    if port is None:
        return None
    return (split.scheme, split.hostname.lower(), port)


def _validate_endpoints(discovery: Any, profile: NetworkProfile) -> OidcEndpoints:
    """Steps 1-2: issuer byte-equality and the same-origin endpoint rule."""

    if not isinstance(discovery, dict):
        raise LoginError("DISCOVERY_INVALID", "discovery document is not a JSON object")
    reported = discovery.get("issuer")
    if reported != profile.issuer:
        # T1: byte-for-byte — a trailing slash is a different issuer.
        raise LoginError(
            "ISSUER_MISMATCH",
            f"discovery issuer {reported!r} does not equal the profile's "
            f"issuer {profile.issuer!r} (byte-for-byte); refusing to "
            "continue",
            data={"discoveryIssuer": reported, "profileIssuer": profile.issuer},
        )
    issuer_origin = _origin(profile.issuer)
    if issuer_origin is None:
        raise LoginError(
            "PROFILE_ISSUER_INVALID",
            f"profile issuer is not an absolute URL: {profile.issuer!r}",
        )
    endpoints = {}
    for key, attribute in (
        ("device_authorization_endpoint", "device_authorization_endpoint"),
        ("token_endpoint", "token_endpoint"),
        ("userinfo_endpoint", "userinfo_endpoint"),
    ):
        value = discovery.get(key)
        if not isinstance(value, str) or not value:
            raise LoginError("DISCOVERY_INVALID", f"discovery has no usable {key}")
        if _origin(value) != issuer_origin:
            # T2: every endpoint the login flow talks to must share the
            # issuer's origin — a token or userinfo endpoint elsewhere is
            # where credentials would leak to.
            raise LoginError(
                "ENDPOINT_ORIGIN_MISMATCH",
                f"{key} {value!r} is not on the issuer's origin "
                f"{profile.issuer!r}; refusing to continue",
                data={key: value, "issuer": profile.issuer},
            )
        endpoints[attribute] = value
    return OidcEndpoints(**endpoints)


def _fetch_endpoints(profile: NetworkProfile) -> OidcEndpoints:
    url = f"{profile.issuer}/.well-known/openid-configuration"
    status, discovery = _request_json(url)
    if status != 200:
        raise LoginError(
            "DISCOVERY_UNAVAILABLE",
            f"discovery request failed with HTTP {status}: {url}"
            f"{_status_hint(status)}",
        )
    return _validate_endpoints(discovery, profile)


# -- device flow ----------------------------------------------------------------


def _request_device_authorization(
    endpoints: OidcEndpoints, profile: NetworkProfile, *, scope: str | None = None
) -> DeviceAuthorization:
    status, payload = _request_json(
        endpoints.device_authorization_endpoint,
        data={"client_id": profile.client_id, "scope": scope or LOGIN_SCOPE},
    )
    error = _token_error(status, payload, where="device authorization")
    if error is not None:
        raise error
    if not isinstance(payload, dict):
        raise LoginError("DEVICE_AUTH_INVALID", "device authorization body invalid")
    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    verification_uri = payload.get("verification_uri")
    for name, value in (
        ("device_code", device_code),
        ("user_code", user_code),
        ("verification_uri", verification_uri),
    ):
        if not isinstance(value, str) or not value:
            raise LoginError(
                "DEVICE_AUTH_INVALID",
                f"device authorization response has no usable {name}",
            )
    expires_in = payload.get("expires_in")
    if (
        not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or expires_in <= 0
    ):
        raise LoginError(
            "DEVICE_AUTH_INVALID",
            f"device authorization expires_in must be a positive integer; "
            f"got {expires_in!r}",
        )
    interval = payload.get("interval")
    if interval is not None and (
        not isinstance(interval, int) or isinstance(interval, bool) or interval < 0
    ):
        raise LoginError(
            "DEVICE_AUTH_INVALID",
            f"device authorization interval must be a non-negative integer; "
            f"got {interval!r}",
        )
    complete = payload.get("verification_uri_complete")
    if complete is not None and not isinstance(complete, str):
        complete = None
    return DeviceAuthorization(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=complete or None,
        expires_in=expires_in,
        interval=interval,
    )


def _token_error(status: int, payload: Any, *, where: str) -> LoginError | None:
    """Map an OAuth error body (Casdoor ``TokenError``) onto ``LoginError``."""

    if 200 <= status < 300:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        error = payload["error"]
        description = payload.get("error_description") or ""
        detail = f"{error}: {description}" if description else error
        return LoginError(
            "TOKEN_ENDPOINT_ERROR",
            f"token endpoint rejected the {where} request ({detail})",
            data={"status": status, "error": error, "description": description},
        )
    return LoginError(
        "TOKEN_ENDPOINT_ERROR",
        f"token endpoint answered HTTP {status} for the {where} request"
        f"{_status_hint(status)}",
        data={"status": status},
    )


def _poll_device_token(
    endpoints: OidcEndpoints,
    profile: NetworkProfile,
    authorization: DeviceAuthorization,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    require_refresh: bool = True,
) -> tuple[str, str | None]:
    """Poll until the token endpoint issues tokens or a terminal state.

    Rhythm is the server's alone: ``interval`` between polls (RFC default
    only if absent), ``slow_down`` ⇒ +5s (RFC 8628 §3.5), ``expires_in`` is
    the wall-clock budget after which the command stops and asks for a
    re-run.  Terminal errors: ``access_denied``, ``expired_token``,
    ``invalid_client`` (the poll is bound to the original client id,
    Casdoor ``controllers/token.go:326-338``).

    A transport error (``ISSUER_UNREACHABLE``: connection reset, TLS EOF)
    is not a terminal state: the user may already have authorised and the
    code is still valid, so the poll waits one ``interval`` and asks again.
    The bound is the code's own ``expires_in`` -- no separate retry budget --
    and an expiry reached that way names the last transport error.
    """

    deadline = clock() + float(authorization.expires_in)
    interval = (
        float(authorization.interval)
        if authorization.interval is not None
        else _RFC8628_DEFAULT_INTERVAL_S
    )
    interval = max(interval, 0.0)
    last_transport_error: LoginError | None = None
    while True:
        if clock() >= deadline:
            cause = (
                f" (last poll failed: {last_transport_error})"
                if last_transport_error is not None
                else ""
            )
            raise LoginError(
                "DEVICE_CODE_EXPIRED",
                "the device code expired before authorization completed"
                f"{cause}; re-run hyprial login. If you signed in with Feishu, "
                "that sign-in did not approve this device: choose sign in "
                "with an existing account rather than Feishu",
            )
        try:
            status, payload = _request_json(
                endpoints.token_endpoint,
                data={
                    "grant_type": DEVICE_GRANT_TYPE,
                    "client_id": profile.client_id,
                    "device_code": authorization.device_code,
                },
            )
        except LoginError as error:
            if error.code != "ISSUER_UNREACHABLE":
                raise
            last_transport_error = error
            # A server interval of 0 must not turn a flaky edge into a tight
            # loop; fall back to the RFC 8628 default for transport retries.
            sleep(interval or _RFC8628_DEFAULT_INTERVAL_S)
            continue
        last_transport_error = None
        if 200 <= status < 300 and isinstance(payload, dict):
            access = payload.get("access_token")
            if not isinstance(access, str) or not access:
                raise LoginError(
                    "TOKEN_RESPONSE_INVALID",
                    "token response has no usable access_token",
                )
            if not require_refresh:
                return access, None
            refresh = payload.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                raise LoginError(
                    "TOKEN_RESPONSE_INVALID",
                    "token response has no refresh_token; login cannot "
                    "persist a credential without one (scope includes "
                    "offline_access)",
                )
            return access, refresh
        error = _token_error(status, payload, where="device code")
        if error is not None:
            code = error.data.get("error")
            if code == "authorization_pending":
                sleep(interval)
                continue
            if code == "slow_down":
                interval += _RFC8628_SLOW_DOWN_INCREMENT_S
                sleep(interval)
                continue
            if code == "access_denied":
                raise LoginError(
                    "ACCESS_DENIED",
                    "the authorization request was denied; nothing was written",
                    data=error.data,
                )
            if code == "expired_token":
                raise LoginError(
                    "DEVICE_CODE_EXPIRED",
                    "the device code expired; re-run hyprial login. If you "
                    "signed in with Feishu, that sign-in did not approve this "
                    "device: choose sign in with an existing account rather "
                    "than Feishu",
                    data=error.data,
                )
            raise error
        raise LoginError("TOKEN_RESPONSE_INVALID", "unexpected token response")


def _fetch_userinfo(
    endpoints: OidcEndpoints, access_token: str
) -> tuple[str, str | None, str | None]:
    """Step 6: bearer userinfo; returns username, sub, and Lark union_id."""

    status, payload = _request_json(
        endpoints.userinfo_endpoint,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if status != 200:
        error = _token_error(status, payload, where="userinfo")
        raise error or LoginError(
            "USERINFO_UNAVAILABLE",
            f"userinfo answered HTTP {status}{_status_hint(status)}",
        )
    if not isinstance(payload, dict):
        raise LoginError("USERINFO_INVALID", "userinfo response is not an object")
    username = payload.get("preferred_username")
    if not isinstance(username, str) or not username.strip():
        # Design §1.1 ⚠️: the username slot is exactly what a real UserInfo
        # must answer — absence terminates with nothing written (T6).
        raise LoginError(
            "USERNAME_MISSING",
            "userinfo carried no non-empty preferred_username; this issuer "
            "or application does not expose the claim hyprial uses for the "
            "owner (requires scope 'profile' and the application allowing "
            "the Name token field)",
            data={"claims": sorted(payload)},
        )
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise LoginError(
            "USER_ID_MISSING",
            "userinfo carried no non-empty sub; the immutable user id is "
            "required and nothing was written",
            data={"claims": sorted(payload)},
        )
    if ":" in subject or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in subject
    ):
        raise LoginError(
            "USER_ID_INVALID",
            "userinfo sub is not a valid address segment; it must contain no "
            "colon, whitespace, or control characters, and nothing was written",
        )
    if not is_user_id_segment(f"u.{subject}"):
        raise LoginError(
            "USER_ID_INVALID",
            "identity provider returned an id format hyprial does not recognise; "
            "an administrator must extend the user-id predicate before this "
            "identity can log in, and nothing was written",
        )
    return (
        username.strip(),
        subject,
        _lark_union_id(payload, access_token),
    )
