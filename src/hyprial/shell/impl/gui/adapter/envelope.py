"""N-A2 request envelope (v0.1): the consumption-side verifier for the facade.

The frozen spec (org-improver direct answer, recorded at
``docs/design/app-cli-capability-audit-2026-10-08.md`` lines 289-295, plus the
v0.1 alignment answer):

- a directed request authenticates with an envelope of exactly
  ``{"deviceId", "ts", "nonce", "mac"}``;
- the request key comes from the sidecar's ``derive`` op (role ``server``,
  peer = the client public): X25519 + HKDF-SHA256, empty salt,
  ``info = "hyprial-orgfs-req-v1"`` plus the raw clientPub then serverPub
  (fixed order) -- the raw DH never leaves the sidecar, and this module never
  re-derives it;
- ``mac`` is HMAC-SHA256 over the pipe-joined text
  ``ts|nonce|space_id|method|digest`` (fixed order, unchanged);
- ``digest`` is SHA-256 (hex) over the **whole request body without its
  ``auth`` key** -- ``{operation, params}`` and, when present, ``author`` --
  canonicalized as ``json.dumps(body, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)`` encoded UTF-8 (the orgfs
  ``_json_bytes`` rule; the final cross-language vectors still govern);
- ``author`` lives in the **request body** (requester.author semantics, not in
  the envelope): absent, or equal to the device row's owner, is allowed; a
  present author that differs from the owner is refused with the reference
  reason ``author-device-mismatch``;
- the verifier confirms identity from the device row's owner, rejects a
  ``ts`` beyond +-120 seconds (``stale-request``) and a replayed nonce
  (``replayed-request``); the nonce is burned **only after the MAC verifies**,
  so a guessed MAC cannot poison a device's window;
- the nonce window's NORMATIVE semantics is the time horizon: entries prune
  at ``ts + 120 s``.  The fixed capacity is local hardening only (refusing
  new requests instead of evicting live replay evidence) and must not be
  propagated as part of the specification;
- a sidecar without the derive capability answers ``SIDECAR_DERIVE_UNSUPPORTED``
  -- never a downgrade to unsigned.

Mapping onto the Core port frame (our design point, documented here and in
the slice record):

- ``method`` := ``operation``, exactly the five named operations;
- ``space_id`` := ``params.sessionId`` -- the facade's session/binding domain;
  the two sessionless operations sign the empty domain ``""``;
- ``ts`` is an integer in seconds on this frame: the float/string time
  representations stay a question until the final vectors land, and requiring
  an integer avoids a cross-language formatting trap instead of guessing one.

Refusals carry OUR frame style for ``code`` and the frozen reference reason in
``error.details.reason``; the facade also logs one refusal line per rejected
request (host-level telemetry) with the same reason.

Gate posture: verification exists only when an :class:`AuthGate` is explicitly
wired into the remote facade (``serve_tcp(..., auth_gate=...)``).  With no
gate, requests take exactly the old path, and the production tailnet arm stays
fail-closed outside this module (bridge kind=0 unchanged).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.shell.impl.gui.adapter.errors import AppAdapterError

#: This facade's frame vocabulary.  Only ``derive-unsupported`` carries an
#: observed spec value (``SIDECAR_DERIVE_UNSUPPORTED``); the rest are our
#: names until the daemon boundary publishes its own codes.  Every refusal
#: also carries the frozen reference reason in ``details.reason``.
ENVELOPE_MISSING = "auth-envelope-missing"
ENVELOPE_INVALID = "auth-envelope-invalid"
TIMESTAMP_SKEW = "auth-timestamp-skew"
NONCE_REPLAYED = "auth-nonce-replayed"
NONCE_WINDOW_FULL = "auth-nonce-window-full"
AUTHOR_MISMATCH = "auth-author-mismatch"
DEVICE_UNKNOWN = "device-unknown"
DERIVE_UNSUPPORTED = "derive-unsupported"
DERIVE_UNAVAILABLE = "derive-unavailable"
NOT_AUTHORIZED = "auth-not-authorized"

#: The frozen reference reasons, carried in ``details.reason`` and in the
#: facade's refusal telemetry.  ``NONCE_WINDOW_FULL`` is deliberately absent:
#: the capacity is local hardening, not part of the specification.
REASON_DEVICE_AUTHENTICATION_REQUIRED = "device-authentication-required"
REASON_MALFORMED_AUTH = "malformed-auth"
REASON_MAC_MISSING = "mac-missing"
REASON_MAC_MISMATCH = "mac-mismatch"
REASON_STALE_REQUEST = "stale-request"
REASON_REPLAYED_REQUEST = "replayed-request"
REASON_AUTHOR_DEVICE_MISMATCH = "author-device-mismatch"
REASON_UNKNOWN_DEVICE = "unknown-device"
REASON_DERIVE_UNAVAILABLE = "derive-unavailable"
REASON_SIDECAR_DERIVE_UNSUPPORTED = "SIDECAR_DERIVE_UNSUPPORTED"

REFERENCE_REASONS = frozenset(
    {
        REASON_DEVICE_AUTHENTICATION_REQUIRED,
        REASON_MALFORMED_AUTH,
        REASON_MAC_MISSING,
        REASON_MAC_MISMATCH,
        REASON_STALE_REQUEST,
        REASON_REPLAYED_REQUEST,
        REASON_AUTHOR_DEVICE_MISMATCH,
        REASON_UNKNOWN_DEVICE,
        REASON_DERIVE_UNAVAILABLE,
        REASON_SIDECAR_DERIVE_UNSUPPORTED,
    }
)

#: The frozen +-120 second window; the capacity is local hardening only.
WINDOW_SECONDS = 120.0
NONCE_CAPACITY = 4096
DERIVE_CACHE_SIZE = 8

#: Exactly the fields one directed envelope carries.
AUTH_FIELDS = frozenset({"deviceId", "ts", "nonce", "mac"})

#: The request body's allowed top-level keys; anything else is malformed.
BODY_FIELDS = frozenset({"operation", "params", "author", "auth"})

#: The signed domain of the two sessionless operations.
SESSIONLESS_SPACE = ""


class DeriveUnsupported(Exception):
    """The derive source answered SIDECAR_DERIVE_UNSUPPORTED."""


@dataclass(frozen=True, slots=True)
class DeviceRow:
    """The part of the N-A2 device row this verifier consumes.

    ``owner`` is the identity authority (a present ``author`` must match it);
    ``client_public`` is the peer public key ``derive(role="server", peer=...)``
    consumes.  How both are sourced in production is the daemon boundary's;
    tests inject them.
    """

    owner: str
    client_public: str


@dataclass(frozen=True, slots=True)
class VerifiedRequest:
    """What a verified envelope proves, for the authorization matrix."""

    device_id: str
    author: str
    operation: str
    session_id: str | None


def mac_text(ts: int, nonce: str, space_id: str, method: str, digest: str) -> str:
    """The frozen MAC pre-image: pipe-joined, fixed order, nothing else."""

    return f"{ts}|{nonce}|{space_id}|{method}|{digest}"


def request_digest(request: Mapping[str, Any]) -> str:
    """SHA-256 over the request body without its ``auth`` key.

    The canonicalization is the orgfs ``_json_bytes`` rule (sorted keys,
    compact separators, UTF-8); the final cross-language vectors still govern
    and this helper is the single place that spells it on our side.
    """

    body = {key: value for key, value in request.items() if key != "auth"}
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _refusal(code: str, message: str, reason: str, **fields: object) -> AppAdapterError:
    details: dict[str, object] = {"reason": reason}
    details.update(fields)
    return AppAdapterError(code, message, details)


class AuthGate:
    """Verifies the N-A2 envelope on one facade; built only when configured.

    ``derive(role, peer)`` returns the request key from the sidecar seam (this
    module never derives it); ``device_row(device_id)`` resolves the device
    row.  The nonce window's normative horizon is ``ts + 120 s``; the capacity
    is local hardening that refuses new requests rather than evicting live
    replay evidence, and a nonce is burned only after its MAC verifies.
    """

    def __init__(
        self,
        *,
        derive: Callable[[str, str], bytes],
        device_row: Callable[[str], DeviceRow | None],
        clock: Callable[[], float] = time.time,
        window_seconds: float = WINDOW_SECONDS,
        nonce_capacity: int = NONCE_CAPACITY,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if nonce_capacity < 1:
            raise ValueError("nonce_capacity must be positive")
        self._derive = derive
        self._device_row = device_row
        self._clock = clock
        self._window = window_seconds
        self._capacity = nonce_capacity
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], float] = {}
        self._keys: dict[str, bytes] = {}

    # -- verification -------------------------------------------------------

    def verify(self, request: Mapping[str, Any]) -> VerifiedRequest:
        """Validate one frame's envelope; raises :class:`AppAdapterError`."""

        unexpected_body = set(request) - BODY_FIELDS
        if unexpected_body:
            raise _refusal(
                ENVELOPE_INVALID,
                "the request carries unknown top-level fields",
                REASON_MALFORMED_AUTH,
                unexpectedFields=sorted(unexpected_body),
            )
        auth = request.get("auth")
        if not isinstance(auth, Mapping) or not auth:
            raise _refusal(
                ENVELOPE_MISSING,
                "the request carries no auth envelope",
                REASON_DEVICE_AUTHENTICATION_REQUIRED,
            )
        unexpected = set(auth) - AUTH_FIELDS
        if unexpected:
            raise _refusal(
                ENVELOPE_INVALID,
                "the auth envelope carries unknown fields",
                REASON_MALFORMED_AUTH,
                unexpectedFields=sorted(unexpected),
            )
        missing = AUTH_FIELDS - set(auth)
        if "mac" in missing:
            raise _refusal(
                ENVELOPE_INVALID,
                "the auth envelope is missing its mac",
                REASON_MAC_MISSING,
                missingFields=sorted(missing),
            )
        if missing:
            raise _refusal(
                ENVELOPE_MISSING,
                "the auth envelope is missing required fields",
                REASON_MALFORMED_AUTH,
                missingFields=sorted(missing),
            )
        operation = request.get("operation")
        if not isinstance(operation, str) or not operation:
            raise _refusal(
                ENVELOPE_INVALID,
                "operation must be a non-empty string",
                REASON_MALFORMED_AUTH,
            )
        params = request.get("params")
        if not isinstance(params, Mapping):
            raise _refusal(
                ENVELOPE_INVALID, "params must be an object", REASON_MALFORMED_AUTH
            )

        device_id = auth["deviceId"]
        mac = auth["mac"]
        nonce = auth["nonce"]
        ts = auth["ts"]
        if (
            not isinstance(device_id, str)
            or not device_id
            or not isinstance(mac, str)
            or not mac
            or not isinstance(nonce, str)
            or not nonce
        ):
            raise _refusal(
                ENVELOPE_INVALID,
                "deviceId, mac and nonce must be non-empty strings",
                REASON_MALFORMED_AUTH,
            )
        if isinstance(ts, bool) or not isinstance(ts, int):
            raise _refusal(
                ENVELOPE_INVALID,
                "ts must be an integer number of seconds on this frame",
                REASON_MALFORMED_AUTH,
            )
        raw_author = request.get("author")
        if raw_author is not None and (
            not isinstance(raw_author, str) or not raw_author
        ):
            raise _refusal(
                ENVELOPE_INVALID,
                "author must be a non-empty string when present",
                REASON_MALFORMED_AUTH,
            )

        row = self._device_row(device_id)
        if row is None:
            raise _refusal(
                DEVICE_UNKNOWN,
                "the device id is not a known device row",
                REASON_UNKNOWN_DEVICE,
                deviceId=device_id,
            )
        if raw_author is not None and raw_author != row.owner:
            raise _refusal(
                AUTHOR_MISMATCH,
                "the request's author does not match the device row's owner",
                REASON_AUTHOR_DEVICE_MISMATCH,
                author=raw_author,
                owner=row.owner,
            )
        author = raw_author if raw_author is not None else row.owner
        now = self._clock()
        if abs(now - ts) > self._window:
            raise _refusal(
                TIMESTAMP_SKEW,
                f"ts is outside the +-{self._window:g} second window",
                REASON_STALE_REQUEST,
                ts=ts,
                now=now,
            )

        session_raw = params.get("sessionId")
        session_id = session_raw if isinstance(session_raw, str) and session_raw else None
        space_id = session_id if session_id is not None else SESSIONLESS_SPACE
        digest = request_digest(request)
        key = self._request_key(row.client_public)
        expected = hmac.new(
            key,
            mac_text(ts, nonce, space_id, operation, digest).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, mac):
            raise _refusal(
                ENVELOPE_INVALID,
                "the request mac does not verify",
                REASON_MAC_MISMATCH,
                operation=operation,
                spaceId=space_id,
            )

        # Burn the nonce only after the MAC verified: a guessed MAC must not
        # poison this device's replay window.
        self._remember_nonce(device_id, nonce, ts, now)
        return VerifiedRequest(
            device_id=device_id,
            author=author,
            operation=operation,
            session_id=session_id,
        )

    def authorize(self, verified: VerifiedRequest, binding: object | None) -> None:
        """The authorization matrix, applied to one verified request."""

        from hyprial.shell.impl.gui.adapter.authz import (
            AuthorizationMatrix,
            matrix_denial,
        )

        denial = matrix_denial(AuthorizationMatrix(), verified, binding)
        if denial is not None:
            raise AppAdapterError(
                NOT_AUTHORIZED,
                "the device row's owner may not run this operation on this session",
                denial,
            )

    # -- internals ----------------------------------------------------------

    def _request_key(self, client_public: str) -> bytes:
        cached = self._keys.get(client_public)
        if cached is not None:
            return cached
        try:
            key = self._derive("server", client_public)
        except DeriveUnsupported as error:
            raise _refusal(
                DERIVE_UNSUPPORTED,
                "the sidecar cannot derive request keys "
                "(SIDECAR_DERIVE_UNSUPPORTED); no unsigned downgrade exists",
                REASON_SIDECAR_DERIVE_UNSUPPORTED,
            ) from error
        except Exception as error:  # noqa: BLE001 - any derive fault is unavailable
            raise _refusal(
                DERIVE_UNAVAILABLE,
                "the derive source is unavailable",
                REASON_DERIVE_UNAVAILABLE,
            ) from error
        if not isinstance(key, bytes) or not key:
            raise _refusal(
                DERIVE_UNAVAILABLE,
                "the derive source returned no usable key",
                REASON_DERIVE_UNAVAILABLE,
            )
        with self._lock:
            if len(self._keys) >= DERIVE_CACHE_SIZE:
                self._keys.clear()
            self._keys[client_public] = key
        return key

    def _remember_nonce(self, device_id: str, nonce: str, ts: int, now: float) -> None:
        with self._lock:
            # Normative horizon = ts + window; pruning follows it.  The
            # capacity below is local hardening and never normative.
            for known, expiry in list(self._seen.items()):
                if expiry <= now:
                    del self._seen[known]
            entry = (device_id, nonce)
            if entry in self._seen:
                raise _refusal(
                    NONCE_REPLAYED,
                    "this nonce was already used inside the replay window",
                    REASON_REPLAYED_REQUEST,
                    deviceId=device_id,
                )
            if len(self._seen) >= self._capacity:
                raise _refusal(
                    NONCE_WINDOW_FULL,
                    "the nonce window is at capacity; refusing instead of "
                    "evicting live replay evidence (local hardening)",
                    # deliberately no reference reason: not part of the spec
                    "nonce-window-full",
                    capacity=self._capacity,
                )
            self._seen[entry] = ts + self._window
