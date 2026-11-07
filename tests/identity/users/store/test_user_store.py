"""The per-machine user store and user homes (user home P1).

Allen, 2026-09-25 (Feishu DM): 「用户库每机一份，允许协调者绑定」
「访客也进同一张表标记为guest」.  So: one row per person, member or guest;
a coordinator may confirm a binding, and every confirmation names who made
it; a guest is never anyone's authorisation.

The properties pinned here are the ones a wrong binding would hide behind:
who may have an owner, how a key is made, that an account is never silently
moved between people, how a platform account resolves, and that every write
leaves an event naming its confirmer.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors
from hyprial.identity.impl.users.home import UserHomeError, ensure_user_home
from hyprial.identity.impl.users.store._base import (
    USER_ACCOUNT_CONFLICT,
    USER_EXISTS,
    USER_OWNER_FORBIDDEN,
    USER_OWNER_REQUIRED,
    Ambiguous,
    ResolvedUser,
    UserStoreError,
)
from hyprial.identity.impl.users.store._store import (
    UserStore,
    )

OWNER = "bob.smith@example.com"
HERE = "lark:cli-developer"
ROSTER = "lark:hq-adjutant"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def store(tmp_path: Path, home: Path):
    users = UserStore(tmp_path / "state" / "users.sqlite3", hyprial_home=home)
    try:
        yield users
    finally:
        users.close()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# -- (a) who may have an owner -------------------------------------------------


def test_a_member_requires_an_owner(store: UserStore) -> None:
    with pytest.raises(UserStoreError) as refused:
        store.add_user(kind="member", nickname="Allen", confirmed_by="allen")

    assert refused.value.code == USER_OWNER_REQUIRED
    assert store.list_users() == ()


def test_a_guest_forbids_an_owner(store: UserStore) -> None:
    """A guest with an owner would make a guest's message read as that
    owner's authorisation -- the one thing a guest must never carry."""

    with pytest.raises(UserStoreError) as refused:
        store.add_user(
            kind="guest", owner=OWNER, nickname="王蒴", confirmed_by="allen"
        )

    assert refused.value.code == USER_OWNER_FORBIDDEN
    assert store.list_users() == ()


def test_the_table_refuses_the_forbidden_shapes_even_past_the_code(
    store: UserStore,
) -> None:
    """The CHECK is the backstop for any future writer that skips add_user."""

    db = sqlite3.connect(store.path)
    try:
        for kind, owner in (("member", None), ("guest", OWNER)):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                db.execute(
                    "INSERT INTO users(user_key, kind, owner, entity_token,"
                    " created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, 1, 1)",
                    (f"k-{kind}", kind, owner, f"t-{kind}"),
                )
    finally:
        db.close()


def test_a_member_key_is_the_owner_slug(store: UserStore) -> None:
    from hyprial.identity.impl.identity_slug import identity_slug

    user = store.add_user(kind="member", owner=OWNER, confirmed_by="allen")

    assert user.user_key == identity_slug(OWNER) == "bob.smith-example.com"
    assert user.owner == OWNER


# -- (b) a guest key is not derived from a name ---------------------------------


def test_a_guest_key_is_random_never_name_derived(store: UserStore) -> None:
    """Names repeat and change; two guests with the same name are two people."""

    first = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    second = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")

    for user in (first, second):
        assert user.user_key.startswith("guest-")
        suffix = user.user_key.removeprefix("guest-")
        assert len(suffix) == 8
        int(suffix, 16)
        assert user.owner is None
    assert first.user_key != second.user_key


def test_the_same_name_on_two_machines_gets_two_keys(tmp_path: Path) -> None:
    """Any deterministic derivation from the name -- however it is encoded --
    gives the same key on two fresh stores; a random key does not."""

    keys = []
    for machine in ("a", "b"):
        users = UserStore(tmp_path / machine / "users.sqlite3")
        try:
            keys.append(
                users.add_user(kind="guest", nickname="王蒴", confirmed_by="allen").user_key
            )
        finally:
            users.close()

    assert keys[0] != keys[1]


# -- (c) a binding is never moved silently --------------------------------------


def test_binding_an_account_of_another_user_is_refused(store: UserStore) -> None:
    allen = store.add_user(kind="member", owner=OWNER, confirmed_by="allen")
    guest = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    store.bind_account(
        allen.user_key, adapter=HERE, open_id="ou_allen", confirmed_by="allen"
    )

    with pytest.raises(UserStoreError) as refused:
        store.bind_account(
            guest.user_key, adapter=HERE, open_id="ou_allen", confirmed_by="hq-adjutant"
        )

    assert refused.value.code == USER_ACCOUNT_CONFLICT
    (account,) = store.accounts(allen.user_key)
    assert account.open_id == "ou_allen"
    assert store.accounts(guest.user_key) == ()


def test_rebinding_to_the_same_user_is_idempotent(store: UserStore) -> None:
    allen = store.add_user(kind="member", owner=OWNER, confirmed_by="allen")
    first, created = store.bind_account(
        allen.user_key,
        adapter=HERE,
        open_id="ou_allen",
        union_id="on_allen",
        confirmed_by="allen",
        now_ms=1_000,
    )
    events_after_first = store.events(allen.user_key)

    again, changed = store.bind_account(
        allen.user_key,
        adapter=HERE,
        open_id="ou_allen",
        union_id="on_allen",
        confirmed_by="hq-adjutant",
        now_ms=2_000,
    )

    assert created is True
    assert changed is False
    assert again == first
    assert store.events(allen.user_key) == events_after_first


def test_a_refused_dm_channel_leaves_no_half_made_binding(store: UserStore) -> None:
    allen = store.add_user(kind="member", owner=OWNER, confirmed_by="allen")
    guest = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    store.add_channel(
        allen.user_key, adapter=HERE, chat_id="oc_dm", confirmed_by="allen"
    )

    with pytest.raises(UserStoreError) as refused:
        store.bind_account(
            guest.user_key,
            adapter=HERE,
            open_id="ou_guest",
            dm_chat_id="oc_dm",
            confirmed_by="allen",
        )

    assert refused.value.code == "USER_CHANNEL_CONFLICT"
    assert store.accounts(guest.user_key) == ()


# -- (d) resolution ------------------------------------------------------------


def test_resolution_by_exact_open_id(store: UserStore) -> None:
    allen = store.add_user(kind="member", owner=OWNER, confirmed_by="allen")
    store.bind_account(
        allen.user_key, adapter=HERE, open_id="ou_allen", confirmed_by="allen"
    )

    resolved = store.resolve_account(HERE, "ou_allen")

    assert isinstance(resolved, ResolvedUser)
    assert resolved.user.user_key == allen.user_key
    assert resolved.matched_by == "open_id"


def test_resolution_by_union_id_across_adapters(store: UserStore) -> None:
    """open_ids are per App: the person was confirmed under the roster App
    and is seen here under a different open_id with the same union_id."""

    allen = store.add_user(kind="member", owner=OWNER, confirmed_by="allen")
    store.bind_account(
        allen.user_key,
        adapter=ROSTER,
        open_id="ou_roster",
        union_id="on_allen",
        confirmed_by="hq-adjutant",
    )

    resolved = store.resolve_account(HERE, "ou_here", "on_allen")

    assert isinstance(resolved, ResolvedUser)
    assert resolved.user.user_key == allen.user_key
    assert resolved.matched_by == "union_id"
    assert resolved.account.adapter == ROSTER


def test_an_exact_open_id_wins_over_a_union_id_elsewhere(store: UserStore) -> None:
    allen = store.add_user(kind="member", owner=OWNER, confirmed_by="allen")
    guest = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    store.bind_account(
        guest.user_key, adapter=HERE, open_id="ou_x", confirmed_by="allen"
    )
    store.bind_account(
        allen.user_key,
        adapter=ROSTER,
        open_id="ou_r",
        union_id="on_x",
        confirmed_by="allen",
    )

    resolved = store.resolve_account(HERE, "ou_x", "on_x")

    assert isinstance(resolved, ResolvedUser)
    assert resolved.user.user_key == guest.user_key


def test_a_union_id_bound_to_two_people_is_ambiguous(store: UserStore) -> None:
    allen = store.add_user(kind="member", owner=OWNER, confirmed_by="allen")
    guest = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    for user, adapter, open_id in (
        (allen, ROSTER, "ou_a"),
        (guest, "lark:other", "ou_b"),
    ):
        store.bind_account(
            user.user_key,
            adapter=adapter,
            open_id=open_id,
            union_id="on_shared",
            confirmed_by="allen",
        )

    resolved = store.resolve_account(HERE, "ou_here", "on_shared")

    assert isinstance(resolved, Ambiguous)
    assert resolved.user_keys == tuple(sorted((allen.user_key, guest.user_key)))


def test_no_binding_resolves_to_none(store: UserStore) -> None:
    store.add_user(kind="member", owner=OWNER, confirmed_by="allen")

    assert store.resolve_account(HERE, "ou_nobody", "on_nobody") is None
    assert store.resolve_account(HERE, "ou_nobody") is None


# -- (f) every write leaves an event naming its confirmer -----------------------


def test_every_write_appends_one_event_whose_actor_is_the_confirmer(
    store: UserStore,
) -> None:
    guest = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    key = guest.user_key
    store.bind_account(
        key, adapter=HERE, open_id="ou_g", confirmed_by="hq-adjutant", now_ms=10
    )
    store.add_channel(
        key, adapter=HERE, chat_id="oc_dm", confirmed_by="squire", now_ms=20
    )
    store.update_user(key, display_name="王蒴 (guest)", confirmed_by="lin", now_ms=30)
    store.unbind_account(
        key, adapter=HERE, open_id="ou_g", confirmed_by="allen-2", now_ms=40
    )

    events = store.events(key)

    assert [(event.event, event.actor) for event in events] == [
        ("add", "allen"),
        ("bind", "hq-adjutant"),
        ("channel", "squire"),
        ("update", "lin"),
        ("unbind", "allen-2"),
    ]
    # The removed binding's confirmer survives only in the event.
    assert events[-1].detail["confirmedBy"] == "hq-adjutant"


@pytest.mark.parametrize("confirmed_by", ["", "   "])
def test_a_write_without_a_confirmer_is_refused(
    store: UserStore, confirmed_by: str
) -> None:
    with pytest.raises(UserStoreError) as refused:
        store.add_user(kind="guest", nickname="x", confirmed_by=confirmed_by)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert store.list_users() == ()


def test_the_event_log_refuses_rewrites(store: UserStore) -> None:
    guest = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    db = sqlite3.connect(store.path)
    try:
        for statement in (
            "UPDATE user_events SET actor = 'someone-else'",
            "DELETE FROM user_events",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement)
    finally:
        db.close()
    assert [event.actor for event in store.events(guest.user_key)] == ["allen"]


def test_the_schema_version_is_stamped(store: UserStore) -> None:
    db = sqlite3.connect(store.path)
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'schemaVersion'").fetchone()
    finally:
        db.close()
    assert row == ("1",)


# -- (g) user homes ------------------------------------------------------------


def test_add_creates_a_private_home_with_a_private_mirror(
    store: UserStore, home: Path
) -> None:
    import json

    allen = store.add_user(
        kind="member", owner=OWNER, real_name="林懿伦", confirmed_by="allen"
    )
    store.bind_account(
        allen.user_key,
        adapter=HERE,
        open_id="ou_allen",
        union_id="on_allen",
        confirmed_by="hq-adjutant",
    )

    root = home / "users" / allen.user_key
    assert _mode(home / "users") == 0o700
    assert _mode(root) == 0o700
    for name in ("config", "secrets", "state"):
        assert _mode(root / name) == 0o700
    profile = root / "profile.json"
    assert _mode(profile) == 0o600
    mirrored = json.loads(profile.read_text(encoding="utf-8"))
    assert mirrored["userKey"] == allen.user_key
    assert mirrored["kind"] == "member"
    assert mirrored["owner"] == OWNER
    assert mirrored["realName"] == "林懿伦"
    assert [account["openId"] for account in mirrored["accounts"]] == ["ou_allen"]
    assert mirrored["accounts"][0]["confirmedBy"] == "hq-adjutant"
    # No leftovers from the atomic replace.
    assert sorted(item.name for item in root.iterdir()) == [
        "config",
        "profile.json",
        "secrets",
        "state",
    ]


def test_a_symlinked_users_root_is_refused_and_nothing_is_recorded(
    tmp_path: Path, home: Path, store: UserStore
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (home / "users").symlink_to(elsewhere)

    with pytest.raises(UserHomeError) as refused:
        store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")

    assert refused.value.category == "unsafe-path"
    assert list(elsewhere.iterdir()) == []
    assert store.list_users() == ()


def test_a_symlinked_user_directory_is_refused(tmp_path: Path, home: Path) -> None:
    from hyprial.identity.impl.users.store._base import User

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (home / "users").mkdir(mode=0o700)
    (home / "users" / "guest-00000000").symlink_to(elsewhere)
    user = User("guest-00000000", "guest", None, "x", None, None, "t", 1, 1)

    with pytest.raises(UserHomeError) as refused:
        ensure_user_home(home, user)

    assert refused.value.category == "unsafe-path"
    assert list(elsewhere.iterdir()) == []


def test_a_loosened_home_directory_is_refused(home: Path, store: UserStore) -> None:
    guest = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    root = home / "users" / guest.user_key
    os.chmod(root / "secrets", 0o755)

    with pytest.raises(UserHomeError) as refused:
        store.update_user(guest.user_key, nickname="W", confirmed_by="allen")

    assert refused.value.category == "unsafe-mode"
    # The refused write rolled back with the mirror.
    assert store.get_user(guest.user_key).nickname == "王蒴"  # type: ignore[union-attr]


def test_a_traversal_key_is_refused_before_any_directory(home: Path) -> None:
    from hyprial.identity.impl.users.store._base import User

    user = User("..", "guest", None, "x", None, None, "t", 1, 1)

    with pytest.raises(UserHomeError) as refused:
        ensure_user_home(home, user)

    assert refused.value.category == "unsafe-key"
    assert not (home / "users").exists()


def test_two_owners_that_normalize_to_one_key_are_refused_not_merged(
    store: UserStore,
) -> None:
    """The member key is the owner slug, and the slug lowercases: a second
    owner spelled differently must not silently become the first person."""

    store.add_user(kind="member", owner="a@b.com", confirmed_by="allen")

    with pytest.raises(UserStoreError) as refused:
        store.add_user(kind="member", owner="A@B.com", confirmed_by="allen")

    assert refused.value.code == USER_EXISTS
    (only,) = store.list_users()
    assert only.owner == "a@b.com"


@pytest.mark.parametrize(
    "owner",
    [
        "u.ou_" + "b" * 32,
        "u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9",
        "d." + "b" * 32,
        "a." + "a" * 26,
        # Case-insensitive reservation (APFS): uppercase variants are refused too.
        "U.OU_" + "B" * 32,
        "D." + "B" * 32,
        "A." + "A" * 26,
    ],
)
def test_add_member_rejects_exact_identity_id_shapes(
    store: UserStore, owner: str
) -> None:
    with pytest.raises(UserStoreError) as refused:
        store.add_user(kind="member", owner=owner, confirmed_by="allen")
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert store.list_users() == ()


@pytest.mark.parametrize("owner", ["d.lane@example.com", "a.smith@x", "u.zhang"])
def test_add_member_accepts_id_prefix_lookalikes(store: UserStore, owner: str) -> None:
    assert store.add_user(kind="member", owner=owner, confirmed_by="allen").owner == owner
