"""The per-machine user store and user homes (user home P1).

Allen, 2026-09-25 (Feishu DM): 「用户库每机一份」「访客也进同一张表标记为guest」.
So: one row per person, member or guest; every write names who confirmed it;
a guest is never anyone's authorisation.  Since R3 of the user-to-IM binding
removal the store keeps names only: no platform accounts and no chats.

The properties pinned here: who may have an owner, how a key is made, that the
store creates no account or channel table, and that every write leaves an event
naming its confirmer.
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
    USER_EXISTS,
    USER_OWNER_FORBIDDEN,
    USER_OWNER_REQUIRED,
    UserStoreError,
)
from hyprial.identity.impl.users.store._store import (
    UserStore,
    )

OWNER = "bob.smith@example.com"


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


def test_a_new_store_has_no_account_or_channel_table(store: UserStore) -> None:
    db = sqlite3.connect(store.path)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        db.close()
    assert "user_accounts" not in tables
    assert "user_channels" not in tables
    assert {"meta", "users", "user_events"} <= tables


# -- (f) every write leaves an event naming its confirmer -----------------------


def test_every_write_appends_one_event_whose_actor_is_the_confirmer(
    store: UserStore,
) -> None:
    guest = store.add_user(kind="guest", nickname="王蒴", confirmed_by="allen")
    key = guest.user_key
    store.update_user(key, display_name="王蒴 (guest)", confirmed_by="lin", now_ms=30)

    events = store.events(key)

    assert [(event.event, event.actor) for event in events] == [
        ("add", "allen"),
        ("update", "lin"),
    ]


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
    assert "accounts" not in mirrored
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


def test_ensure_user_home_writes_profile_without_o_nofollow(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from hyprial.identity.impl.users.store._base import User

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    first = User("guest-a1b2c3d4", "guest", None, "初值", None, None, "t", 1, 1000)
    updated = User("guest-a1b2c3d4", "guest", None, "更新", None, None, "t", 1, 2000)

    root = ensure_user_home(home, first)
    ensure_user_home(home, updated)

    profile = root / "profile.json"
    assert _mode(profile) == 0o600
    mirrored = json.loads(profile.read_text(encoding="utf-8"))
    assert mirrored["userKey"] == "guest-a1b2c3d4"
    assert mirrored["nickname"] == "更新"
    assert sorted(item.name for item in root.iterdir()) == [
        "config",
        "profile.json",
        "secrets",
        "state",
    ]
