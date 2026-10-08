"""Verified Casdoor binding proofs read from organization directories.

JWKS keys are retained only for the registered TTL.  Removing a key at the
issuer therefore stops local verification within that window.  A deliberate
rotation de-verifies proofs signed by the retired key; affected members must
log in again to publish a new, expired assertion signed by the replacement.
"""

from __future__ import annotations

import base64
import json
import math
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hyprial.daemon import url_opener
from hyprial.daemon.impl.bootstrap.discovery import DISCOVERY_TIMEOUT_SECONDS
from hyprial.identity import (
    BINDING_ASSERTION_OWNER,
    binding_numeric_date,
    identity_slug,
)
from hyprial.kernel import (
    IDENTITY_BINDING_JWKS_TTL_SECONDS,
    IDENTITY_BINDING_PUBLISH_RETRY_MAX_SECONDS,
    IDENTITY_BINDING_PUBLISH_RETRY_SECONDS,
    IDENTITY_BINDING_PUBLISH_WARNING_INTERVAL_SECONDS,
    IDENTITY_BINDING_UNKNOWN_KID_REFRESH_INTERVAL_SECONDS,
    atomic_json_write,
    capped_exponential,
)


class _Directory(Protocol):
    def orgs(self) -> list[str]: ...

    def list_binding_rows(self, org: str) -> list[tuple[str, dict[str, Any]]]: ...

    def is_member(self, org: str, user: str) -> bool: ...


class OrgBindingUnavailable(RuntimeError):
    """The directory or Casdoor verification source cannot be read."""


@dataclass(frozen=True, slots=True)
class VerifiedOrgBinding:
    user_key: str
    user: str
    union_id: str
    issued_at_ms: int


_ROW_KEYS = frozenset({"user", "larkUnionId", "proof", "publishedAt"})
_CACHE_ROW_KEYS = frozenset({"user", "userKey", "unionId", "issuedAtMs"})
ORG_BINDING_CACHE_FILENAME = "verified-org-bindings.json"
_HTTP_TIMEOUT_S = DISCOVERY_TIMEOUT_SECONDS


def _decode_segment(value: str) -> bytes:
    if not value or len(value) > 65_536:
        raise ValueError("JWT segment is empty or too large")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_segment(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_decode_segment(value))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"binding proof {label} is invalid") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"binding proof {label} is not an object")
    return decoded


def _origin(value: str) -> tuple[str, str, int] | None:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return None
    port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
    return (parsed.scheme, parsed.hostname.lower(), port) if port else None


def _integer(value: object, label: str) -> int:
    if not isinstance(value, str):
        raise ValueError(f"JWKS {label} is not a string")
    try:
        return int.from_bytes(_decode_segment(value), "big")
    except ValueError as error:
        raise ValueError(f"JWKS {label} is invalid") from error


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"binding {label} must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"binding {label} must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"binding {label} must include a timezone")
    return parsed


class OrgBindingSource:
    """Rebuild the org-directory cache from rows verified on every read."""

    def __init__(
        self,
        *,
        directory: _Directory,
        issuer: str,
        client_id: str,
        logger: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._directory = directory
        self._issuer = issuer
        self._client_id = client_id
        self._logger = logger
        self._clock = clock
        self._monotonic = monotonic
        self._keys: dict[str, rsa.RSAPublicKey] | None = None
        self._keys_loaded_at: float | None = None
        self._last_unknown_kid_refresh_at: float | None = None
        self._next_recheck_at: float | None = None

    @property
    def next_recheck_at(self) -> float | None:
        """Earliest rejected live proof expiry observed by the last rebuild."""

        return self._next_recheck_at

    def _request_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        open_url, _route = url_opener(follow_redirects=False)
        try:
            with open_url(request, timeout=_HTTP_TIMEOUT_S) as response:
                if response.status != 200:
                    raise OrgBindingUnavailable(
                        f"binding verification endpoint answered HTTP {response.status}"
                    )
                return json.loads(response.read())
        except OrgBindingUnavailable:
            raise
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise OrgBindingUnavailable(
                f"binding verification source is unavailable: {type(error).__name__}"
            ) from error

    def _load_keys(self, *, refresh: bool = False) -> dict[str, rsa.RSAPublicKey]:
        now = self._monotonic()
        if (
            self._keys is not None
            and not refresh
            and self._keys_loaded_at is not None
            and now - self._keys_loaded_at < IDENTITY_BINDING_JWKS_TTL_SECONDS
        ):
            return self._keys
        discovery = self._request_json(
            f"{self._issuer}/.well-known/openid-configuration"
        )
        if not isinstance(discovery, dict) or discovery.get("issuer") != self._issuer:
            raise OrgBindingUnavailable("binding discovery issuer is invalid")
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str) or _origin(jwks_uri) != _origin(self._issuer):
            raise OrgBindingUnavailable("binding discovery JWKS endpoint is invalid")
        document = self._request_json(jwks_uri)
        values = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(values, list):
            raise OrgBindingUnavailable("binding JWKS has no keys")
        keys: dict[str, rsa.RSAPublicKey] = {}
        try:
            for value in values:
                if (
                    not isinstance(value, dict)
                    or value.get("kty") != "RSA"
                    or value.get("alg") not in {None, "RS256"}
                ):
                    continue
                kid = value.get("kid")
                if not isinstance(kid, str) or not kid:
                    continue
                keys[kid] = rsa.RSAPublicNumbers(
                    _integer(value.get("e"), "e"),
                    _integer(value.get("n"), "n"),
                ).public_key()
        except ValueError as error:
            raise OrgBindingUnavailable(str(error)) from error
        if not keys:
            raise OrgBindingUnavailable("binding JWKS has no usable RS256 keys")
        self._keys = keys
        self._keys_loaded_at = now
        return keys

    def _verified(self, path_user: str, row: Mapping[str, Any]) -> VerifiedOrgBinding:
        if set(row) != _ROW_KEYS:
            raise ValueError("binding row does not have exactly the frozen fields")
        user = row.get("user")
        union_id = row.get("larkUnionId")
        proof = row.get("proof")
        published_at = row.get("publishedAt")
        if not all(
            isinstance(value, str) and value
            for value in (user, union_id, proof, published_at)
        ):
            raise ValueError("binding row fields must be non-empty strings")
        assert isinstance(user, str)
        assert isinstance(union_id, str)
        assert isinstance(proof, str)
        assert isinstance(published_at, str)
        _timestamp(published_at, "publishedAt")
        if identity_slug(user) != path_user:
            raise ValueError("binding path user does not match row user")
        parts = proof.split(".")
        if len(parts) != 3:
            raise ValueError("binding proof is not a compact JWT")
        header = _json_segment(parts[0], "header")
        claims = _json_segment(parts[1], "claims")
        if header.get("alg") != "RS256":
            raise ValueError("binding proof algorithm is not RS256")
        kid = header.get("kid")
        keys_were_stale = (
            self._keys is not None
            and self._keys_loaded_at is not None
            and self._monotonic() - self._keys_loaded_at
            >= IDENTITY_BINDING_JWKS_TTL_SECONDS
        )
        keys = self._load_keys()
        if isinstance(kid, str) and kid not in keys and not keys_were_stale:
            now = self._monotonic()
            if (
                self._last_unknown_kid_refresh_at is None
                or now - self._last_unknown_kid_refresh_at
                >= IDENTITY_BINDING_UNKNOWN_KID_REFRESH_INTERVAL_SECONDS
            ):
                self._last_unknown_kid_refresh_at = now
                try:
                    keys = self._load_keys(refresh=True)
                except OrgBindingUnavailable as error:
                    raise ValueError(
                        "binding proof signing key refresh is unavailable"
                    ) from error
        if not isinstance(kid, str) or kid not in keys:
            raise ValueError("binding proof signing key is unknown")
        try:
            keys[kid].verify(
                _decode_segment(parts[2]),
                f"{parts[0]}.{parts[1]}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as error:
            raise ValueError("binding proof signature is invalid") from error
        if claims.get("iss") != self._issuer:
            raise ValueError("binding proof issuer does not match")
        if claims.get("aud") != self._client_id:
            raise ValueError("binding proof audience does not match")
        if claims.get("owner") != BINDING_ASSERTION_OWNER:
            raise ValueError("binding proof owner does not match")
        if claims.get("name") != user:
            raise ValueError("binding proof name does not match row user")
        if claims.get("oauth_Lark_unionId") != union_id:
            raise ValueError("binding proof union id does not match row")
        expires_at = binding_numeric_date(claims.get("exp"), "expiry")
        if expires_at >= self._clock():
            recheck_at = math.nextafter(float(expires_at), math.inf)
            if self._next_recheck_at is None or recheck_at < self._next_recheck_at:
                self._next_recheck_at = recheck_at
            raise ValueError("binding proof has not expired")
        issued_at = binding_numeric_date(claims.get("iat"), "issued-at")
        return VerifiedOrgBinding(
            identity_slug(user), user, union_id, int(issued_at * 1000)
        )

    def bindings(self) -> tuple[VerifiedOrgBinding, ...]:
        self._next_recheck_at = None
        verified: dict[tuple[str, str], VerifiedOrgBinding] = {}
        try:
            orgs = self._directory.orgs()
            rows = [
                (org, path_user, row)
                for org in orgs
                for path_user, row in self._directory.list_binding_rows(org)
            ]
        except Exception as error:  # noqa: BLE001 - source boundary
            raise OrgBindingUnavailable(
                f"organization directory is unavailable: {type(error).__name__}"
            ) from error
        for org, path_user, row in rows:
            try:
                user = row.get("user")
                if not isinstance(user, str) or not self._directory.is_member(org, user):
                    raise ValueError("binding row user is not a current org member")
                binding = self._verified(path_user, row)
            except OrgBindingUnavailable:
                raise
            except (TypeError, ValueError) as error:
                if self._logger is not None:
                    self._logger(
                        "warn",
                        "identity.org-binding.ignored",
                        org=org,
                        pathUser=path_user,
                        reason=str(error),
                    )
                continue
            verified[(binding.union_id, binding.user_key)] = binding
        return tuple(verified[key] for key in sorted(verified))

    def current_cached_members(
        self, bindings: tuple[VerifiedOrgBinding, ...]
    ) -> tuple[VerifiedOrgBinding, ...]:
        """Drop cached rows whose path disappeared or user left every org.

        This is deliberately proof-free.  It is used only after verification
        infrastructure failed, so it can revoke a stale verified fact from
        current local membership without trusting a replacement row.
        """

        current: set[tuple[str, str]] = set()
        try:
            for org in self._directory.orgs():
                for path_user, row in self._directory.list_binding_rows(org):
                    user = row.get("user")
                    if (
                        isinstance(user, str)
                        and path_user == identity_slug(user)
                        and self._directory.is_member(org, user)
                    ):
                        current.add((path_user, user))
        except Exception as error:  # noqa: BLE001 - source boundary
            raise OrgBindingUnavailable(
                f"organization directory is unavailable: {type(error).__name__}"
            ) from error
        return tuple(
            binding
            for binding in bindings
            if (binding.user_key, binding.user) in current
        )


class OrgBindingCache:
    """Proof-free, atomic resolver cache shared by daemon and workers."""

    def __init__(
        self,
        state_dir: Path,
        *,
        source: OrgBindingSource | None = None,
        logger: Callable[..., None] | None = None,
    ) -> None:
        self.path = Path(state_dir) / ORG_BINDING_CACHE_FILENAME
        self._source = source
        self._logger = logger

    def _log(self, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger("warn", event, **fields)

    @staticmethod
    def _record(binding: VerifiedOrgBinding) -> dict[str, object]:
        return {
            "user": binding.user,
            "userKey": binding.user_key,
            "unionId": binding.union_id,
            "issuedAtMs": binding.issued_at_ms,
        }

    def _write(self, bindings: tuple[VerifiedOrgBinding, ...]) -> None:
        atomic_json_write(
            self.path,
            {"bindings": [self._record(binding) for binding in bindings]},
        )

    @property
    def next_recheck_at(self) -> float | None:
        return self._source.next_recheck_at if self._source is not None else None

    def bindings(self) -> tuple[VerifiedOrgBinding, ...]:
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            rows = record.get("bindings") if isinstance(record, dict) else None
            if not isinstance(rows, list):
                raise ValueError("cache bindings must be an array")
        except FileNotFoundError:
            return ()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._log(
                "identity.org-binding.cache-invalid", reason=type(error).__name__
            )
            return ()
        values: dict[tuple[str, str], VerifiedOrgBinding] = {}
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict) or set(row) != _CACHE_ROW_KEYS:
                    raise ValueError("cache row does not have the frozen fields")
                if not all(
                    isinstance(row.get(key), str) and row.get(key)
                    for key in ("user", "userKey", "unionId")
                ):
                    raise ValueError("cache row identity fields must be non-empty strings")
                issued_at_ms = row.get("issuedAtMs")
                if isinstance(issued_at_ms, bool) or not isinstance(issued_at_ms, int):
                    raise ValueError("cache issuedAtMs must be an integer")
                binding = VerifiedOrgBinding(
                    user_key=row["userKey"],
                    user=row["user"],
                    union_id=row["unionId"],
                    issued_at_ms=issued_at_ms,
                )
                if binding.user_key != identity_slug(binding.user):
                    raise ValueError("cache userKey does not match user")
                values[(binding.union_id, binding.user_key)] = binding
            except (TypeError, ValueError) as error:
                self._log(
                    "identity.org-binding.cache-row-invalid",
                    index=index,
                    reason=str(error),
                )
        return tuple(values[key] for key in sorted(values))

    def rebuild(self) -> bool:
        """Verify current rows; retain only still-member cache on outage."""

        if self._source is None:
            raise RuntimeError("binding cache has no verification source")
        previous = self.bindings()
        try:
            verified = self._source.bindings()
        except OrgBindingUnavailable as error:
            try:
                current = self._source.current_cached_members(previous)
            except OrgBindingUnavailable:
                current = previous
            if current != previous:
                self._write(current)
            self._log(
                "identity.org-binding.verification-unavailable",
                reason=str(error),
                retained=len(current),
            )
            return False
        self._write(verified)
        return True


class OrgBindingCacheProjector:
    """Coalesced OrgFS row/meta watches feeding the proof-free cache."""

    def __init__(
        self,
        *,
        cache: Any,
        directory: Any,
        facade: Any,
        logger: Callable[..., None] | None = None,
    ) -> None:
        self._cache = cache
        self._logger = logger
        self._directory = directory
        self._facade = facade
        self._registrations: dict[tuple[str, str], Any] = {}
        self._dirty = threading.Event()
        self._stopping = threading.Event()
        self._publication_lock = threading.Lock()
        self._publication_at: float | None = None
        self._publisher: Callable[[], None] | None = None
        self._publication_retry_attempt = 0
        self._publication_warning_at = float("-inf")
        self._rebuild_at: float | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="hyprial-org-binding-cache",
            daemon=True,
        )
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        self._refresh_watches()
        # Off the startup path: the first verification may wait on JWKS, and
        # resolvers keep reading the last good cache file meanwhile.
        self.request_rebuild()

    def _refresh_watches(self) -> None:
        for _org, space_id in self._directory.binding_watch_targets():
            row_key = (space_id, "rows")
            if row_key not in self._registrations:
                self._registrations[row_key] = self._facade.watch(
                    space_id,
                    "directory/people/*/binding.json",
                    lambda _event: self.request_rebuild(),
                )
            meta_key = (space_id, "meta")
            if meta_key not in self._registrations:
                self._registrations[meta_key] = self._facade.watch_meta(
                    space_id, self.request_rebuild
                )

    def request_rebuild(self) -> None:
        if not self._stopping.is_set():
            self._dirty.set()

    def schedule_publish(
        self,
        publish_at: float,
        publisher: Callable[[], None],
        *,
        retry_attempt: int = 0,
    ) -> None:
        """Wake once at the earliest deferred proof-publication deadline."""

        with self._publication_lock:
            if self._publication_at is None or publish_at < self._publication_at:
                self._publication_at = publish_at
                self._publisher = publisher
                self._publication_retry_attempt = retry_attempt
        self._dirty.set()

    def retry_publish(
        self, publisher: Callable[[], None], *, attempt: int = 0
    ) -> None:
        """Retry an immediate publication failure on the projector thread."""

        delay = capped_exponential(
            IDENTITY_BINDING_PUBLISH_RETRY_SECONDS,
            IDENTITY_BINDING_PUBLISH_RETRY_MAX_SECONDS,
            attempt,
        )
        self.schedule_publish(
            time.time() + delay,
            publisher,
            retry_attempt=attempt + 1,
        )

    def refresh(self) -> None:
        """Discover newly joined orgs, then queue their first projection."""

        self._refresh_watches()
        self.request_rebuild()

    def _run(self) -> None:
        while not self._stopping.is_set():
            with self._publication_lock:
                publish_at = self._publication_at
            deadlines = [
                deadline
                for deadline in (publish_at, self._rebuild_at)
                if deadline is not None
            ]
            wake_at = min(deadlines) if deadlines else None
            timeout = None if wake_at is None else max(0.0, wake_at - time.time())
            woke = self._dirty.wait(timeout)
            if self._stopping.is_set():
                break
            now = time.time()
            rebuild_due = self._rebuild_at is not None and now >= self._rebuild_at
            if woke or rebuild_due:
                self._dirty.clear()
                try:
                    self._cache.rebuild()
                except Exception as error:  # noqa: BLE001 - later changes retry
                    if self._logger is not None:
                        self._logger(
                            "warn",
                            "identity.org-binding.cache-rebuild-failed",
                            reason=type(error).__name__,
                        )
                periodic_at = now + IDENTITY_BINDING_JWKS_TTL_SECONDS
                recheck_at = getattr(self._cache, "next_recheck_at", None)
                self._rebuild_at = (
                    min(periodic_at, recheck_at)
                    if isinstance(recheck_at, (int, float))
                    else periodic_at
                )
            with self._publication_lock:
                if (
                    self._publication_at is not None
                    and time.time() >= self._publication_at
                ):
                    publisher = self._publisher
                    retry_attempt = self._publication_retry_attempt
                    self._publication_at = None
                    self._publisher = None
                    self._publication_retry_attempt = 0
                else:
                    publisher = None
                    retry_attempt = 0
            if publisher is not None:
                try:
                    publisher()
                except Exception as error:  # noqa: BLE001 - later start/join retries
                    warning_now = time.monotonic()
                    if (
                        self._logger is not None
                        and warning_now - self._publication_warning_at
                        >= IDENTITY_BINDING_PUBLISH_WARNING_INTERVAL_SECONDS
                    ):
                        self._publication_warning_at = warning_now
                        self._logger(
                            "warn",
                            "identity.org-binding.deferred-publish-failed",
                            reason=type(error).__name__,
                        )
                    retry_publisher = getattr(error, "retry_publisher", publisher)
                    self.retry_publish(retry_publisher, attempt=retry_attempt)

    def close(self) -> None:
        if not self._started:
            return
        self._stopping.set()
        self._dirty.set()
        self._thread.join(timeout=DISCOVERY_TIMEOUT_SECONDS)
        for registration in self._registrations.values():
            registration.close()
        self._registrations.clear()


__all__ = [
    "ORG_BINDING_CACHE_FILENAME",
    "OrgBindingCache",
    "OrgBindingCacheProjector",
    "OrgBindingSource",
    "OrgBindingUnavailable",
    "VerifiedOrgBinding",
]
