"""Frozen identity-resolver IPC contract and R1 source semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark import Identity, LarkStateStore
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import UserStore
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.runtime._owned_applications import close_application


OWNER = "aaron@example.com"
USER_KEY = "aaron-example.com"
REPO = Path(__file__).resolve().parents[3]


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()


class _Presence:
    def actor_online(self, _actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[()]:
        return ()

    def raw_online_actors(self) -> tuple[()]:
        return ()

    def liveness_keeps(self, _actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[()]:
        return ()


class _Inbox:
    pass


def _settings(home: Path, *, union_id: str | None = None) -> None:
    record: dict[str, object] = {
        "owner": OWNER,
        "identityMode": "casdoor",
        "identityIssuer": "https://auth.hyprial.com",
    }
    if union_id is not None:
        # A non-secret projection of the claim carried by the local login
        # credential. The resolver must read only this allowlisted claim.
        record["identityClaims"] = {
            "properties": {"oauth_Lark_unionId": union_id}
        }
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(record), encoding="utf-8")


def _application(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    *,
    owner_union_id: str | None = None,
) -> DaemonApplication:
    home = tmp_path / "home"
    state = home / "state"
    _settings(home, union_id=owner_union_id)
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="identity-r1-node",
        hyprial_home=home,
        owner=OWNER,
        **daemon_dependencies(),
    )
    request.addfinalizer(lambda: close_application(app))
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    return app


def _users(app: DaemonApplication) -> UserStore:
    return UserStore(
        app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home
    )


def _legacy(app: DaemonApplication, adapter: str = "lark:main") -> LarkStateStore:
    return LarkStateStore(app.state_dir / "adapters.sqlite3", adapter=adapter)


def _member(store: UserStore, *, owner: str = OWNER) -> str:
    return store.add_user(
        kind="member", owner=owner, confirmed_by="operator"
    ).user_key


def test_casdoor_owner_resolves_through_frozen_ipc(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path, owner_union_id="on_allen")
    users = _users(app)
    try:
        assert _member(users) == USER_KEY
    finally:
        users.close()
    observed = _legacy(app)
    try:
        observed.observe_identity(
            "user",
            "ou_allen_main",
            union_id="on_allen",
            source="event-sender:oc_dm",
            now_ms=101,
        )
    finally:
        observed.close()

    assert app.handle(
        "identity.resolve", {"platform": "lark", "unionId": "on_allen"}
    ) == {
        "user": USER_KEY,
        "kind": "member",
        "source": "casdoor-login",
        "verified": True,
        "accounts": [
            {
                "adapter": "main",
                "openId": "ou_allen_main",
                "observedAtMs": 101,
                "source": "observed",
            }
        ],
    }


def test_local_override_and_legacy_sources_resolve_through_ipc(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    users = _users(app)
    try:
        user_key = _member(users)
        users.bind_account(
            user_key,
            adapter="lark:main",
            open_id="ou_override",
            union_id="on_override",
            confirmed_by="operator",
            source="local-override",
            now_ms=201,
        )
    finally:
        users.close()
    legacy = _legacy(app, "lark:legacy")
    try:
        legacy.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_legacy",
                union_id="on_legacy",
                hyprial_owner=OWNER,
                standing="verified",
                source="manual:pre-r1",
            ),
            now_ms=301,
        )
    finally:
        legacy.close()

    override = app.handle(
        "identity.resolve", {"platform": "lark", "unionId": "on_override"}
    )
    assert override["source"] == "local-override"
    assert override["confirmedBy"] == "operator"
    assert override["verified"] is True

    old = app.handle(
        "identity.resolve", {"adapter": "legacy", "openId": "ou_legacy"}
    )
    assert old["user"] == user_key
    assert old["source"] == "legacy-identities"
    assert old["verified"] is True


def test_bindings_list_exercises_all_four_frozen_source_values(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path, owner_union_id="on_allen")
    users = _users(app)
    try:
        user_key = _member(users)
        users.bind_account(
            user_key,
            adapter="lark:main",
            open_id="ou_override",
            union_id="on_override",
            confirmed_by="operator",
            source="local-override",
        )
        guest = users.add_user(kind="guest", nickname="Bound", confirmed_by="operator")
        users.bind_account(
            guest.user_key,
            adapter="lark:main",
            open_id="ou_bound",
            union_id="on_bound",
            confirmed_by="operator",
        )
    finally:
        users.close()
    legacy = _legacy(app)
    try:
        legacy.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_legacy",
                union_id="on_legacy",
                hyprial_owner=OWNER,
                standing="verified",
                source="manual:pre-r1",
            )
        )
    finally:
        legacy.close()

    assert app.handle(
        "identity.bindings.list", {"source": "org-directory"}
    ) == {"bindings": []}
    for source in (
        "casdoor-login",
        "local-override",
        "legacy-identities",
        "legacy-user-bind",
    ):
        rows = app.handle("identity.bindings.list", {"source": source})
        assert rows["bindings"]
        assert {row["source"] for row in rows["bindings"]} == {source}
    # The filter takes exactly the five binding-level values (§5 @ 5cbffb0e);
    # "override" is only an accounts[].source value.
    with pytest.raises(DaemonRequestError) as refused:
        app.handle("identity.bindings.list", {"source": "override"})
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    # Each legacy kind reports its own label (org-improver, §5 @ 80dea557).
    by_union = {
        row["unionId"]: row
        for row in app.handle("identity.bindings.list", {})["bindings"]
    }
    assert by_union["on_bound"]["source"] == "legacy-user-bind"
    assert by_union["on_bound"]["confirmedBy"] == "operator"
    assert by_union["on_legacy"]["source"] == "legacy-identities"


def test_identity_unbound_is_reported_through_ipc(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    with pytest.raises(DaemonRequestError) as unbound:
        app.handle(
            "identity.resolve", {"platform": "lark", "unionId": "on_missing"}
        )
    assert unbound.value.code == ipc_errors.IDENTITY_UNBOUND


def test_conflicting_sources_never_auto_pick(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path, owner_union_id="on_shared")
    users = _users(app)
    try:
        _member(users)
        guest = users.add_user(
            kind="guest", nickname="Guest", confirmed_by="operator"
        )
        users.bind_account(
            guest.user_key,
            adapter="lark:main",
            open_id="ou_guest",
            union_id="on_shared",
            confirmed_by="operator",
            source="local-override",
        )
    finally:
        users.close()

    with pytest.raises(DaemonRequestError) as conflict:
        app.handle(
            "identity.resolve", {"platform": "lark", "unionId": "on_shared"}
        )
    assert conflict.value.code == ipc_errors.IDENTITY_CONFLICT


def test_source_failure_is_not_fabricated_as_unbound(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _application(request, tmp_path)

    def unavailable(*_args: object, **_kwargs: object) -> tuple[()]:
        raise OSError("fixture source unavailable")

    monkeypatch.setattr(app._identity_resolver, "_legacy_rows", unavailable)
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "identity.resolve", {"platform": "lark", "unionId": "on_any"}
        )
    assert failure.value.code == ipc_errors.IDENTITY_SOURCE_UNAVAILABLE


def test_operator_writes_users_and_overrides_but_worker_cannot(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    added = app.handle(
        "identity.users.add",
        {
            "kind": "guest",
            "displayName": "Guest",
            "nickname": "Guest",
            "confirmedBy": "operator",
        },
    )
    user_key = added["user"]["userKey"]
    assert app.handle("identity.users.list", {"kind": "guest"}) == {
        "users": [added["user"]]
    }
    assert app.handle(
        "identity.users.list", {"kind": "guest", "user": user_key}
    ) == {"users": [added["user"]]}
    assert app.handle(
        "identity.users.list", {"user": "missing-user"}
    ) == {"users": []}
    row = app.handle(
        "identity.override.set",
        {
            "platform": "lark",
            "unionId": "on_guest",
            "user": user_key,
            "confirmedBy": "operator",
        },
    )
    assert row["source"] == "local-override"
    assert app.handle(
        "identity.override.clear",
        {"platform": "lark", "unionId": "on_guest"},
    ) == {"cleared": True}

    for method, params in (
        (
            "identity.users.add",
            {"kind": "guest", "displayName": "Other", "confirmedBy": "worker"},
        ),
        (
            "identity.override.set",
            {
                "platform": "lark",
                "unionId": "on_guest_2",
                "user": user_key,
                "confirmedBy": "worker",
            },
        ),
        (
            "identity.override.clear",
            {"platform": "lark", "unionId": "on_guest_2"},
        ),
    ):
        with pytest.raises(DaemonRequestError) as denied:
            app.handle(method, params, _trusted_message_origin="worker")
        assert denied.value.code == ipc_errors.IDENTITY_OVERRIDE_FORBIDDEN


def test_legacy_source_has_no_write_ipc(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    with pytest.raises(DaemonRequestError) as absent:
        app.handle("identity.legacy.set", {})
    assert absent.value.code == ipc_errors.METHOD_NOT_FOUND


@pytest.mark.parametrize(
    "relative",
    [
        "src/hyprial/daemon/impl/squire/addressing.py",
        "src/hyprial/daemon/impl/application/messaging/visibility.py",
        "src/hyprial/daemon/impl/application/messaging/status/views.py",
    ],
)
def test_daemon_readers_delegate_owner_account_resolution(relative: str) -> None:
    source = (REPO / relative).read_text(encoding="utf-8")
    assert "owner_open_id" not in source


def test_union_override_refuses_repoint_and_same_user_is_idempotent(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    users = _users(app)
    try:
        first = users.add_user(
            kind="guest", nickname="First", confirmed_by="operator"
        )
        second = users.add_user(
            kind="guest", nickname="Second", confirmed_by="operator"
        )
    finally:
        users.close()
    params = {
        "platform": "lark",
        "unionId": "on_fixed",
        "user": first.user_key,
        "confirmedBy": "operator",
    }
    original = app.handle("identity.override.set", params)
    assert app.handle("identity.override.set", params) == original

    with pytest.raises(DaemonRequestError) as conflict:
        app.handle("identity.override.set", {**params, "user": second.user_key})
    assert conflict.value.code == ipc_errors.IDENTITY_CONFLICT
    assert app.handle(
        "identity.resolve", {"platform": "lark", "unionId": "on_fixed"}
    )["user"] == first.user_key


def test_account_clear_undoes_the_account_set_and_keeps_the_legacy_account(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """§5 at cc2c7184: clear removes exactly what the matching set created.

    override.set {adapter, openId} on an account that already carries a
    unionId records an account-keyed override; clear {adapter, openId}
    removes exactly that, never the legacy account, and reports the legacy
    binding that still resolves the unionId.
    """
    app = _application(request, tmp_path)
    users = _users(app)
    try:
        user_key = _member(users)
        users.bind_account(
            user_key,
            adapter="lark:main",
            open_id="ou_existing",
            union_id="on_existing",
            confirmed_by="operator",
        )
    finally:
        users.close()
    app.handle(
        "identity.override.set",
        {
            "adapter": "main",
            "openId": "ou_existing",
            "user": user_key,
            "confirmedBy": "operator",
        },
    )
    assert app._identity_resolver._overrides.list(platform="lark") == ()
    assert app._identity_resolver._overrides.list_accounts()

    assert app.handle(
        "identity.override.clear", {"adapter": "main", "openId": "ou_existing"}
    ) == {
        "cleared": True,
        "unionBindingRemains": {"unionId": "on_existing", "source": "legacy-user-bind"},
    }
    assert app._identity_resolver._overrides.list_accounts() == ()
    users = _users(app)
    try:
        assert [(row.open_id, row.source) for row in users.accounts(user_key)] == [
            ("ou_existing", "manual:operator")
        ]
    finally:
        users.close()


def test_lark_adapter_never_mutates_identity_resolver_user_store() -> None:
    source = (
        REPO / "src/hyprial/daemon/impl/adapters/lark/inbound/adapter.py"
    ).read_text(encoding="utf-8")
    assert "resolver._users =" not in source


def test_identity_sqlite_readers_close_connections_and_escape_uri_paths() -> None:
    resolver = (
        REPO / "src/hyprial/daemon/impl/identity/resolver.py"
    ).read_text(encoding="utf-8")
    store = (
        REPO / "src/hyprial/daemon/impl/identity/store.py"
    ).read_text(encoding="utf-8")
    assert "quote(str(self._legacy_path), safe=\"/\")" in resolver
    assert store.count("self._connect()") == 6
    assert store.count("closing(self._connect())") == 6
