"""OIDC device-authorization protocol owned by the credential worker.

Decision: the worker receives only ``AuthProfile`` and trusted proxy/CA data,
builds its own standard-library opener, and returns only public tokens to the
worker state machine.  Access and refresh tokens never cross the worker frame
boundary.  The original CLI protocol in the shell remains the legacy path until
it is switched to a thin caller.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from hyprial import __version__
from hyprial.kernel import is_user_id_segment

from ..ports import AuthProfile, HttpTransport, IdentityError

LOGIN_SCOPE = "openid profile offline_access"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
USER_AGENT = f"hyprial-login/{__version__}"
_HTTP_TIMEOUT_S = 30.0
_RFC8628_DEFAULT_INTERVAL_S = 5.0
_RFC8628_SLOW_DOWN_INCREMENT_S = 5.0


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int | None


@dataclass(frozen=True, slots=True)
class OidcEndpoints:
    device_authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str


@dataclass(frozen=True, slots=True)
class DevicePollResult:
    outcome: Literal["pending", "slow_down", "tokens"]
    access_token: str | None = None
    refresh_token: str | None = None


def _origin(url: str) -> tuple[str, str, int] | None:
    split = urlsplit(url)
    if not split.scheme or not split.hostname:
        return None
    default = {"https": 443, "http": 80}.get(split.scheme)
    port = split.port if split.port is not None else default
    if port is None:
        return None
    return (split.scheme, split.hostname.lower(), port)


def _status_hint(status: int) -> str:
    if status == 403:
        return (
            " (HTTP 403 may come from an edge filter in front of the issuer, "
            f"not the issuer itself; the request sent User-Agent {USER_AGENT!r})"
        )
    return ""


def _token_error(status: int, payload: Any, *, where: str) -> IdentityError | None:
    if 200 <= status < 300:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        error = payload["error"]
        description = payload.get("error_description") or ""
        detail = f"{error}: {description}" if description else error
        return IdentityError(
            "TOKEN_ENDPOINT_ERROR",
            f"token endpoint rejected the {where} request ({detail})",
            data={"status": status, "error": error, "description": description},
        )
    return IdentityError(
        "TOKEN_ENDPOINT_ERROR",
        f"token endpoint answered HTTP {status} for the {where} request"
        f"{_status_hint(status)}",
        data={"status": status},
    )


class OidcClient:
    """One issuer-facing client built from trusted transport data."""

    def __init__(self, transport: HttpTransport | None = None) -> None:
        handlers: list[urllib.request.BaseHandler] = []
        if transport is not None and transport.proxy:
            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": transport.proxy, "https": transport.proxy}
                )
            )
        if transport is not None and transport.ca_bundle:
            context = ssl.create_default_context(cafile=transport.ca_bundle)
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def request_json(
        self,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        body = None
        request_headers = dict(headers or {})
        request_headers.setdefault("User-Agent", USER_AGENT)
        if data is not None:
            body = urllib.parse.urlencode(dict(data)).encode("utf-8")
            request_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded"
            )
        request = urllib.request.Request(url, data=body, headers=request_headers)
        try:
            with self._opener.open(
                request, timeout=_HTTP_TIMEOUT_S if timeout is None else timeout
            ) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
        except urllib.error.URLError as error:
            raise IdentityError(
                "ISSUER_UNREACHABLE", f"cannot reach {url}: {error.reason}"
            ) from error
        except OSError as error:
            raise IdentityError(
                "ISSUER_UNREACHABLE", f"cannot reach {url}: {error}"
            ) from error
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, {"_raw": raw.decode("utf-8", "replace")}

    def fetch_endpoints(
        self, profile: AuthProfile, *, timeout: float | None = None
    ) -> OidcEndpoints:
        url = f"{profile.issuer}/.well-known/openid-configuration"
        status, discovery = self.request_json(url, timeout=timeout)
        if status != 200:
            raise IdentityError(
                "DISCOVERY_UNAVAILABLE",
                f"discovery request failed with HTTP {status}: {url}"
                f"{_status_hint(status)}",
            )
        return self._validate_endpoints(discovery, profile)

    def _validate_endpoints(
        self, discovery: Any, profile: AuthProfile
    ) -> OidcEndpoints:
        if not isinstance(discovery, dict):
            raise IdentityError(
                "DISCOVERY_INVALID", "discovery document is not a JSON object"
            )
        reported = discovery.get("issuer")
        if reported != profile.issuer:
            raise IdentityError(
                "ISSUER_MISMATCH",
                f"discovery issuer {reported!r} does not equal the profile's "
                f"issuer {profile.issuer!r} (byte-for-byte); refusing to continue",
                data={"discoveryIssuer": reported, "profileIssuer": profile.issuer},
            )
        issuer_origin = _origin(profile.issuer)
        if issuer_origin is None:
            raise IdentityError(
                "PROFILE_ISSUER_INVALID",
                f"profile issuer is not an absolute URL: {profile.issuer!r}",
            )
        endpoints = {}
        for key in (
            "device_authorization_endpoint",
            "token_endpoint",
            "userinfo_endpoint",
        ):
            value = discovery.get(key)
            if not isinstance(value, str) or not value:
                raise IdentityError(
                    "DISCOVERY_INVALID", f"discovery has no usable {key}"
                )
            if _origin(value) != issuer_origin:
                raise IdentityError(
                    "ENDPOINT_ORIGIN_MISMATCH",
                    f"{key} {value!r} is not on the issuer's origin "
                    f"{profile.issuer!r}; refusing to continue",
                    data={key: value, "issuer": profile.issuer},
                )
            endpoints[key] = value
        return OidcEndpoints(**endpoints)

    def request_device_authorization(
        self,
        endpoints: OidcEndpoints,
        profile: AuthProfile,
        *,
        scope: str | None = None,
    ) -> DeviceAuthorization:
        status, payload = self.request_json(
            endpoints.device_authorization_endpoint,
            data={"client_id": profile.client_id, "scope": scope or LOGIN_SCOPE},
        )
        error = _token_error(status, payload, where="device authorization")
        if error is not None:
            raise error
        if not isinstance(payload, dict):
            raise IdentityError(
                "DEVICE_AUTH_INVALID", "device authorization body invalid"
            )
        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_uri = payload.get("verification_uri")
        for name, value in (
            ("device_code", device_code),
            ("user_code", user_code),
            ("verification_uri", verification_uri),
        ):
            if not isinstance(value, str) or not value:
                raise IdentityError(
                    "DEVICE_AUTH_INVALID",
                    f"device authorization response has no usable {name}",
                )
        expires_in = payload.get("expires_in")
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise IdentityError(
                "DEVICE_AUTH_INVALID",
                "device authorization expires_in must be a positive integer; "
                f"got {expires_in!r}",
            )
        interval = payload.get("interval")
        if interval is not None and (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or interval < 0
        ):
            raise IdentityError(
                "DEVICE_AUTH_INVALID",
                "device authorization interval must be a non-negative integer; "
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

    def poll_device_token_once(
        self,
        endpoints: OidcEndpoints,
        profile: AuthProfile,
        authorization: DeviceAuthorization,
    ) -> DevicePollResult:
        try:
            status, payload = self.request_json(
                endpoints.token_endpoint,
                data={
                    "grant_type": DEVICE_GRANT_TYPE,
                    "client_id": profile.client_id,
                    "device_code": authorization.device_code,
                },
            )
        except IdentityError:
            raise
        if 200 <= status < 300 and isinstance(payload, dict):
            access = payload.get("access_token")
            if not isinstance(access, str) or not access:
                raise IdentityError(
                    "TOKEN_RESPONSE_INVALID",
                    "token response has no usable access_token",
                )
            refresh = payload.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                raise IdentityError(
                    "TOKEN_RESPONSE_INVALID",
                    "token response has no usable refresh_token; login cannot "
                    "persist a credential without one (scope includes "
                    "offline_access)",
                )
            return DevicePollResult(
                outcome="tokens", access_token=access, refresh_token=refresh
            )
        error = _token_error(status, payload, where="device code")
        assert error is not None
        code = error.data.get("error")
        if code == "authorization_pending":
            return DevicePollResult(outcome="pending")
        if code == "slow_down":
            return DevicePollResult(outcome="slow_down")
        if code == "access_denied":
            raise IdentityError(
                "ACCESS_DENIED",
                "the authorization request was denied; nothing was written",
                data=error.data,
            )
        if code == "expired_token":
            raise IdentityError(
                "DEVICE_CODE_EXPIRED",
                "the device code expired; re-run hyprial login. If you signed "
                "in with Feishu, that sign-in now approves this device",
                data=error.data,
            )
        raise error

    def fetch_userinfo(
        self, endpoints: OidcEndpoints, access_token: str
    ) -> tuple[str, str]:
        status, payload = self.request_json(
            endpoints.userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if status != 200:
            error = _token_error(status, payload, where="userinfo")
            raise error or IdentityError(
                "USERINFO_UNAVAILABLE",
                f"userinfo answered HTTP {status}{_status_hint(status)}",
            )
        if not isinstance(payload, dict):
            raise IdentityError(
                "USERINFO_INVALID", "userinfo response is not an object"
            )
        username = payload.get("preferred_username")
        if not isinstance(username, str) or not username.strip():
            raise IdentityError(
                "USERNAME_MISSING",
                "userinfo carried no non-empty preferred_username; this issuer "
                "or application does not expose the claim hyprial uses for the "
                "owner (requires scope 'profile' and the application allowing "
                "the Name token field)",
                data={"claims": sorted(payload)},
            )
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise IdentityError(
                "USER_ID_MISSING",
                "userinfo carried no non-empty sub; the immutable user id is "
                "required and nothing was written",
                data={"claims": sorted(payload)},
            )
        if ":" in subject or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in subject
        ):
            raise IdentityError(
                "USER_ID_INVALID",
                "userinfo sub is not a valid address segment; it must contain "
                "no colon, whitespace, or control characters, and nothing was "
                "written",
            )
        if not is_user_id_segment(f"u.{subject}"):
            raise IdentityError(
                "USER_ID_INVALID",
                "OIDC issuer returned an id format hyprial does not "
                "recognise; an administrator must extend the user-id predicate "
                "before this identity can log in, and nothing was written",
            )
        return username.strip(), subject


def interval_after_slow_down(current: float) -> float:
    return current + _RFC8628_SLOW_DOWN_INCREMENT_S


def initial_interval(authorization: DeviceAuthorization) -> float:
    if authorization.interval is None:
        return _RFC8628_DEFAULT_INTERVAL_S
    return max(float(authorization.interval), 0.0)


__all__ = [
    "DEVICE_GRANT_TYPE",
    "LOGIN_SCOPE",
    "USER_AGENT",
    "DeviceAuthorization",
    "DevicePollResult",
    "OidcClient",
    "OidcEndpoints",
    "initial_interval",
    "interval_after_slow_down",
]
