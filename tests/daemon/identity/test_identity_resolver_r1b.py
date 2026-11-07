"""R1b account-only overrides and the frozen bare-adapter boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark import Identity, LarkStateStore
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.identity import IdentityResolverError
from hyprial.daemon.impl.squire.addressing import (
    ReceiverUserDelivery,
    UserAdapterRegistry,
    UserDeliveryLedger,
    UserDeliveryRequest,
)
from hyprial.identity import UserStore
from hyprial.identity.impl.users.profile import UserProfileStore
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.runtime._owned_applications import close_application


OWNER = "aaron@example.com"
USER_KEY = "aaron-example.com"


class _Inbox:
    pass


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


class _OwnerAdapter:
    def __init__(self) -> None:
        self.open_ids: list[str] = []

    def send_owner_dm(
        self, open_id: str, _text: str, *, idempotency_key: str
    ) -> str:
        assert idempotency_key == "r1b-owner-dm"
        self.open_ids.append(open_id)
        return "om-r1b"


def _application(
    request: pytest.FixtureRequest, tmp_path: Path
) -> DaemonApplication:
    home = tmp_path / "home"
    state = home / "state"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(
        json.dumps(
            {
                "owner": OWNER,
                "identityMode": "casdoor",
                "identityClaims": {
                    "properties": {"oauth_Lark_unionId": "on_allen"}
                },
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="identity-r1b-node",
        hyprial_home=home,
        owner=OWNER,
        **daemon_dependencies(),
    )
    request.addfinalizer(lambda: close_application(app))
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    users = UserStore(state / "users.sqlite3", hyprial_home=home)
    try:
        assert (
            users.add_user(
                kind="member", owner=OWNER, confirmed_by="operator"
            ).user_key
            == USER_KEY
        )
    finally:
        users.close()
    return app


def _account_override(app: DaemonApplication) -> dict[str, object]:
    return app.handle(
        "identity.override.set",
        {
            "adapter": "main",
            "openId": "ou_owner",
            "user": USER_KEY,
            "confirmedBy": "operator",
        },
    )


def test_zero_inbound_account_override_delivers_owner_dm_and_clear_removes_it(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)

    row = _account_override(app)
    assert row["user"] == USER_KEY
    assert row["source"] == "local-override"
    assert row["unionId"] is None
    assert row["outboundOnly"] is True
    assert [
        {key: account[key] for key in ("adapter", "openId")}
        for account in row["accounts"]  # type: ignore[union-attr]
    ] == [{"adapter": "main", "openId": "ou_owner"}]

    users = UserStore(
        app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home
    )
    try:
        (account,) = users.accounts(USER_KEY)
        assert account.adapter == "lark:main"
        assert account.union_id is None
        assert account.source == "override"
    finally:
        users.close()

    profiles = UserProfileStore(tmp_path / "profiles.json")
    profiles.ensure(
        owner=OWNER,
        owner_key=USER_KEY,
        login_name="allen",
        machine="identity-r1b-node",
        machine_key="node-key",
    )
    adapter_uri = "channel:aaron@example.com:identity-r1b-node:main"
    profiles.set_squire_channel(USER_KEY, adapter_uri)
    owner_adapter = _OwnerAdapter()
    adapters = UserAdapterRegistry()
    adapters.register(adapter_uri, owner_adapter)
    receiver = ReceiverUserDelivery(
        node_id="identity-r1b-node",
        profiles=profiles,
        adapters=adapters,
        ledger=UserDeliveryLedger(tmp_path / "delivery.json"),
        identity_resolver=app._identity_resolver,
    )

    delivered = receiver.handle(
        UserDeliveryRequest(
            message_id="msg-r1b",
            idempotency_key="r1b-owner-dm",
            owner=OWNER,
            sender="agent:alice:node:worker",
            message="hello",
            conversation_id="r1b",
        )
    )
    assert delivered.accepted is True
    assert owner_adapter.open_ids == ["ou_owner"]

    assert app.handle(
        "identity.override.clear",
        {"adapter": "main", "openId": "ou_owner"},
    ) == {"cleared": True}
    assert app._identity_resolver.profile_open_id(
        profiles.resolve(OWNER), adapter_uri
    ) is None


def test_later_observation_for_another_user_is_a_conflict(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _account_override(app)
    users = UserStore(
        app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home
    )
    try:
        guest = users.add_user(
            kind="guest", nickname="Guest", confirmed_by="operator"
        )
        users.bind_account(
            guest.user_key,
            adapter="lark:other",
            open_id="ou_guest",
            union_id="on_guest",
            confirmed_by="operator",
            source="local-override",
        )
    finally:
        users.close()
    observed = LarkStateStore(
        app.state_dir / "adapters.sqlite3", adapter="lark:main"
    )
    try:
        observed.observe_identity(
            "user",
            "ou_owner",
            union_id="on_guest",
            source="event-sender:oc_dm",
        )
    finally:
        observed.close()

    with pytest.raises(DaemonRequestError) as conflict:
        app.handle(
            "identity.resolve", {"adapter": "main", "openId": "ou_owner"}
        )
    assert conflict.value.code == ipc_errors.IDENTITY_CONFLICT


def test_clear_removes_only_override_account_not_observed_identity(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _account_override(app)
    observed = LarkStateStore(
        app.state_dir / "adapters.sqlite3", adapter="lark:main"
    )
    try:
        observed.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_owner",
                union_id="on_allen",
                hyprial_owner=OWNER,
                standing="verified",
                source="manual:pre-r1",
            )
        )
        assert app.handle(
            "identity.override.clear",
            {"adapter": "main", "openId": "ou_owner"},
        ) == {"cleared": True}
        assert observed.find_identities(platform_id="ou_owner")
    finally:
        observed.close()


@pytest.mark.parametrize("adapter", ["lark:main", "channel:lark:main"])
def test_namespaced_adapter_is_refused_at_frozen_ipc_boundary(
    request: pytest.FixtureRequest, tmp_path: Path, adapter: str
) -> None:
    app = _application(request, tmp_path)
    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "identity.override.set",
            {
                "adapter": adapter,
                "openId": "ou_owner",
                "user": USER_KEY,
                "confirmedBy": "operator",
            },
        )
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT


def test_account_only_override_is_listed_and_resolves_outbound_only(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _account_override(app)

    listed = app.handle("identity.bindings.list", {})
    assert set(listed) == {"bindings"}
    binding = next(
        row for row in listed["bindings"] if row["unionId"] is None
    )
    assert {
        key: binding[key]
        for key in (
            "user",
            "kind",
            "platform",
            "unionId",
            "source",
            "confirmedBy",
            "outboundOnly",
        )
    } == {
        "user": USER_KEY,
        "kind": "member",
        "platform": "lark",
        "unionId": None,
        "source": "local-override",
        "confirmedBy": "operator",
        "outboundOnly": True,
    }
    assert binding["accounts"][0]["adapter"] == "main"
    assert binding["accounts"][0]["openId"] == "ou_owner"
    resolved = app.handle(
        "identity.resolve", {"adapter": "main", "openId": "ou_owner"}
    )
    assert resolved["user"] == USER_KEY
    assert resolved["kind"] == "member"
    assert resolved["source"] == "local-override"
    assert resolved["verified"] is False
    assert resolved["outboundOnly"] is True
    assert resolved["accounts"][0]["openId"] == "ou_owner"


def test_profile_open_id_matches_bare_account_adapter(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    users = UserStore(
        app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home
    )
    try:
        users.bind_account(
            USER_KEY,
            adapter="lark:main",
            open_id="ou_observed",
            union_id="on_allen",
            confirmed_by="operator",
        )
    finally:
        users.close()
    profile, _events = UserProfileStore(tmp_path / "profiles-bare.json").ensure(
        owner=OWNER,
        owner_key=USER_KEY,
        login_name="allen",
        machine="identity-r1b-node",
        machine_key="node-key",
    )

    assert app._identity_resolver.profile_open_id(
        profile, "channel:aaron@example.com:identity-r1b-node:main"
    ) == "ou_observed"


def test_profile_open_id_surfaces_identity_conflict(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    users = UserStore(
        app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home
    )
    try:
        guest = users.add_user(
            kind="guest", nickname="Guest", confirmed_by="operator"
        )
    finally:
        users.close()
    app.handle(
        "identity.override.set",
        {
            "platform": "lark",
            "unionId": "on_allen",
            "user": guest.user_key,
            "confirmedBy": "operator",
        },
    )
    observed = LarkStateStore(
        app.state_dir / "adapters.sqlite3", adapter="lark:main"
    )
    try:
        observed.observe_identity(
            "user",
            "ou_conflict",
            union_id="on_allen",
            source="event-sender:oc_dm",
        )
    finally:
        observed.close()
    profile, _events = UserProfileStore(tmp_path / "profiles-conflict.json").ensure(
        owner=OWNER,
        owner_key=USER_KEY,
        login_name="allen",
        machine="identity-r1b-node",
        machine_key="node-key",
    )

    with pytest.raises(IdentityResolverError) as conflict:
        app._identity_resolver.profile_open_id(
            profile, "channel:aaron@example.com:identity-r1b-node:main"
        )
    assert conflict.value.code == ipc_errors.IDENTITY_CONFLICT



def _receiver_for_profile(
    app: DaemonApplication,
    tmp_path: Path,
    *,
    profile_open_id: str | None = None,
) -> tuple[ReceiverUserDelivery, _OwnerAdapter, str]:
    profiles = UserProfileStore(tmp_path / "receiver-profiles.json")
    profiles.ensure(
        owner=OWNER,
        owner_key=USER_KEY,
        login_name="allen",
        machine="identity-r1b-node",
        machine_key="node-key",
    )
    adapter_uri = "channel:aaron@example.com:identity-r1b-node:main"
    profiles.set_squire_channel(USER_KEY, adapter_uri)
    if profile_open_id is not None:
        profiles.bind_owner_open_id(
            USER_KEY, channel=adapter_uri, open_id=profile_open_id
        )
    owner_adapter = _OwnerAdapter()
    adapters = UserAdapterRegistry()
    adapters.register(adapter_uri, owner_adapter)
    return (
        ReceiverUserDelivery(
            node_id="identity-r1b-node",
            profiles=profiles,
            adapters=adapters,
            ledger=UserDeliveryLedger(tmp_path / "receiver-deliveries.json"),
            identity_resolver=app._identity_resolver,
        ),
        owner_adapter,
        adapter_uri,
    )


def test_duplicate_overrides_deliver_through_squire_using_base_first_match(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    users = UserStore(
        app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home
    )
    try:
        for open_id in ("ou_a_first", "ou_z_second"):
            users.bind_account(
                USER_KEY,
                adapter="lark:main",
                open_id=open_id,
                confirmed_by="operator",
                source="override",
            )
    finally:
        users.close()
    receiver, owner_adapter, _adapter_uri = _receiver_for_profile(app, tmp_path)

    result = receiver.handle(
        UserDeliveryRequest(
            message_id="msg-duplicate-overrides",
            idempotency_key="r1b-owner-dm",
            owner=OWNER,
            sender="agent:alice:node:worker",
            message="hello",
            conversation_id="r1b-duplicate-overrides",
        )
    )

    assert result.accepted is True
    assert owner_adapter.open_ids == ["ou_a_first"]


def test_stale_legacy_rows_do_not_precede_profile_binding_on_receiver_path(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    receiver, owner_adapter, _adapter_uri = _receiver_for_profile(
        app, tmp_path, profile_open_id="ou_profile_binding"
    )
    observed = LarkStateStore(
        app.state_dir / "adapters.sqlite3", adapter="lark:main"
    )
    try:
        observed.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_stale_legacy",
                union_id=None,
                hyprial_owner=OWNER,
                standing="verified",
                source="manual:stale",
            )
        )
    finally:
        observed.close()

    result = receiver.handle(
        UserDeliveryRequest(
            message_id="msg-stale-legacy",
            idempotency_key="r1b-owner-dm",
            owner=OWNER,
            sender="agent:alice:node:worker",
            message="hello",
            conversation_id="r1b-stale-legacy",
        )
    )

    assert result.accepted is True
    assert owner_adapter.open_ids == ["ou_profile_binding"]


def test_owner_open_id_ignores_unverified_legacy_rows(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    observed = LarkStateStore(
        app.state_dir / "adapters.sqlite3", adapter="lark:main"
    )
    try:
        observed.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_unverified",
                union_id="on_unverified",
                hyprial_owner=OWNER,
                standing="observed",
                source="event-sender:oc_dm",
            )
        )
    finally:
        observed.close()

    assert app._identity_resolver.owner_open_id(OWNER, "main") is None
