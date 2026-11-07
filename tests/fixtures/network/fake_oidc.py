"""In-repo fake OIDC provider for ``hyprial login`` tests (login U2, spec §1.6).

A threaded ``http.server`` (the conftest.py:141-172 pattern: ``serve_forever``
on a daemon thread, ``shutdown`` + ``server_close`` to stop) speaking the four
endpoints ``hyprial login`` consumes: discovery, device authorization, token,
userinfo.

⚠️ Response shapes are pinned to **Casdoor v3.144.0**, field by field — this
fixture must never invent a field layout and call it Casdoor's:

- discovery: ``OidcDiscovery`` (``object/wellknown_oidc_discovery.go:24-56``);
  this fixture emits the subset the client reads plus a few always-present
  Casdoor fields.  ⚠️ 夹具形状待 U4 R1 隔离容器真实响应校正(spec §1.6)。
- device authorization response: exactly ``DeviceAuthResponse``
  (``object/token_oauth_util.go:129-135``) — five fields: ``device_code``,
  ``user_code``, ``verification_uri``, ``expires_in``, ``interval``.
  ``verification_uri`` is built like
  ``GetDeviceAuthResponse`` (``object/wellknown_oidc_discovery.go:282-291``):
  ``<origin>/login/oauth/device/<user_code>?cancelToken=<token>``.
  **Casdoor v3.144.0 does NOT emit ``verification_uri_complete``**; the field
  exists only as RFC 8628 §3.2's optional addition, so the fixture sends it
  only when a test explicitly configures it (to exercise the client's
  prefer-if-present branch) — never as a claimed Casdoor shape.
- token endpoint: device-grant error states are exactly the five Casdoor
  branches of ``controllers/token.go:272-364`` — ``authorization_pending``,
  ``access_denied`` (denied / already-issued), ``expired_token`` (unknown or
  expired device code), ``invalid_client`` (client_id does not match the
  device authorization request, ``controllers/token.go:326-338``) — and the
  success body is ``TokenWrapper`` (``object/token_oauth_util.go:60-66``):
  ``access_token``/``id_token``/``refresh_token``/``token_type``/
  ``expires_in``/``scope``.
- HTTP status: ``TokenError`` ⇒ 400, ``invalid_client`` ⇒ 401, ``TokenWrapper``
  ⇒ 200 (``controllers/util.go:75-89`` ``SetTokenErrorHttpStatus``).
- userinfo: ``Userinfo`` (``object/user.go:250-268``) — ``sub``/``iss``/``aud``
  always; ``preferred_username`` filled only when the granted scope contains
  ``profile`` (``object/user.go:1246-1249``) and the app's TokenFields allow
  ``Name`` — the fixture models the TokenFields half with the
  ``preferred_username=None`` switch.  ``email`` requires the ``email`` scope
  (``object/user.go:1276-1280``); hyprial never asks for it, so the fixture's
  userinfo omits ``email`` — tests that need a three-field separation
  (username vs sub vs email) use ``userinfo_email=True`` to model an issuer
  that would send one.

Two behaviors are **client-side RFC branches, not Casdoor shapes** and are
deliberately marked so in their config fields:

- ``slow_down_at``: RFC 8628 §3.5 defines ``slow_down``; Casdoor v3.144.0
  never returns it (no ``slow_down`` literal anywhere in the tagged source).
  The client must still handle it, so the fixture can be told to emit it.
- ``verification_uri_complete``: see above.

The fake is deterministic: token/code values come from the config so tests
can scan for them (T10) without magic.  Recorded requests live in memory
only; nothing is written to disk and nothing is logged.
"""

from __future__ import annotations

import json
import base64
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic, time
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
"""Casdoor's device_code grant type (controllers/token.go:190-350)."""


@dataclass
class FakeOidcConfig:
    """Everything the fake's four endpoints are parameterized on."""

    client_id: str = "fake-client-id"
    # Device authorization (Casdoor GetDeviceAuthResponse defaults: interval=5,
    # expires_in=120 from CodeResendTimeout fallback; tests default to fast
    # values so only T4 exercises rhythm deliberately).
    device_interval: int = 0
    device_expires_in: int = 600
    pending_polls: int = 1
    # RFC 8628 §3.5 branch — Casdoor never emits it; see module docstring.
    slow_down_at: int | None = None
    # "success" | "denied" | "expired" | "bad_client" — the token-endpoint
    # outcomes of controllers/token.go:272-350 (expired also covers an
    # unknown device_code, token.go:280-287).
    outcome: str = "success"
    # Identity claims (Userinfo shape, object/user.go:250-268).
    preferred_username: str | None = "alicesmith"
    sub: str = "ou_0123456789abcdef0123456789abcdef"
    userinfo_email: bool = False
    email: str = "aaron@not-the-owner.example"
    display_name: str = "Allen Woods"
    lark_union_id: str | None = None
    # RFC-only fields a test opts into; never a Casdoor shape (see docstring).
    verification_uri_complete: str | None = None
    # Discovery overrides for the negative tests: discovery issuer that does
    # not byte-equal the profile issuer (T1) and endpoints advertised on
    # another origin (T2).
    discovery_issuer_override: str | None = None
    userinfo_endpoint_override: str | None = None
    device_endpoint_override: str | None = None
    token_endpoint_override: str | None = None
    # Discovery answered by something other than the issuer — an edge filter
    # such as Cloudflare error 1010 rejecting a User-Agent: a non-200 status
    # with an HTML body, never JSON (U2b).
    discovery_status: int = 200
    # When set, the token endpoint only knows THIS device code: a poll with
    # any other code models a server restart wiping in-memory device state
    # (Casdoor keeps device auth in process memory, object/device_auth_store.go).
    known_device_code: str | None = None
    # Refresh-token behavior (object/token_oauth_util.go RefreshToken:393-459):
    # rotation mirrors Casdoor issuing a new refresh token per refresh.
    rotate_refresh: bool = True
    refresh_error: bool = False
    # Deterministic secrets so T10 can scan for their exact values.
    device_code: str = "fake-device-code-1"
    user_code: str = "FAKE-CODE"
    cancel_token: str = "fake-cancel-token"
    access_token: str = "fake-access-token-1"
    refresh_token: str = "fake-refresh-token-1"
    rotated_refresh_token: str = "fake-refresh-token-2"
    id_token: str = "fake-id-token-1.opaque-no-jose-check"
    # The dedicated public binding-assertion application used after a login
    # that carries the cross-app Lark union id.
    binding_client_id: str = "1be2763a29154030fbcb"
    binding_pending_polls: int = 1
    binding_expired_attempts: int = 0
    binding_access_token: str | None = None
    binding_owner: str = "hyprial"
    binding_name: str | None = None
    binding_union_id: str | None = None
    binding_refresh_token: str = "discard-this-binding-refresh-marker"

    def __post_init__(self) -> None:
        if self.outcome not in {"success", "denied", "expired", "bad_client"}:
            raise ValueError(f"unknown fake outcome: {self.outcome!r}")


@dataclass
class RecordedRequest:
    """One request the fake saw — in memory only, for assertions."""

    method: str
    path: str
    query: dict[str, list[str]]
    form: dict[str, list[str]]
    authorization: str | None
    content_type: str | None
    user_agent: str | None
    at: float


class _FakeState:
    """Mutable server-side state shared by the handler threads."""

    def __init__(self, config: FakeOidcConfig) -> None:
        self.config = config
        self.base_url = ""  # set once the socket is bound
        self.records: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._polls = 0
        self._binding_authorizations = 0
        self._binding_polls: dict[str, int] = {}
        self._issued_for_scope: dict[str, str] = {}
        self.refresh_issued = 0

    def record(self, request: RecordedRequest) -> None:
        with self._lock:
            self.records.append(request)

    @property
    def polls(self) -> int:
        with self._lock:
            return self._polls

    def next_poll(self) -> int:
        with self._lock:
            self._polls += 1
            return self._polls


def _token_error(status: int, error: str, description: str) -> tuple[int, dict[str, str]]:
    """Casdoor TokenError JSON at its pinned HTTP status (util.go:75-89)."""

    return status, {"error": error, "error_description": description}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")


def _make_handler(state: _FakeState) -> type[BaseHTTPRequestHandler]:
    config = state.config

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:  # silence
            return

        # -- plumbing ------------------------------------------------------

        def _respond(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, body: Any) -> None:
            self._respond(status, _json_bytes(body), "application/json")

        def _read_form(self) -> dict[str, list[str]]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return parse_qs(raw.decode("utf-8"), keep_blank_values=True)

        def _record(self, form: dict[str, list[str]]) -> None:
            split = urlsplit(self.path)
            state.record(
                RecordedRequest(
                    method=self.command,
                    path=split.path,
                    query=parse_qs(split.query, keep_blank_values=True),
                    form=form,
                    authorization=self.headers.get("Authorization"),
                    content_type=self.headers.get("Content-Type"),
                    user_agent=self.headers.get("User-Agent"),
                    at=monotonic(),
                )
            )

        # -- endpoints -----------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            path = urlsplit(self.path).path
            self._record({})
            if path == "/.well-known/openid-configuration":
                if config.discovery_status != 200:
                    self._respond(
                        config.discovery_status,
                        b"<html>error code: 1010</html>",
                        "text/html",
                    )
                    return
                self._json(200, self._discovery())
                return
            if path == "/api/userinfo":
                self._userinfo()
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            form = self._read_form()
            self._record(form)
            path = urlsplit(self.path).path
            if path == "/api/device-auth":
                self._device_auth(form)
                return
            if path == "/api/token":
                self._token(form)
                return
            self._json(404, {"error": "not_found"})

        def _discovery(self) -> dict[str, Any]:
            base = state.base_url
            issuer = config.discovery_issuer_override or base
            userinfo = config.userinfo_endpoint_override or f"{base}/api/userinfo"
            device = config.device_endpoint_override or f"{base}/api/device-auth"
            token = config.token_endpoint_override or f"{base}/api/token"
            # Subset of Casdoor OidcDiscovery (wellknown_oidc_discovery.go:24-56):
            # every field the client reads, plus the always-present fields a
            # real discovery carries, so a client that over-reads still sees a
            # realistic document.  待 U4 R1 实测校正。
            return {
                "issuer": issuer,
                "authorization_endpoint": f"{base}/api/authorize",
                "token_endpoint": token,
                "userinfo_endpoint": userinfo,
                "device_authorization_endpoint": device,
                "jwks_uri": f"{base}/api/certs",
                "grant_types_supported": [
                    "authorization_code",
                    "implicit",
                    "refresh_token",
                    "password",
                    "client_credentials",
                    DEVICE_GRANT,
                ],
                "response_types_supported": ["code", "token", "id_token"],
                "scopes_supported": ["openid", "profile", "email", "offline_access"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "claims_supported": [
                    "sub",
                    "iss",
                    "aud",
                    "preferred_username",
                    "name",
                    "email",
                ],
            }

        def _device_auth(self, form: dict[str, list[str]]) -> None:
            # Casdoor DeviceAuth (controllers/auth.go:1510-1604): client_id
            # must resolve to an application that has Device login enabled;
            # unknown client ⇒ TokenError invalid_client.
            client_id = (form.get("client_id") or [""])[0]
            if client_id not in {config.client_id, config.binding_client_id}:
                self._json(
                    *_token_error(400, "invalid_client", "Invalid client_id")
                )
                return
            scope = (form.get("scope") or [""])[0]
            if client_id == config.binding_client_id:
                state._binding_authorizations += 1
                device_code = f"fake-binding-code-{state._binding_authorizations}"
                user_code = f"BIND-{state._binding_authorizations}"
            else:
                device_code = config.device_code
                user_code = config.user_code
            state._issued_for_scope[device_code] = scope
            # Exactly DeviceAuthResponse (token_oauth_util.go:129-135): five
            # fields, verification_uri built like GetDeviceAuthResponse
            # (wellknown_oidc_discovery.go:282-291).
            body: dict[str, Any] = {
                "device_code": device_code,
                "user_code": user_code,
                "verification_uri": (
                    f"{state.base_url}/login/oauth/device/{user_code}"
                    f"?cancelToken={config.cancel_token}"
                ),
                "expires_in": config.device_expires_in,
                "interval": config.device_interval,
            }
            if config.verification_uri_complete is not None:
                # RFC 8628 §3.2 optional field — Casdoor v3.144.0 does not
                # send it; only an explicit test opt-in (module docstring).
                body["verification_uri_complete"] = config.verification_uri_complete
            self._json(200, body)

        def _token(self, form: dict[str, list[str]]) -> None:
            grant = (form.get("grant_type") or [""])[0]
            if grant == DEVICE_GRANT:
                self._device_token_poll(form)
                return
            if grant == "refresh_token":
                self._refresh(form)
                return
            self._json(
                *_token_error(
                    400, "unsupported_grant_type", "grant_type is not supported"
                )
            )

        def _device_token_poll(self, form: dict[str, list[str]]) -> None:
            device_code = (form.get("device_code") or [""])[0]
            client_id = (form.get("client_id") or [""])[0]
            if client_id == config.binding_client_id:
                expected = f"fake-binding-code-{state._binding_authorizations}"
                if device_code != expected:
                    self._json(
                        *_token_error(400, "expired_token", "token is expired")
                    )
                    return
                attempt = state._binding_authorizations
                if attempt <= config.binding_expired_attempts:
                    self._json(
                        *_token_error(400, "expired_token", "token is expired")
                    )
                    return
                polls = state._binding_polls.get(device_code, 0) + 1
                state._binding_polls[device_code] = polls
                if polls <= config.binding_pending_polls:
                    self._json(
                        *_token_error(
                            400, "authorization_pending", "authorization pending"
                        )
                    )
                    return
                scope = state._issued_for_scope.get(device_code, "")
                binding_access_token = config.binding_access_token
                if binding_access_token is None:
                    encode = lambda value: base64.urlsafe_b64encode(  # noqa: E731
                        json.dumps(value).encode()
                    ).rstrip(b"=").decode()
                    now = int(time())
                    binding_access_token = (
                        f"{encode({'alg': 'RS256', 'kid': 'fake-binding-key'})}."
                        f"{encode({'iss': state.base_url, 'aud': config.binding_client_id, 'owner': config.binding_owner, 'name': config.binding_name or config.preferred_username, 'oauth_Lark_unionId': config.binding_union_id or config.lark_union_id, 'iat': now, 'exp': now + 180})}."
                        "fake-signature"
                    )
                self._json(
                    200,
                    {
                        "access_token": binding_access_token,
                        "id_token": "binding-id-token",
                        "refresh_token": config.binding_refresh_token,
                        "token_type": "Bearer",
                        "expires_in": 180,
                        "scope": scope,
                    },
                )
                return
            known = config.known_device_code or config.device_code
            if device_code != known:
                # Unknown device code: token.go:280-287 answers expired_token.
                self._json(
                    *_token_error(400, "expired_token", "token is expired")
                )
                return
            if config.outcome == "bad_client" or client_id != config.client_id:
                # token.go:326-338: the poll is bound to the original client.
                self._json(
                    *_token_error(
                        401,
                        "invalid_client",
                        "client_id does not match the device authorization request",
                    )
                )
                return
            if config.outcome == "expired":
                self._json(
                    *_token_error(400, "expired_token", "token is expired")
                )
                return
            if config.outcome == "denied":
                self._json(
                    *_token_error(400, "access_denied", "device login was denied")
                )
                return
            poll = state.next_poll()
            if config.slow_down_at is not None and poll == config.slow_down_at:
                # RFC 8628 §3.5 branch — NOT a Casdoor v3.144.0 behavior.
                self._json(
                    *_token_error(400, "slow_down", "polling interval too short")
                )
                return
            if poll <= config.pending_polls:
                self._json(
                    *_token_error(
                        400, "authorization_pending", "authorization pending"
                    )
                )
                return
            scope = state._issued_for_scope.get(device_code, "")
            self._json(200, self._token_wrapper(scope, rotate=False))

        def _refresh(self, form: dict[str, list[str]]) -> None:
            refresh_token = (form.get("refresh_token") or [""])[0]
            if config.refresh_error or refresh_token not in (
                config.refresh_token,
                config.rotated_refresh_token,
            ):
                # token_oauth_util.go RefreshToken: invalid/revoked refresh
                # token ⇒ invalid_grant.
                self._json(
                    *_token_error(
                        400,
                        "invalid_grant",
                        "refresh token is invalid or revoked",
                    )
                )
                return
            state.refresh_issued += 1
            self._json(200, self._token_wrapper("openid profile", rotate=True))

        def _token_wrapper(self, scope: str, *, rotate: bool) -> dict[str, Any]:
            # TokenWrapper (token_oauth_util.go:60-66): six fields, no omitempty.
            refresh = config.refresh_token
            if rotate and config.rotate_refresh:
                refresh = config.rotated_refresh_token
            return {
                "access_token": config.access_token,
                "id_token": config.id_token,
                "refresh_token": refresh,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": scope,
            }

        def _userinfo(self) -> None:
            expected = f"Bearer {config.access_token}"
            if self.headers.get("Authorization") != expected:
                self._json(
                    *_token_error(401, "invalid_token", "token is invalid")
                )
                return
            scope = state._issued_for_scope.get(config.device_code, "")
            base = state.base_url
            # Userinfo (object/user.go:250-268): sub/iss/aud always;
            # preferred_username only under a profile-containing scope
            # (user.go:1246-1249) with TokenFields allowing Name — modeled
            # here by the preferred_username=None switch.
            body: dict[str, Any] = {
                "sub": config.sub,
                "iss": config.discovery_issuer_override or base,
                "aud": config.client_id,
            }
            if "profile" in scope and config.preferred_username is not None:
                body["preferred_username"] = config.preferred_username
                body["name"] = config.display_name
            if config.userinfo_email:
                body["email"] = config.email
                body["email_verified"] = True
            if config.lark_union_id is not None:
                body["properties"] = {
                    "oauth_Lark_unionId": config.lark_union_id
                }
            self._json(200, body)

    return Handler


class FakeOidcServer:
    """A started fake OIDC provider: ``with fake_oidc() as server: ...``."""

    def __init__(self, config: FakeOidcConfig | None = None) -> None:
        self.config = config or FakeOidcConfig()
        self.state = _FakeState(self.config)
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "FakeOidcServer":
        self._server = HTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.state.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeOidcServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- what the fake saw ----------------------------------------------------

    @property
    def base_url(self) -> str:
        return self.state.base_url

    @property
    def issuer(self) -> str:
        """The issuer a profile for this fake should carry."""

        return self.state.base_url

    @property
    def records(self) -> list[RecordedRequest]:
        return list(self.state.records)

    def records_for(self, path: str) -> list[RecordedRequest]:
        return [item for item in self.records if item.path == path]

    def current_refresh_token(self) -> str:
        """The refresh token the fake most recently handed out."""

        return (
            self.config.rotated_refresh_token
            if self.config.rotate_refresh and self.state.refresh_issued
            else self.config.refresh_token
        )


def fake_oidc(config: FakeOidcConfig | None = None) -> FakeOidcServer:
    """Start a fake OIDC provider (use as a context manager)."""

    return FakeOidcServer(config)
