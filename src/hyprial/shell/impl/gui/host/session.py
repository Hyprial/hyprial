# Decision record (GUI session host, PR 2b):
# - Sessions are in memory only. A host restart ends every session; durable
#   state stays in the daemon, so the GUI never becomes a second authority.
# - Two cookies and one proof: the control cookie survives a refresh, the
#   business cookie is rotated with generation and CSRF, and the view proof is
#   held by the page, never in a cookie. A copied cookie alone is not a session.
# - The upstream catalog is pinned per session. `mutates` and `callable` in that
#   pinned list, not the GUI build, decide CSRF and admission.
# - Clock and token source are injectable so the expiry and rotation rules are
#   tested without sleeping; production uses wall-clock milliseconds and
#   32 random bytes (base64url) per token, compared in constant time.
# - The grant is consumed under a lock that covers its re-check and the mark:
#   the single event loop already serialises it, and the lock keeps that true
#   if a caller ever runs off the loop.
# - The Host and Origin rule applies to every path of the host, not only
#   /daemon/*; the browser sends nothing the rule rejects.
"""Control and business sessions, cookies, launch-grant state and the origin rule."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
import asyncio
from dataclasses import dataclass
from typing import Callable, Literal, Protocol
from urllib.parse import quote, unquote, urlsplit

from hyprial.kernel import ipc_errors

from .config import HostConfig, is_loopback_name
from .envelope import HostFailure

CONTROL_COOKIE = "hyprial_gui_control"
SESSION_COOKIE = "hyprial_gui_session"
COOKIE_PATH = "/daemon"

GrantStatus = Literal["ok", "invalid", "used", "expired"]


class Handle(Protocol):
    def cancel(self) -> None: ...


class Clock(Protocol):
    """Wall-clock milliseconds plus one-shot callbacks; replaced by a fake in tests."""

    def now_ms(self) -> int: ...

    def call_later(self, delay_seconds: float, callback: Callable[[], None]) -> Handle: ...


class SystemClock:
    """The production clock: wall time and the running asyncio loop."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def call_later(self, delay_seconds: float, callback: Callable[[], None]) -> Handle:
        return asyncio.get_running_loop().call_later(delay_seconds, callback)


def random_token() -> str:
    """32 random bytes, base64url, as control ids, views, sessions, generations and CSRF."""
    return secrets.token_urlsafe(32)


def equal_strings(left: str | None, right: str | None) -> bool:
    """Constant-time equality that treats a missing value as unequal."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def session_required(message: str) -> HostFailure:
    return HostFailure(401, "SESSION_REQUIRED", message)


@dataclass(frozen=True)
class SessionMethod:
    name: str
    mutates: bool
    callable: bool
    state: str


@dataclass(frozen=True)
class SessionAuthority:
    """What one upstream probe established; copied into every session minted from it."""

    driver: str
    phase: str
    catalog_digest: str
    declaration_digest: str
    backend_epoch: str
    http_contract_version: int
    catalog_version: str
    methods: tuple[SessionMethod, ...]


@dataclass
class ControlSession:
    id: str
    view: str
    expires_at: int


@dataclass
class GuiSession:
    id: str
    phase: str
    control_id: str
    view: str
    generation: str
    csrf: str
    expires_at: int
    catalog_digest: str
    declaration_digest: str
    backend_epoch: str
    http_contract_version: int
    catalog_version: str
    methods: dict[str, SessionMethod]
    revoked: bool = False


@dataclass(frozen=True)
class RotatedSession:
    control: ControlSession
    session: GuiSession
    initial: bool


class SessionStore:
    """Control sessions, business sessions and the one-time launch grant."""

    def __init__(
        self,
        config: HostConfig,
        *,
        clock: Clock | None = None,
        tokens: Callable[[], str] = random_token,
    ) -> None:
        self._clock = clock or SystemClock()
        self._tokens = tokens
        self._ttl_ms = config.session_ttl_ms
        self._max_sessions = config.max_sessions
        self._launch_grant = config.launch_grant
        self._launch_ttl_ms = config.launch_ttl_ms
        self._publication_path = config.publication_path
        self._sessions: dict[str, GuiSession] = {}
        self._controls: dict[str, ControlSession] = {}
        self._grant_used = False
        self._grant_lock = threading.Lock()
        self._launch_expires_at: int | None = (
            self._clock.now_ms() + self._launch_ttl_ms if self._publication_path is None else None
        )

    @property
    def now(self) -> int:
        return self._clock.now_ms()

    @property
    def active_count(self) -> int:
        self.prune()
        return len(self._sessions)

    def activate_launch_grant(self) -> int | None:
        """Start the grant clock once publication exists; ``None`` while it does not."""
        if self._launch_expires_at is not None:
            return self._launch_expires_at
        if self._publication_path is None or not os.path.exists(self._publication_path):
            return None
        self._launch_expires_at = self._clock.now_ms() + self._launch_ttl_ms
        return self._launch_expires_at

    def grant_status(self, raw: str) -> GrantStatus:
        with self._grant_lock:
            return self._grant_state(raw)

    def _grant_state(self, raw: str) -> GrantStatus:
        if not equal_strings(raw, self._launch_grant):
            return "invalid"
        if self._grant_used:
            return "used"
        expires_at = self.activate_launch_grant()
        if expires_at is None or self._clock.now_ms() >= expires_at:
            return "expired"
        return "ok"

    def consume_grant(self, raw: str) -> GrantStatus:
        with self._grant_lock:
            status = self._grant_state(raw)
            if status == "ok":
                self._grant_used = True
            return status

    def mint(self, authority: SessionAuthority) -> RotatedSession:
        self.prune()
        self._ensure_capacity()
        now = self._clock.now_ms()
        control = ControlSession(id=self._tokens(), view=self._tokens(), expires_at=now + self._ttl_ms)
        self._controls[control.id] = control
        return RotatedSession(control, self._issue(control, authority, now), True)

    def refresh(self, control_id: str | None, view: str | None, authority: SessionAuthority) -> RotatedSession:
        self.prune()
        control = self.require_control(control_id, view)
        for session in self._sessions.values():
            if session.control_id == control.id:
                session.revoked = True
        self.prune()
        self._ensure_capacity()
        return RotatedSession(control, self._issue(control, authority, self._clock.now_ms()), False)

    def require_control(self, control_id: str | None, view: str | None) -> ControlSession:
        self.prune()
        control = self._controls.get(control_id) if control_id is not None else None
        if control is None or control.expires_at <= self._clock.now_ms() or not equal_strings(control.view, view):
            raise session_required("a live control session and view proof are required")
        return control

    def attach(self, control_id: str | None, session_id: str | None, view: str | None) -> GuiSession:
        """The live business session of a proven control, for a second document; rotates nothing."""
        control = self.require_control(control_id, view)
        session = self.get(session_id)
        if session is None or session.control_id != control.id:
            raise session_required("a live business session of this control is required")
        return session

    def invalidate_business_sessions(self) -> None:
        for session in self._sessions.values():
            session.revoked = True
        self._sessions.clear()

    def is_current(
        self, control_id: str | None, view: str | None, session_id: str | None, generation: str | None
    ) -> bool:
        control = self.require_control(control_id, view)
        session = self.get(session_id)
        return (
            session is not None
            and session.control_id == control.id
            and equal_strings(session.generation, generation)
        )

    def get(self, session_id: str | None) -> GuiSession | None:
        self.prune()
        if session_id is None:
            return None
        session = self._sessions.get(session_id)
        return session if session is not None and not session.revoked else None

    def validate(self, session_id: str | None, view: str | None, generation: str | None) -> GuiSession:
        session = self.get(session_id)
        if session is None or not equal_strings(session.view, view):
            raise session_required("a live business session and view proof are required")
        if not equal_strings(session.generation, generation):
            raise HostFailure(403, "GENERATION_REJECTED", "the request generation is no longer current")
        return session

    def csrf_matches(self, session: GuiSession, candidate: str | None) -> bool:
        return equal_strings(session.csrf, candidate)

    def revoke(self, session_id: str, *, include_control: bool = False) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.revoked = True
        if include_control:
            self._controls.pop(session.control_id, None)
        del self._sessions[session_id]

    def prune(self) -> None:
        now = self._clock.now_ms()
        for key in [k for k, s in self._sessions.items() if s.revoked or s.expires_at <= now]:
            del self._sessions[key]
        for key in [k for k, c in self._controls.items() if c.expires_at <= now]:
            del self._controls[key]

    def _ensure_capacity(self) -> None:
        if len(self._sessions) >= self._max_sessions:
            raise HostFailure(429, "SESSION_CAPACITY", "the host session capacity is exhausted")

    def _issue(self, control: ControlSession, authority: SessionAuthority, now: int) -> GuiSession:
        session = GuiSession(
            id=self._tokens(),
            phase=authority.phase,
            control_id=control.id,
            view=control.view,
            generation=self._tokens(),
            csrf=self._tokens(),
            expires_at=min(now + self._ttl_ms, control.expires_at),
            catalog_digest=authority.catalog_digest,
            declaration_digest=authority.declaration_digest,
            backend_epoch=authority.backend_epoch,
            http_contract_version=authority.http_contract_version,
            catalog_version=authority.catalog_version,
            methods={method.name: method for method in authority.methods},
        )
        self._sessions[session.id] = session
        return session


def parse_cookies(header: str | None) -> dict[str, str]:
    """First value wins; unnamed or empty parts are ignored; bad encoding is a 400."""
    values: dict[str, str] = {}
    if not header:
        return values
    for part in header.split(";"):
        index = part.find("=")
        if index <= 0:
            continue
        name, encoded = part[:index].strip(), part[index + 1 :].strip()
        if not name or not encoded or name in values:
            continue
        values[name] = _decode_cookie(encoded)
    return values


def _decode_cookie(encoded: str) -> str:
    malformed = HostFailure(400, ipc_errors.INVALID_REQUEST, "a cookie value is not valid encoding")
    index = encoded.find("%")
    while index != -1:
        digits = encoded[index + 1 : index + 3]
        if len(digits) != 2 or any(c not in "0123456789abcdefABCDEF" for c in digits):
            raise malformed
        index = encoded.find("%", index + 3)
    try:
        return unquote(encoded, errors="strict")
    except UnicodeDecodeError:
        raise malformed from None


def _cookie(name: str, value: str, max_age_ms: int) -> str:
    seconds = max(1, max_age_ms // 1000)
    return f"{name}={quote(value, safe='')}; Path={COOKIE_PATH}; Max-Age={seconds}; HttpOnly; SameSite=Strict"


def session_cookies(rotated: RotatedSession, now: int) -> tuple[str, str]:
    return (
        _cookie(CONTROL_COOKIE, rotated.control.id, rotated.control.expires_at - now),
        _cookie(SESSION_COOKIE, rotated.session.id, rotated.session.expires_at - now),
    )


def expire_cookies() -> tuple[str, str]:
    def expired(name: str) -> str:
        return f"{name}=; Path={COOKIE_PATH}; Max-Age=0; HttpOnly; SameSite=Strict"

    return expired(CONTROL_COOKIE), expired(SESSION_COOKIE)


def require_same_origin(
    *,
    host: str | None,
    origin: str | None,
    fetch_site: str | None,
    listener_port: int | None,
    require_origin: bool,
) -> str:
    """Enforce the loopback Host, the exact Origin and same-origin Fetch Metadata."""
    if not host:
        raise HostFailure(403, "HTTP_HOST_DENIED", "Host header is required")
    try:
        parsed = urlsplit(f"http://{host}")
        port = parsed.port
        hostname = parsed.hostname or ""
    except ValueError:
        raise HostFailure(403, "HTTP_HOST_DENIED", "Host header is not a loopback origin") from None
    if (
        not is_loopback_name(hostname)
        or port is None
        or port == 80
        or port != listener_port
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or "@" in host
    ):
        raise HostFailure(403, "HTTP_HOST_DENIED", "Host header must name this loopback listener")
    shown = f"[{hostname}]" if ":" in hostname else hostname
    expected = f"http://{shown}:{port}"
    if origin is None:
        if require_origin:
            raise HostFailure(403, "ORIGIN_REQUIRED", "an explicit same-origin Origin header is required")
    elif origin != expected:
        raise HostFailure(403, "ORIGIN_REJECTED", "Origin does not match this loopback listener")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        raise HostFailure(403, "ORIGIN_REJECTED", "cross-site request metadata is not accepted")
    return expected
