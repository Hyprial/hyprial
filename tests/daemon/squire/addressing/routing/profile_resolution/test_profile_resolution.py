from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity.impl.users.profile import UserProfileError, UserProfileStore
from hyprial.daemon.impl.squire.addressing import UserDeliveryTarget, is_user_target
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from ..support import (
    _TEST_APPLICATIONS,
    _close_test_applications as _close_test_applications,
    _cross_vocabulary_store,
)


def test_user_target_parser_accepts_first_class_owner_address() -> None:
    target = UserDeliveryTarget.parse("user:gregpaws")

    assert target.owner == "gregpaws"
    assert target.uri == "user:gregpaws"
    assert is_user_target("user:gregpaws") is True
    assert is_user_target("agent:gregpaws:mac:worker") is False
    with pytest.raises(ValueError, match="non-empty owner"):
        UserDeliveryTarget.parse("user:")


def test_owner_requester_addresses_include_proxy_agent_adapter_and_route(
    tmp_path: Path,
) -> None:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="receiver-mac",
        owner="bob",
    **_deep_module_daemon_dependencies())
    _TEST_APPLICATIONS.append(app)
    app.user_profiles.ensure(
        owner="bob",
        owner_key="owner-bob",
        login_name="bob",
        machine="receiver-mac",
        machine_key="receiver-machine-key",
    )
    app.user_profiles.set_delivery_agent("owner-bob", "bob-proxy")
    agent = app.agents.create("bob-proxy")
    app.agents.pin("bob-channel", agent.actor)
    app.load_persistent_configuration = lambda: SimpleNamespace(
        channels=SimpleNamespace(
            gateways=(
                SimpleNamespace(
                    name="bob-channel",
                    routes=(SimpleNamespace(name="owner"),),
                ),
            )
        )
    )

    addresses = app._owner_requester_addresses()

    assert agent.uri in addresses
    assert "adapter:lark:bob-channel" in addresses
    assert "route:bob-channel:owner" in addresses


def test_resolve_finds_the_profile_by_owner_key(tmp_path: Path) -> None:
    """The failing direction: the daemon addresses by owner key."""

    profiles = _cross_vocabulary_store(tmp_path)

    profile = profiles.resolve("bobadmin")

    assert profile is not None
    assert profile.owner == "alicesmith"


def test_resolve_still_finds_the_profile_by_owner(tmp_path: Path) -> None:
    """The direction that already worked must keep working."""

    profiles = _cross_vocabulary_store(tmp_path)

    profile = profiles.resolve("alicesmith")

    assert profile is not None
    assert profile.owner_key == "bobadmin"


def test_resolve_returns_none_only_when_neither_vocabulary_matches(
    tmp_path: Path,
) -> None:
    """Without this case the two above are satisfied by a function that
    always returns the sole profile, which would test nothing."""

    profiles = _cross_vocabulary_store(tmp_path)

    assert profiles.resolve("nobody") is None


def test_owner_key_wins_when_a_value_is_both(tmp_path: Path) -> None:
    """Owner and owner key share a value space, so collisions are possible.

    Pinned deliberately: the owner key is the globally unique anchor, so it
    wins. This is a decision, not an accident -- if it is ever reversed, this
    test should be the thing that argues about it.
    """

    profiles = UserProfileStore(tmp_path / "home" / "state" / "users.json")
    profiles.ensure(
        owner="carol",
        owner_key="dave",
        login_name="dave",
        machine="box",
        machine_key="box-key",
    )
    profiles.ensure(
        owner="dave",
        owner_key="dave-key",
        login_name="dave2",
        machine="box2",
        machine_key="box2-key",
    )

    resolved = profiles.resolve("dave")

    assert resolved is not None
    assert resolved.owner == "carol"
    assert resolved.owner_key == "dave"


def test_resolve_keeps_the_ambiguous_owner_guard(tmp_path: Path) -> None:
    """The fallback must not become a silent first-match.

    `get_by_owner` raises on duplicate owners; `resolve` reaches it only when
    the owner-key lookup missed, and that guard has to survive the new path.
    """

    profiles = UserProfileStore(tmp_path / "home" / "state" / "users.json")
    profiles.ensure(
        owner="same",
        owner_key="key-one",
        login_name="one",
        machine="box",
        machine_key="box-key",
    )
    profiles.ensure(
        owner="same",
        owner_key="key-two",
        login_name="two",
        machine="box2",
        machine_key="box2-key",
    )

    with pytest.raises(UserProfileError, match="ambiguous"):
        profiles.resolve("same")


def test_profile_error_safe_detail_masks_an_ambiguous_owner(tmp_path: Path) -> None:
    owner = "owner.person@example.test"
    profiles = UserProfileStore(tmp_path / "users.json")
    profiles.ensure(
        owner=owner,
        owner_key="key-one",
        login_name="one",
        machine="box",
        machine_key="box-key",
    )
    profiles.ensure(
        owner=owner,
        owner_key="key-two",
        login_name="two",
        machine="box2",
        machine_key="box2-key",
    )

    with pytest.raises(UserProfileError) as failure:
        profiles.resolve(owner)

    assert failure.value.safe_detail == "user owner <redacted> is ambiguous"
    assert owner not in failure.value.safe_detail


def test_profile_error_safe_detail_hides_io_paths(tmp_path: Path) -> None:
    owner = "owner.person@example.test"
    path = tmp_path / owner / "users.json"
    path.parent.mkdir()
    path.write_text("{", encoding="utf-8")

    with pytest.raises(UserProfileError) as failure:
        UserProfileStore(path).list()

    assert failure.value.safe_detail == "failed to read user profiles: JSONDecodeError"
    assert owner not in failure.value.safe_detail
    assert str(path) not in failure.value.safe_detail


def test_profile_error_safe_detail_masks_an_unknown_owner_key(tmp_path: Path) -> None:
    owner_key = "owner.person@example.test"
    profiles = UserProfileStore(tmp_path / "users.json")
    profiles.ensure(
        owner="owner",
        owner_key="key-one",
        login_name="one",
        machine="box",
        machine_key="box-key",
    )

    with pytest.raises(UserProfileError) as failure:
        profiles.set_delivery_agent(owner_key, "worker")

    assert failure.value.safe_detail == "user profile <redacted> was not registered"
    assert owner_key not in failure.value.safe_detail


def test_profile_error_safe_detail_redacts_unknown_free_text() -> None:
    error = UserProfileError("unexpected owner.person@example.test")

    assert error.safe_detail == "<redacted>"
