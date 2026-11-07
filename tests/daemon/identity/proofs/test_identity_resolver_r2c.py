"""R2c security regressions for verified organization identity bindings."""

from __future__ import annotations

import base64
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.identity import bindings as binding_module
from hyprial.daemon.impl.identity.bindings import (
    OrgBindingCache,
    OrgBindingCacheProjector,
    OrgBindingSource,
    OrgBindingUnavailable,
)
from hyprial.daemon.impl.identity.resolver import IdentityResolver, IdentityResolverError
from hyprial.identity import identity_slug
from hyprial.identity import binding_assertion_publish_after
from hyprial.kernel import IDENTITY_BINDING_PUBLISH_SKEW_SECONDS, ipc_errors


CLIENT_ID = "1be2763a29154030fbcb"
ALICE = "alice@example.com"
BOB = "bob@example.com"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _integer(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwt(
    key: rsa.RSAPrivateKey,
    *,
    issuer: str,
    user: str = ALICE,
    union_id: str = "on_alice",
    owner: str | None = "hyprial",
    kid: str = "key-a",
    issued_at: object = 100,
    expires_at: object | None = 110,
) -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": CLIENT_ID,
        "name": user,
        "oauth_Lark_unionId": union_id,
        "iat": issued_at,
    }
    if owner is not None:
        claims["owner"] = owner
    if expires_at is not None:
        claims["exp"] = expires_at
    payload = _b64(json.dumps(claims).encode())
    signed = f"{header}.{payload}".encode()
    signature = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "n": _integer(numbers.n),
        "e": _integer(numbers.e),
    }


def _row(
    proof: str,
    *,
    user: str = ALICE,
    union_id: str = "on_alice",
    published_at: object = "2026-10-07T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "user": user,
        "larkUnionId": union_id,
        "proof": proof,
        "publishedAt": published_at,
    }


class _Directory:
    def __init__(self, rows: list[tuple[str, dict[str, object]]]) -> None:
        self.rows = rows
        self.members = {str(row["user"]) for _path, row in rows}

    def orgs(self) -> list[str]:
        return ["group-example"]

    def list_binding_rows(self, _org: str):
        return [(path, dict(row)) for path, row in self.rows]

    def is_member(self, _org: str, user: str) -> bool:
        return user in self.members

    def binding_watch_targets(self):
        return []


class _StaticSource(OrgBindingSource):
    def __init__(
        self,
        *,
        directory: _Directory,
        issuer: str,
        documents: list[dict[str, object]],
        clock=lambda: 1_000.0,
        monotonic=lambda: 1_000.0,
        logs: list[tuple] | None = None,
    ) -> None:
        self.documents = list(documents)
        self.calls: list[str] = []
        super().__init__(
            directory=directory,
            issuer=issuer,
            client_id=CLIENT_ID,
            clock=clock,
            monotonic=monotonic,
            logger=(
                (lambda level, event, **fields: logs.append((level, event, fields)))
                if logs is not None
                else None
            ),
        )

    def _request_json(self, url: str):
        self.calls.append(url)
        if url.endswith("openid-configuration"):
            return {"issuer": self._issuer, "jwks_uri": f"{self._issuer}/jwks"}
        if len(self.documents) > 1:
            return self.documents.pop(0)
        return self.documents[0]


def _source(
    directory: _Directory,
    key: rsa.RSAPrivateKey,
    *,
    clock=lambda: 1_000.0,
    monotonic=lambda: 1_000.0,
    logs: list[tuple] | None = None,
) -> _StaticSource:
    return _StaticSource(
        directory=directory,
        issuer="https://issuer.invalid",
        documents=[{"keys": [_jwk(key, "key-a")]}],
        clock=clock,
        monotonic=monotonic,
        logs=logs,
    )


@pytest.fixture
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize("owner", [None, "built-in", "dipc"])
def test_binding_owner_other_than_hyprial_is_ignored_per_row(
    key: rsa.RSAPrivateKey, owner: str | None
) -> None:
    logs: list[tuple] = []
    row = _row(
        _jwt(key, issuer="https://issuer.invalid", owner=owner, expires_at=10)
    )

    assert _source(_Directory([(identity_slug(ALICE), row)]), key, logs=logs).bindings() == ()
    assert logs[-1][1] == "identity.org-binding.ignored"


def test_binding_owner_hyprial_resolves_as_org_directory(
    key: rsa.RSAPrivateKey, tmp_path: Path
) -> None:
    row = _row(
        _jwt(key, issuer="https://issuer.invalid", owner="hyprial", expires_at=10)
    )
    source = _source(_Directory([(identity_slug(ALICE), row)]), key)
    resolver = IdentityResolver(
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        owner="owner@example.com",
        org_bindings=source.bindings,
    )

    assert resolver.resolve(platform="lark", union_id="on_alice")["source"] == (
        "org-directory"
    )


def test_live_or_exp_less_binding_is_never_published_from_daemon(
    tmp_path: Path, key: rsa.RSAPrivateKey
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    future = int(time.time()) + 3_600
    rows = [
        _row(_jwt(key, issuer="https://issuer.invalid", expires_at=future)),
        _row(
            _jwt(key, issuer="https://issuer.invalid", expires_at=None),
            union_id="on_missing_exp",
        ),
    ]

    class Directory:
        def __init__(self) -> None:
            self.published: list[dict[str, object]] = []

        def orgs(self) -> list[str]:
            return ["group-example"]

        def put_binding(self, _org: str, record: dict[str, object]) -> None:
            self.published.append(dict(record))

    directory = Directory()
    app = object.__new__(DaemonApplication)
    app.hyprial_home = home
    app._org_binding_directory = lambda: directory  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    app._org_identity_binding_projector = None

    for row in rows:
        (home / "settings.json").write_text(
            json.dumps({"identityBinding": row}), encoding="utf-8"
        )
        app._publish_identity_binding()

    assert directory.published == []


def test_projector_publishes_once_after_deferred_deadline() -> None:
    published = threading.Event()
    rebuilt = threading.Event()

    class Cache:
        def rebuild(self) -> bool:
            rebuilt.set()
            return True

    class Directory:
        def binding_watch_targets(self):
            return []

    projector = OrgBindingCacheProjector(
        cache=Cache(), directory=Directory(), facade=object()
    )
    try:
        projector.start()
        assert rebuilt.wait(1.0)
        time.sleep(0.02)
        projector.schedule_publish(time.time() + 0.05, published.set)
        assert published.wait(1.0)
    finally:
        projector.close()


def _wait_until(predicate, *, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_projector_periodically_drops_binding_signed_by_retired_key(
    key: rsa.RSAPrivateKey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    row = _row(_jwt(key, issuer="https://issuer.invalid", expires_at=10))
    directory = _Directory([(identity_slug(ALICE), row)])
    source = _StaticSource(
        directory=directory,
        issuer="https://issuer.invalid",
        documents=[
            {"keys": [_jwk(key, "key-a")]},
            {"keys": [_jwk(rotated, "key-b")]},
        ],
        monotonic=time.monotonic,
    )
    cache = OrgBindingCache(tmp_path / "state", source=source)
    monkeypatch.setattr(binding_module, "IDENTITY_BINDING_JWKS_TTL_SECONDS", 0.05)
    projector = OrgBindingCacheProjector(
        cache=cache, directory=directory, facade=object()
    )

    try:
        projector.start()
        assert _wait_until(lambda: [item.user for item in cache.bindings()] == [ALICE])
        assert _wait_until(lambda: cache.bindings() == ())
    finally:
        projector.close()

    assert sum(url.endswith("/jwks") for url in source.calls) >= 2


def test_projector_rechecks_row_when_its_proof_expires(
    key: rsa.RSAPrivateKey, tmp_path: Path
) -> None:
    expires_at = time.time() + 0.1
    row = _row(
        _jwt(key, issuer="https://issuer.invalid", expires_at=expires_at)
    )
    directory = _Directory([(identity_slug(ALICE), row)])
    source = _source(directory, key, clock=time.time, monotonic=time.monotonic)
    cache = OrgBindingCache(tmp_path / "state", source=source)
    projector = OrgBindingCacheProjector(
        cache=cache, directory=directory, facade=object()
    )

    try:
        projector.start()
        assert _wait_until(lambda: [item.user for item in cache.bindings()] == [ALICE])
    finally:
        projector.close()


def test_projector_retries_failed_deferred_publication(
    tmp_path: Path,
    key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuilt = threading.Event()
    published = threading.Event()
    attempts = 0
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "identityBinding": _row(
                    _jwt(key, issuer="https://issuer.invalid", expires_at=10)
                )
            }
        ),
        encoding="utf-8",
    )

    class Cache:
        def rebuild(self) -> bool:
            rebuilt.set()
            return True

    class Directory:
        def binding_watch_targets(self):
            return []

        def orgs(self) -> list[str]:
            return ["group-example"]

        def put_binding(self, _org: str, _row: dict[str, object]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient publication failure")
            published.set()

    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_SECONDS", 0.02
    )
    directory = Directory()
    projector = OrgBindingCacheProjector(
        cache=Cache(), directory=directory, facade=object()
    )
    app = object.__new__(DaemonApplication)
    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app._org_binding_directory = lambda: directory  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    app._org_identity_binding_projector = projector
    try:
        projector.start()
        assert rebuilt.wait(1.0)
        projector.schedule_publish(
            time.time() + 0.01, app._publish_identity_binding
        )
        assert published.wait(1.0)
    finally:
        projector.close()

    assert attempts == 2


def test_reader_ignores_live_proof_then_accepts_it_after_expiry(
    key: rsa.RSAPrivateKey,
) -> None:
    now = [109.0]
    proof = _jwt(key, issuer="https://issuer.invalid", expires_at=110)
    directory = _Directory([(identity_slug(ALICE), _row(proof))])
    source = _source(directory, key, clock=lambda: now[0])

    assert source.bindings() == ()
    now[0] = 111.0
    assert [binding.user for binding in source.bindings()] == [ALICE]


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        pytest.param("exp", float("nan"), id="exp-nan"),
        pytest.param("exp", float("inf"), id="exp-inf"),
        pytest.param("exp", "110", id="exp-non-numeric"),
        pytest.param("iat", float("nan"), id="iat-nan"),
        pytest.param("iat", float("inf"), id="iat-inf"),
        pytest.param("iat", "100", id="iat-non-numeric"),
        pytest.param("exp", 1.7e308, id="exp-huge-float"),
        pytest.param("exp", 10**400, id="exp-huge-int"),
        pytest.param("iat", 1.7e308, id="iat-huge-float"),
        pytest.param("iat", 10**400, id="iat-huge-int"),
    ],
)
def test_non_finite_or_non_numeric_numeric_claim_costs_only_its_row(
    key: rsa.RSAPrivateKey, claim: str, value: object
) -> None:
    proof_kwargs = {"issued_at": 100, "expires_at": 10}
    proof_kwargs[{"iat": "issued_at", "exp": "expires_at"}[claim]] = value
    bad = _row(
        _jwt(key, issuer="https://issuer.invalid", **proof_kwargs)
    )
    good = _row(
        _jwt(
            key,
            issuer="https://issuer.invalid",
            user=BOB,
            union_id="on_bob",
            expires_at=10,
        ),
        user=BOB,
        union_id="on_bob",
    )
    source = _source(
        _Directory(
            [(identity_slug(ALICE), bad), (identity_slug(BOB), good)]
        ),
        key,
    )

    assert [binding.user for binding in source.bindings()] == [BOB]


def test_binding_publisher_uses_registered_30_to_60_second_margin(
    key: rsa.RSAPrivateKey,
) -> None:
    proof = _jwt(key, issuer="https://issuer.invalid", expires_at=100)

    assert 30.0 <= IDENTITY_BINDING_PUBLISH_SKEW_SECONDS <= 60.0
    assert binding_assertion_publish_after(proof) == (
        100.0 + IDENTITY_BINDING_PUBLISH_SKEW_SECONDS
    )


def test_malformed_published_at_costs_only_its_row(
    key: rsa.RSAPrivateKey,
) -> None:
    alice = _row(
        _jwt(key, issuer="https://issuer.invalid", expires_at=10),
        published_at="timezone-less",
    )
    bob = _row(
        _jwt(
            key,
            issuer="https://issuer.invalid",
            user=BOB,
            union_id="on_bob",
            expires_at=10,
        ),
        user=BOB,
        union_id="on_bob",
    )
    logs: list[tuple] = []
    source = _source(
        _Directory(
            [(identity_slug(ALICE), alice), (identity_slug(BOB), bob)]
        ),
        key,
        logs=logs,
    )

    assert [binding.user for binding in source.bindings()] == [BOB]
    assert logs[-1][1] == "identity.org-binding.ignored"


def test_cache_drops_only_bad_row_and_checks_user_key(tmp_path: Path) -> None:
    path = tmp_path / "state" / binding_module.ORG_BINDING_CACHE_FILENAME
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "user": ALICE,
                        "userKey": "wrong-key",
                        "unionId": "on_alice",
                        "issuedAtMs": 100_000,
                    },
                    {
                        "user": BOB,
                        "userKey": identity_slug(BOB),
                        "unionId": "on_bob",
                        "issuedAtMs": 100_000,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    assert [binding.user for binding in OrgBindingCache(tmp_path / "state").bindings()] == [
        BOB
    ]


def test_signed_iat_not_published_at_drives_updated_at(
    tmp_path: Path, key: rsa.RSAPrivateKey
) -> None:
    proof = _jwt(
        key, issuer="https://issuer.invalid", issued_at=123, expires_at=124
    )
    rows = [
        (identity_slug(ALICE), _row(proof, published_at="2026-01-01T00:00:00Z")),
        (identity_slug(ALICE), _row(proof, published_at="2027-01-01T00:00:00Z")),
    ]
    source = _source(_Directory(rows), key)
    resolver = IdentityResolver(
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        owner="owner@example.com",
        org_bindings=source.bindings,
    )

    listed = resolver.bindings(source="org-directory")
    assert [row["updatedAt"] for row in listed] == [123_000]


def test_expired_jwks_ttl_refetch_removes_retired_key(
    key: rsa.RSAPrivateKey,
) -> None:
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elapsed = [0.0]
    row = _row(_jwt(key, issuer="https://issuer.invalid", expires_at=10))
    source = _StaticSource(
        directory=_Directory([(identity_slug(ALICE), row)]),
        issuer="https://issuer.invalid",
        documents=[
            {"keys": [_jwk(key, "key-a")]},
            {"keys": [_jwk(rotated, "key-b")]},
        ],
        monotonic=lambda: elapsed[0],
    )

    assert [binding.user for binding in source.bindings()] == [ALICE]
    elapsed[0] = binding_module.IDENTITY_BINDING_JWKS_TTL_SECONDS + 1
    assert source.bindings() == ()
    assert sum(url.endswith("/jwks") for url in source.calls) == 2


def test_unknown_kids_refresh_once_and_do_not_hide_good_row(
    key: rsa.RSAPrivateKey,
) -> None:
    rows = [
        (
            identity_slug(ALICE),
            _row(
                _jwt(
                    key,
                    issuer="https://issuer.invalid",
                    union_id="on_unknown_one",
                    kid="unknown-one",
                    expires_at=10,
                ),
                union_id="on_unknown_one",
            ),
        ),
        (
            identity_slug(BOB),
            _row(
                _jwt(
                    key,
                    issuer="https://issuer.invalid",
                    user=BOB,
                    union_id="on_unknown_two",
                    kid="unknown-two",
                    expires_at=10,
                ),
                user=BOB,
                union_id="on_unknown_two",
            ),
        ),
        (
            identity_slug("carol@example.com"),
            _row(
                _jwt(
                    key,
                    issuer="https://issuer.invalid",
                    user="carol@example.com",
                    union_id="on_carol",
                    expires_at=10,
                ),
                user="carol@example.com",
                union_id="on_carol",
            ),
        ),
    ]
    source = _StaticSource(
        directory=_Directory(rows),
        issuer="https://issuer.invalid",
        documents=[
            {"keys": [_jwk(key, "key-a")]},
            {"keys": [_jwk(key, "key-a")]},
        ],
    )

    assert [binding.user for binding in source.bindings()] == [
        "carol@example.com"
    ]
    assert sum(url.endswith("/jwks") for url in source.calls) == 2


def test_unknown_kid_refresh_outage_is_only_a_row_failure(
    key: rsa.RSAPrivateKey,
) -> None:
    rows = [
        (
            identity_slug(ALICE),
            _row(
                _jwt(
                    key,
                    issuer="https://issuer.invalid",
                    kid="unknown",
                    expires_at=10,
                )
            ),
        ),
        (
            identity_slug(BOB),
            _row(
                _jwt(
                    key,
                    issuer="https://issuer.invalid",
                    user=BOB,
                    union_id="on_bob",
                    expires_at=10,
                ),
                user=BOB,
                union_id="on_bob",
            ),
        ),
    ]

    class Source(OrgBindingSource):
        def __init__(self) -> None:
            self.discovery_calls = 0
            super().__init__(
                directory=_Directory(rows),
                issuer="https://issuer.invalid",
                client_id=CLIENT_ID,
                clock=lambda: 1_000.0,
            )

        def _request_json(self, url: str):
            if url.endswith("openid-configuration"):
                self.discovery_calls += 1
                if self.discovery_calls > 1:
                    raise OrgBindingUnavailable("rotation endpoint unavailable")
                return {
                    "issuer": self._issuer,
                    "jwks_uri": f"{self._issuer}/jwks",
                }
            return {"keys": [_jwk(key, "key-a")]}

    assert [binding.user for binding in Source().bindings()] == [BOB]


@contextmanager
def _redirecting_issuer(key: rsa.RSAPrivateKey):
    class Handler(BaseHTTPRequestHandler):
        followed = False

        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/.well-known/openid-configuration":
                self.send_response(302)
                self.send_header("Location", "/redirected-discovery")
                self.end_headers()
                return
            if self.path == "/redirected-discovery":
                type(self).followed = True
                body = {
                    "issuer": server.issuer,
                    "jwks_uri": f"{server.issuer}/jwks",
                }
            else:
                body = {"keys": [_jwk(key, "key-a")]}
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.issuer = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_discovery_redirect_is_not_followed(key: rsa.RSAPrivateKey) -> None:
    with _redirecting_issuer(key) as (server, handler):
        row = _row(_jwt(key, issuer=server.issuer, expires_at=10))
        source = OrgBindingSource(
            directory=_Directory([(identity_slug(ALICE), row)]),
            issuer=server.issuer,
            client_id=CLIENT_ID,
        )
        with pytest.raises(OrgBindingUnavailable):
            source.bindings()
        assert handler.followed is False


@pytest.mark.parametrize(
    ("discovery_issuer", "jwks_uri"),
    [
        ("https://other.invalid", "https://issuer.invalid/jwks"),
        ("https://issuer.invalid", "https://other.invalid/jwks"),
    ],
)
def test_discovery_issuer_and_jwks_origin_are_independent_guards(
    key: rsa.RSAPrivateKey, discovery_issuer: str, jwks_uri: str
) -> None:
    row = _row(_jwt(key, issuer="https://issuer.invalid", expires_at=10))

    class Source(OrgBindingSource):
        def _request_json(self, url: str):
            if url.endswith("openid-configuration"):
                return {"issuer": discovery_issuer, "jwks_uri": jwks_uri}
            return {"keys": [_jwk(key, "key-a")]}

    source = Source(
        directory=_Directory([(identity_slug(ALICE), row)]),
        issuer="https://issuer.invalid",
        client_id=CLIENT_ID,
    )
    with pytest.raises(OrgBindingUnavailable):
        source.bindings()


def test_non_casdoor_mode_never_reads_stale_org_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"identityMode": "local", "owner": "owner@example.com"}),
        encoding="utf-8",
    )
    (state / binding_module.ORG_BINDING_CACHE_FILENAME).write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "user": ALICE,
                        "userKey": identity_slug(ALICE),
                        "unionId": "on_alice",
                        "issuedAtMs": 100_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    resolver = IdentityResolver(
        state_dir=state, hyprial_home=home, owner="owner@example.com"
    )

    with pytest.raises(IdentityResolverError) as caught:
        resolver.resolve(platform="lark", union_id="on_alice")
    assert caught.value.code == ipc_errors.IDENTITY_UNBOUND


def test_startup_binding_failures_are_logged_and_do_not_escape() -> None:
    app = object.__new__(DaemonApplication)
    calls: list[str] = []
    app._start_org_identity_binding_cache = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("watch failed")
    )
    app._publish_identity_binding = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("directory failed")
    )
    app._log = (  # type: ignore[method-assign]
        lambda _level, _domain, event, **_fields: calls.append(event)
    )

    app._start_org_identity_bindings_safely()

    assert calls == [
        "identity.org-binding.cache-start-failed",
        "identity.org-binding.publish-start-failed",
    ]


def test_transport_startup_uses_safe_binding_initializer() -> None:
    source = Path(
        "src/hyprial/daemon/impl/application/wiring/transport.py"
    ).read_text(encoding="utf-8")
    assert "self._start_org_identity_bindings_safely()" in source
    assert "self._start_org_identity_binding_cache()" not in source
    assert "self._publish_identity_binding()" not in source
