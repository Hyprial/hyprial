import threading

import pytest

from hyprial.identity import UserProfileError, UserProfileStore


def test_separate_store_writers_do_not_overwrite_each_others_fields(tmp_path):
    path = tmp_path / "users.json"
    first = UserProfileStore(path)
    first.ensure(
        owner="alice",
        owner_key="alice-key",
        login_name="alice",
        machine="node",
        machine_key="node-key",
    )
    second = UserProfileStore(path)
    entered, release, second_started, second_saved = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    save_first, save_second = first._save, second._save

    def blocked(profiles):
        entered.set()
        assert release.wait(3)
        save_first(profiles)

    first._save = blocked

    def observe(profiles):
        second_saved.set()
        save_second(profiles)

    second._save = observe
    errors = []

    def first_writer():
        try:
            first.set_delivery_agent("alice-key", "worker")
        except BaseException as error:
            errors.append(error)

    def second_writer():
        second_started.set()
        try:
            second.set_squire_channel("alice-key", "channel:alice:node:bot")
        except BaseException as error:
            errors.append(error)

    one = threading.Thread(target=first_writer)
    two = threading.Thread(target=second_writer)
    try:
        one.start()
        assert entered.wait(1)
        two.start()
        assert second_started.wait(1)
        assert not second_saved.wait(0.05)
    finally:
        release.set()
        one.join(2)
        two.join(2)
    assert not errors
    result = UserProfileStore(path).get("alice-key")
    assert result.delivery_agent == "worker"
    assert result.squire_channel == "channel:alice:node:bot"


@pytest.mark.parametrize(
    "reserved_id",
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
@pytest.mark.parametrize(
    "field", ["owner", "owner_key", "login_name", "machine", "machine_key"]
)
def test_profile_identity_names_reject_exact_id_shapes(
    tmp_path, reserved_id: str, field: str
) -> None:
    values = {
        "owner": "alice",
        "owner_key": "alice-key",
        "login_name": "alice",
        "machine": "node",
        "machine_key": "node-key",
    }
    values[field] = reserved_id
    with pytest.raises(UserProfileError, match="identity id shape"):
        UserProfileStore(tmp_path / "users.json").ensure(**values)


def test_profile_identity_names_accept_id_prefix_lookalikes(tmp_path) -> None:
    profile, _ = UserProfileStore(tmp_path / "users.json").ensure(
        owner="d.lane@example.com",
        owner_key="a.smith@x",
        login_name="u.zhang",
        machine="d.workstation",
        machine_key="a.machine",
    )
    assert profile.owner == "d.lane@example.com"
