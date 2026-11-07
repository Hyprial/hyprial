"""Review 1020: profile_open_id may raise; its three callers must not.

A conflicted (or unreadable) owner binding makes ``profile_open_id`` raise
IdentityResolverError.  Owner delivery refuses with a typed code before any
send; the after-delivery reply-path check and the targets listing log and
carry on, so a delivered message never comes back as an IPC error and one
conflicted owner never empties the whole listing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.identity import IdentityResolverError
from hyprial.daemon.impl.squire.addressing import (
    ReceiverUserDelivery,
    UserAdapterRegistry,
    UserDeliveryLedger,
    UserDeliveryRequest,
)
from hyprial.identity.impl.users.profile import UserProfileStore
from hyprial.kernel import ipc_errors
from tests.daemon.identity.test_identity_resolver_r1b import (
    OWNER,
    USER_KEY,
    _application,
    _OwnerAdapter,
)

NODE = "identity-r1b-node"
ADAPTER_URI = f"channel:{OWNER}:{NODE}:main"


class _ConflictedResolver:
    def profile_open_id(self, _profile: Any, _adapter: str) -> str | None:
        raise IdentityResolverError(
            ipc_errors.IDENTITY_CONFLICT, "owner account is bound to two users"
        )


def _with_squire_profile(profiles: UserProfileStore) -> None:
    profiles.ensure(
        owner=OWNER,
        owner_key=USER_KEY,
        login_name="allen",
        machine=NODE,
        machine_key="node-key",
    )
    profiles.set_squire_channel(USER_KEY, ADAPTER_URI)


def _recorded_log(app: DaemonApplication, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    events: list[tuple] = []
    monkeypatch.setattr(
        app, "_log", lambda level, component, event, **fields: events.append((level, event, fields))
    )
    return events


def test_owner_delivery_refuses_with_a_typed_code_on_identity_conflict(
    tmp_path: Path,
) -> None:
    profiles = UserProfileStore(tmp_path / "profiles.json")
    _with_squire_profile(profiles)
    owner_adapter = _OwnerAdapter()
    adapters = UserAdapterRegistry()
    adapters.register(ADAPTER_URI, owner_adapter)
    receiver = ReceiverUserDelivery(
        node_id=NODE,
        profiles=profiles,
        adapters=adapters,
        ledger=UserDeliveryLedger(tmp_path / "delivery.json"),
        identity_resolver=_ConflictedResolver(),  # type: ignore[arg-type]
    )

    result = receiver.handle(
        UserDeliveryRequest(
            message_id="msg-r1d",
            idempotency_key="r1b-owner-dm",
            owner=OWNER,
            sender="agent:alice:node:worker",
            message="hello",
            conversation_id="r1d",
        )
    )

    assert result.accepted is False
    assert result.code == ipc_errors.IDENTITY_CONFLICT
    assert result.definitely_not_sent is True
    assert owner_adapter.open_ids == []


def test_reply_path_check_after_delivery_survives_an_identity_conflict(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _application(request, tmp_path)
    _with_squire_profile(app.user_profiles)
    app._identity_resolver = _ConflictedResolver()  # type: ignore[assignment]
    events = _recorded_log(app, monkeypatch)

    assert app._reply_path_unavailable(f"user:{OWNER}") is False
    assert ("warn", "send.reply_path_identity_unresolved") in {
        (level, event) for level, event, _fields in events
    }


def test_targets_listing_survives_one_conflicted_owner(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _application(request, tmp_path)
    _with_squire_profile(app.user_profiles)
    app._identity_resolver = _ConflictedResolver()  # type: ignore[assignment]
    events = _recorded_log(app, monkeypatch)

    listed = app.handle("targets", {})

    assert all(row["targetKind"] != "user" for row in listed["targets"])
    assert ("warn", "targets.owner_account_unresolved") in {
        (level, event) for level, event, _fields in events
    }


# -- accounts[].source: how we know an adapter's openId (§5, org-improver) ----


def _account_sources(app: DaemonApplication, union_id: str) -> dict[str, str]:
    row = app.handle("identity.resolve", {"platform": "lark", "unionId": union_id})
    return {account["openId"]: account["source"] for account in row["accounts"]}


def test_accounts_report_observed_or_override_by_how_they_are_known(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.adapters.lark import LarkStateStore
    from hyprial.identity import UserStore

    app = _application(request, tmp_path)
    users = UserStore(app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home)
    try:
        # A legacy user-bind account (human-asserted) on another adapter.
        users.bind_account(
            USER_KEY,
            adapter="lark:other",
            open_id="ou_bound",
            union_id="on_allen",
            confirmed_by="operator",
        )
        # Asserted AND later seen inbound: the observation wins.
        users.bind_account(
            USER_KEY,
            adapter="lark:main",
            open_id="ou_both",
            union_id="on_allen",
            confirmed_by="operator",
        )
    finally:
        users.close()
    state = LarkStateStore(app.state_dir / "adapters.sqlite3", adapter="lark:main")
    try:
        for open_id in ("ou_seen", "ou_both"):
            state.observe_identity(
                "user", open_id, union_id="on_allen",
                source="event-sender:oc_dm", now_ms=101,
            )
    finally:
        state.close()

    assert _account_sources(app, "on_allen") == {
        "ou_bound": "override",
        "ou_both": "observed",
        "ou_seen": "observed",
    }


def test_an_override_set_account_reports_override(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)

    row = app.handle(
        "identity.override.set",
        {"adapter": "main", "openId": "ou_owner", "user": USER_KEY, "confirmedBy": "operator"},
    )

    assert [account["source"] for account in row["accounts"]] == ["override"]


# -- users.add: store-generated keys (§5 @ 4ff498d4) --------------------------


def test_users_add_generates_member_and_guest_keys(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)

    member = app.handle(
        "identity.users.add",
        {"kind": "member", "owner": "bob@example.com", "confirmedBy": "operator"},
    )["user"]
    first = app.handle(
        "identity.users.add",
        {"kind": "guest", "displayName": "Visitor", "confirmedBy": "operator"},
    )["user"]
    second = app.handle(
        "identity.users.add",
        {"kind": "guest", "displayName": "Visitor", "confirmedBy": "operator"},
    )["user"]

    assert member["userKey"] == "bob-example.com"
    assert first["displayName"] == "Visitor"
    # Random, so the same display name never collides into one person.
    assert first["userKey"] != second["userKey"]


def test_users_add_refuses_a_caller_supplied_key(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    from hyprial.kernel import DaemonRequestError

    app = _application(request, tmp_path)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "identity.users.add",
            {
                "user": "chosen-key",
                "kind": "member",
                "owner": "bob@example.com",
                "confirmedBy": "operator",
            },
        )

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    listed = app.handle("identity.users.list", {"user": "bob-example.com"})
    assert listed == {"users": []}


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "member", "owner": "carol@example.com"},
        {"kind": "member", "owner": "dave@example.com", "displayName": "Dave",
         "nickname": "D", "realName": "Dave Real"},
        {"kind": "guest", "displayName": "Guest", "nickname": "G", "realName": "G Real"},
    ],
)
def test_users_add_accepts_each_allowed_field_combination(
    request: pytest.FixtureRequest, tmp_path: Path, params: dict[str, str]
) -> None:
    app = _application(request, tmp_path)

    record = app.handle(
        "identity.users.add", {**params, "confirmedBy": "operator"}
    )["user"]

    assert record["kind"] == params["kind"]


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "member", "owner": "erin@example.com", "unknownField": "x"},
        {"kind": "guest"},
    ],
    ids=["unknown-field", "guest-without-displayName"],
)
def test_users_add_refuses_unknown_fields_and_a_nameless_guest(
    request: pytest.FixtureRequest, tmp_path: Path, params: dict[str, str]
) -> None:
    from hyprial.kernel import DaemonRequestError

    app = _application(request, tmp_path)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle("identity.users.add", {**params, "confirmedBy": "operator"})

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT


# -- review 1023: an unreadable override store is "unavailable", everywhere ----


def _app_with_broken_override_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> DaemonApplication:
    app = _application(request, tmp_path)
    _with_squire_profile(app.user_profiles)
    # A locked-past-timeout or corrupt identity.sqlite3 reads like this.
    (app.state_dir / "identity.sqlite3").write_bytes(b"not a sqlite database")
    return app


def test_owner_delivery_refuses_typed_when_the_override_store_is_unreadable(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _app_with_broken_override_store(request, tmp_path)
    owner_adapter = _OwnerAdapter()
    adapters = UserAdapterRegistry()
    adapters.register(ADAPTER_URI, owner_adapter)
    receiver = ReceiverUserDelivery(
        node_id=NODE,
        profiles=app.user_profiles,
        adapters=adapters,
        ledger=UserDeliveryLedger(tmp_path / "delivery.json"),
        identity_resolver=app._identity_resolver,
    )

    result = receiver.handle(
        UserDeliveryRequest(
            message_id="msg-r1d-store",
            idempotency_key="r1b-owner-dm",
            owner=OWNER,
            sender="agent:alice:node:worker",
            message="hello",
            conversation_id="r1d",
        )
    )

    assert result.accepted is False
    assert result.code == ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
    assert owner_adapter.open_ids == []


def test_reply_path_check_survives_an_unreadable_override_store(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app_with_broken_override_store(request, tmp_path)
    events = _recorded_log(app, monkeypatch)

    assert app._reply_path_unavailable(f"user:{OWNER}") is False
    assert ("warn", "send.reply_path_identity_unresolved") in {
        (level, event) for level, event, _fields in events
    }


def test_targets_listing_survives_an_unreadable_override_store(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app_with_broken_override_store(request, tmp_path)
    events = _recorded_log(app, monkeypatch)

    listed = app.handle("targets", {})

    assert all(row["targetKind"] != "user" for row in listed["targets"])
    assert ("warn", "targets.owner_account_unresolved") in {
        (level, event) for level, event, _fields in events
    }


def test_bindings_list_and_owner_dm_survive_mixed_union_and_account_only_rows(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A union-keyed override and an account-only override coexist.

    bindings() sorted on a key holding unionId, which is None for the
    account-only row: TypeError ('<' between NoneType and str), surfaced raw
    by bindings.list and by profile_open_id (the owner DM outbound path).
    """

    app = _application(request, tmp_path)
    guest = app.handle(
        "identity.users.add",
        {"kind": "guest", "displayName": "Guest", "confirmedBy": "operator"},
    )["user"]["userKey"]
    app.handle(
        "identity.override.set",
        {"platform": "lark", "unionId": "on_x", "user": guest, "confirmedBy": "operator"},
    )
    app.handle(
        "identity.override.set",
        {"adapter": "outb", "openId": "ou_outb", "user": USER_KEY, "confirmedBy": "operator"},
    )

    _with_squire_profile(app.user_profiles)
    profile = app.user_profiles.resolve(OWNER)
    # Another adapter than the account-only one, so the lookup reaches bindings().
    assert app._identity_resolver.profile_open_id(profile, ADAPTER_URI) is None
    rows = app.handle("identity.bindings.list", {})["bindings"]
    assert {(row["unionId"], row.get("outboundOnly", False)) for row in rows} >= {
        ("on_x", False),
        (None, True),
    }
