"""R2 verified OrgFS binding rows and resolver integration.

Audience shapes (RFC 7519 §4.1.3: `aud` is a string or an array of strings).
Casdoor signs the binding access token with a one-element array, so comparing
`aud` against the client id as a plain string rejected every otherwise valid
proof: 22,808 `identity.org-binding.ignored` rows on HQ across 6 members
(2026-10-20T11:34Z .. 2026-11-02T13:38Z).  The cases at the end of this module
pin both directions against the real `bindings()` path with RS256-signed
tokens: the shapes that must be accepted, and the shapes that must stay
refused.  Case intent lives in the `id=` of each parameter, not in a comment.
"""

from __future__ import annotations

import base64
import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hyprial.daemon.impl.identity.bindings import (
    ORG_BINDING_CACHE_FILENAME,
    OrgBindingCache,
    OrgBindingCacheProjector,
    OrgBindingSource,
)
from hyprial.daemon.impl.identity.resolver import IdentityResolver, IdentityResolverError
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import UserStore, identity_slug
from hyprial.kernel import ipc_errors


CLIENT_ID = "1be2763a29154030fbcb"
ALICE = "alice@example.com"
BOB = "bob@example.com"
UNION = "on_shared"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _integer(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwt(key, claims: dict[str, object], *, kid: str = "test-key") -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    payload = _b64(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


@contextmanager
def _jwks_server(public_key):
    numbers = public_key.public_numbers()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/.well-known/openid-configuration":
                body = {"issuer": server.issuer, "jwks_uri": f"{server.issuer}/jwks"}
            elif self.path == "/jwks":
                body = {
                    "keys": [
                        {
                            "kty": "RSA",
                            "kid": "test-key",
                            "alg": "RS256",
                            "use": "sig",
                            "n": _integer(numbers.n),
                            "e": _integer(numbers.e),
                        }
                    ]
                }
            else:
                self.send_error(404)
                return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.issuer = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Directory:
    def __init__(self, row: dict[str, object], *, path_user: str | None = None):
        self.row = row
        self.path_user = path_user or identity_slug(str(row["user"]))
        self.members = {str(row["user"])}

    def orgs(self) -> list[str]:
        return ["group-example"]

    def list_binding_rows(self, _org: str):
        return [(self.path_user, dict(self.row))]

    def is_member(self, _org: str, user: str) -> bool:
        return user in self.members


@pytest.fixture
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _claims(issuer: str, **changes: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": CLIENT_ID,
        "owner": "hyprial",
        "name": ALICE,
        "oauth_Lark_unionId": UNION,
        "iat": 1,
        "exp": 2,
    }
    claims.update(changes)
    return claims


def _row(proof: str, **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "user": ALICE,
        "larkUnionId": UNION,
        "proof": proof,
        "publishedAt": datetime(2026, 10, 7, tzinfo=UTC).isoformat(),
    }
    row.update(changes)
    return row


def _source(directory: _Directory, issuer: str, logs: list[tuple] | None = None):
    return OrgBindingSource(
        directory=directory,
        issuer=issuer,
        client_id=CLIENT_ID,
        logger=(
            (lambda level, event, **fields: logs.append((level, event, fields)))
            if logs is not None
            else None
        ),
    )


def test_valid_expired_proof_resolves_and_lists_as_org_directory(
    tmp_path: Path, key
) -> None:
    with _jwks_server(key.public_key()) as server:
        row = _row(_jwt(key, _claims(server.issuer)))
        source = _source(_Directory(row), server.issuer)
        resolver = IdentityResolver(
            state_dir=tmp_path / "state",
            hyprial_home=tmp_path / "home",
            owner="owner@example.com",
            org_bindings=source.bindings,
        )

        resolved = resolver.resolve(platform="lark", union_id=UNION)
        listed = resolver.bindings(source="org-directory")

    assert resolved == {
        "user": identity_slug(ALICE),
        "kind": "member",
        "source": "org-directory",
        "verified": True,
        "accounts": [],
    }
    assert len(listed) == 1
    assert listed[0]["user"] == identity_slug(ALICE)
    assert listed[0]["unionId"] == UNION
    assert listed[0]["source"] == "org-directory"
    assert isinstance(listed[0]["updatedAt"], int)


def test_daemon_publishes_exact_binding_row_into_every_org(tmp_path: Path, key) -> None:
    home = tmp_path / "home"
    home.mkdir()
    row = _row(_jwt(key, _claims("https://issuer.invalid")))
    (home / "settings.json").write_text(
        json.dumps({"identityBinding": row}), encoding="utf-8"
    )

    class Directory:
        def __init__(self) -> None:
            self.published: list[tuple[str, dict[str, object]]] = []

        def orgs(self) -> list[str]:
            return ["group-one", "group-two"]

        def put_binding(self, org: str, record: dict[str, object]) -> None:
            self.published.append((org, dict(record)))

    directory = Directory()
    app = object.__new__(DaemonApplication)
    app.hyprial_home = home
    app._org_binding_directory = lambda: directory  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    app._publish_identity_binding()

    assert directory.published == [
        ("group-one", row),
        ("group-two", row),
    ]
    assert all(
        set(record) == {"user", "larkUnionId", "proof", "publishedAt"}
        for _org, record in directory.published
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "forged-signature",
        "wrong-aud",
        "wrong-iss",
        "union-mismatch",
        "name-mismatch",
        "removed-member",
        "wrong-path-user",
    ],
)
def test_each_invalid_binding_guard_ignores_the_row_and_logs(
    key, mutation: str
) -> None:
    with _jwks_server(key.public_key()) as server:
        claims = _claims(server.issuer)
        row_changes: dict[str, object] = {}
        signing_key = key
        if mutation == "forged-signature":
            signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        elif mutation == "wrong-aud":
            claims["aud"] = "wrong-client"
        elif mutation == "wrong-iss":
            claims["iss"] = f"{server.issuer}/wrong"
        elif mutation == "union-mismatch":
            row_changes["larkUnionId"] = "on_other"
        elif mutation == "name-mismatch":
            claims["name"] = BOB
        row = _row(_jwt(signing_key, claims), **row_changes)
        directory = _Directory(
            row,
            path_user=(identity_slug(BOB) if mutation == "wrong-path-user" else None),
        )
        if mutation == "removed-member":
            directory.members.clear()
        logs: list[tuple] = []

        assert _source(directory, server.issuer, logs).bindings() == ()

    assert logs
    assert logs[-1][1] == "identity.org-binding.ignored"


def test_jwks_unreachable_is_source_unavailable(tmp_path: Path, key) -> None:
    with _jwks_server(key.public_key()) as server:
        issuer = server.issuer
        row = _row(_jwt(key, _claims(issuer)))
    source = _source(_Directory(row), issuer)
    resolver = IdentityResolver(
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        owner="owner@example.com",
        org_bindings=source.bindings,
    )

    with pytest.raises(IdentityResolverError) as caught:
        resolver.resolve(platform="lark", union_id=UNION)

    assert caught.value.code == ipc_errors.IDENTITY_SOURCE_UNAVAILABLE


def test_cached_bindings_and_owner_resolve_without_network(
    tmp_path: Path, key, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(
        json.dumps(
                {
                    "owner": "owner@example.com",
                    "identityMode": "casdoor",
                    "identityClaims": {
                    "properties": {"oauth_Lark_unionId": "on_owner"}
                },
            }
        ),
        encoding="utf-8",
    )
    with _jwks_server(key.public_key()) as server:
        source = _source(
            _Directory(_row(_jwt(key, _claims(server.issuer)))), server.issuer
        )
        cache = OrgBindingCache(state, source=source)
        assert cache.rebuild()

    monkeypatch.setattr(
        "hyprial.daemon.impl.identity.bindings.urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("resolve path attempted network I/O"),
    )
    resolver = IdentityResolver(
        state_dir=state,
        hyprial_home=home,
        owner="owner@example.com",
    )

    assert resolver.resolve(platform="lark", union_id="on_owner")["source"] == (
        "casdoor-login"
    )
    assert resolver.resolve(platform="lark", union_id=UNION)["source"] == (
        "org-directory"
    )


def test_unknown_kid_refreshes_jwks_once_and_cache_never_contains_proof(
    tmp_path: Path, key
) -> None:
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    directory = _Directory(_row("placeholder"))
    jwks_calls: list[int] = []

    class Source(OrgBindingSource):
        def _request_json(self, url: str):
            if url.endswith("openid-configuration"):
                return {"issuer": "https://issuer.invalid", "jwks_uri": "https://issuer.invalid/jwks"}
            jwks_calls.append(1)
            public = key.public_key() if len(jwks_calls) == 1 else rotated.public_key()
            numbers = public.public_numbers()
            kid = "old-key" if len(jwks_calls) == 1 else "new-key"
            return {
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": kid,
                        "alg": "RS256",
                        "n": _integer(numbers.n),
                        "e": _integer(numbers.e),
                    }
                ]
            }

    directory.row = _row(
        _jwt(rotated, _claims("https://issuer.invalid"), kid="new-key")
    )
    cache = OrgBindingCache(
        tmp_path / "state",
        source=Source(
            directory=directory,
            issuer="https://issuer.invalid",
            client_id=CLIENT_ID,
        ),
    )

    assert cache.rebuild()
    assert len(jwks_calls) == 2
    payload = (tmp_path / "state" / ORG_BINDING_CACHE_FILENAME).read_text(
        encoding="utf-8"
    )
    assert "proof" not in payload
    assert set(json.loads(payload)["bindings"][0]) == {
        "user",
        "userKey",
        "unionId",
        "issuedAtMs",
    }
    assert (tmp_path / "state" / ORG_BINDING_CACHE_FILENAME).stat().st_mode & 0o777 == 0o600


def test_membership_removal_drops_cached_binding_during_jwks_outage(
    tmp_path: Path, key
) -> None:
    with _jwks_server(key.public_key()) as server:
        directory = _Directory(_row(_jwt(key, _claims(server.issuer))))
        source = _source(directory, server.issuer)
        cache = OrgBindingCache(tmp_path / "state", source=source)
        assert cache.rebuild()

    class ChangedDirectory(_Directory):
        def list_binding_rows(self, _org: str):
            return [
                (identity_slug(ALICE), dict(self.row)),
                (
                    identity_slug(BOB),
                    {
                        **self.row,
                        "user": BOB,
                        "larkUnionId": "on_bob",
                    },
                ),
            ]

    changed = ChangedDirectory(directory.row)
    changed.members = {BOB}
    source._directory = changed
    source._keys = None
    assert not cache.rebuild()
    assert cache.bindings() == ()


def test_cache_projector_rebuilds_at_start_and_on_rows_and_membership() -> None:
    callbacks: dict[str, object] = {}

    class Registration:
        def close(self) -> None:
            callbacks["closed"] = True

    class Facade:
        def watch(self, space_id, glob, callback):
            assert (space_id, glob) == (
                "space-1",
                "directory/people/*/binding.json",
            )
            callbacks["row"] = callback
            return Registration()

        def watch_meta(self, space_id, callback):
            assert space_id == "space-1"
            callbacks["meta"] = callback
            return Registration()

    class Directory:
        def binding_watch_targets(self):
            return [("group-acme", "space-1")]

    class Cache:
        def __init__(self) -> None:
            self.rebuilds = 0
            self.changed = threading.Event()

        def rebuild(self) -> bool:
            self.rebuilds += 1
            self.changed.set()
            return True

    cache = Cache()
    projector = OrgBindingCacheProjector(
        cache=cache, directory=Directory(), facade=Facade()
    )
    try:
        projector.start()
        assert cache.changed.wait(2)
        assert cache.rebuilds == 1
        cache.changed.clear()
        callbacks["row"](object())
        assert cache.changed.wait(2)
        assert cache.rebuilds == 2
        cache.changed.clear()
        callbacks["meta"]()
        assert cache.changed.wait(2)
        assert cache.rebuilds == 3
    finally:
        projector.close()
    assert callbacks["closed"] is True


def test_projector_start_never_waits_on_the_first_verification() -> None:
    """Daemon startup must not block on JWKS; resolvers read the old cache."""

    class Registration:
        def close(self) -> None:
            return None

    class Facade:
        def watch(self, _space_id, _glob, _callback):
            return Registration()

        def watch_meta(self, _space_id, _callback):
            return Registration()

    class Directory:
        def binding_watch_targets(self):
            return [("group-acme", "space-1")]

    release = threading.Event()
    entered = threading.Event()

    class SlowCache:
        def rebuild(self) -> bool:
            entered.set()
            release.wait(10)
            return False

    projector = OrgBindingCacheProjector(
        cache=SlowCache(), directory=Directory(), facade=Facade()
    )
    started = threading.Thread(target=projector.start, daemon=True)
    try:
        started.start()
        started.join(2)
        assert not started.is_alive(), "start() waited on the verification"
        assert entered.wait(2)
    finally:
        release.set()
        projector.close()


def test_org_binding_conflicts_with_different_local_override(
    tmp_path: Path, key
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    home.mkdir(parents=True)
    users = UserStore(state / "users.sqlite3", hyprial_home=home)
    try:
        users.add_user(kind="member", owner=ALICE, confirmed_by="operator")
        bob = users.add_user(kind="member", owner=BOB, confirmed_by="operator")
    finally:
        users.close()
    with _jwks_server(key.public_key()) as server:
        source = _source(
            _Directory(_row(_jwt(key, _claims(server.issuer)))), server.issuer
        )
        resolver = IdentityResolver(
            state_dir=state,
            hyprial_home=home,
            owner="owner@example.com",
            org_bindings=source.bindings,
        )
        resolver.set_override(
            platform="lark",
            union_id=UNION,
            user_key=bob.user_key,
            confirmed_by="operator",
        )

        with pytest.raises(IdentityResolverError) as caught:
            resolver.resolve(platform="lark", union_id=UNION)

    assert caught.value.code == ipc_errors.IDENTITY_CONFLICT


_OTHER_CLIENT = "other-client-id"


@pytest.mark.parametrize(
    ("audience", "azp"),
    [
        pytest.param(CLIENT_ID, None, id="aud-string"),
        pytest.param([CLIENT_ID], None, id="aud-array-casdoor-form"),
        pytest.param([CLIENT_ID, _OTHER_CLIENT], CLIENT_ID, id="aud-multi-azp-is-us"),
    ],
)
def test_accepted_audience_shapes(key, audience, azp) -> None:
    with _jwks_server(key.public_key()) as server:
        changes: dict[str, object] = {"aud": audience}
        if azp is not None:
            changes["azp"] = azp
        row = _row(_jwt(key, _claims(server.issuer, **changes)))
        source = _source(_Directory(row), server.issuer)

        verified = source.bindings()

    assert len(verified) == 1
    assert verified[0].user == ALICE


@pytest.mark.parametrize(
    ("audience", "azp"),
    [
        pytest.param([_OTHER_CLIENT], None, id="aud-array-other-client"),
        pytest.param([], None, id="aud-empty-array"),
        pytest.param([CLIENT_ID, _OTHER_CLIENT], _OTHER_CLIENT, id="azp-other-client"),
        pytest.param([CLIENT_ID, _OTHER_CLIENT], None, id="aud-multi-no-azp"),
        pytest.param(None, None, id="aud-absent"),
        pytest.param(12345, None, id="aud-wrong-type"),
        pytest.param([CLIENT_ID, 7], CLIENT_ID, id="aud-non-string-member"),
    ],
)
def test_refused_audience_shapes(key, audience, azp) -> None:
    with _jwks_server(key.public_key()) as server:
        changes: dict[str, object] = {"aud": audience}
        if azp is not None:
            changes["azp"] = azp
        claims = _claims(server.issuer, **changes)
        if audience is None:
            claims.pop("aud")
        row = _row(_jwt(key, claims))
        logs: list[tuple] = []

        assert _source(_Directory(row), server.issuer, logs).bindings() == ()

    assert logs
    assert logs[-1][1] == "identity.org-binding.ignored"
    assert logs[-1][2]["reason"] == "binding proof audience does not match"
