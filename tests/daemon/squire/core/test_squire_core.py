from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.squire import (
    AppOnboardingPort,
    DeferredAppOnboarding,
)
from hyprial.identity import (
    UserProfileStore)
from hyprial.daemon.impl.squire.setup import SetupStateStore


def test_binding_code_expires_after_ten_minutes(tmp_path: Path) -> None:
    now = [1_000.0]
    store = SetupStateStore(tmp_path / "setup.json", now=lambda: now[0])
    binding = store.ensure_binding("owner-key", "channel:gregpaws:mac-studio:squire")

    now[0] += 601

    with pytest.raises(ValueError, match="invalid or expired"):
        store.verify("owner-key", binding.channel, binding.code)
    replacement = store.ensure_binding("owner-key", binding.channel)
    assert replacement.code != binding.code


def test_profile_allows_cross_user_squire_channel_without_acl(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    store = UserProfileStore(path)
    store.ensure(
        owner="gregpaws",
        owner_key="owner-a",
        login_name="gregpaws",
        machine="mac-a",
        machine_key="machine-a",
    )

    profile, changed = store.set_squire_channel(
        "owner-a", "channel:alicesmith:mac-b:admin-app"
    )

    assert changed == ("users.squireChannel",)
    assert profile.squire_channel == "channel:alicesmith:mac-b:admin-app"
    reloaded = UserProfileStore(path).get("owner-a")
    assert reloaded is not None
    assert reloaded.squire_channel == "channel:alicesmith:mac-b:admin-app"


def test_app_onboarding_placeholder_implements_public_protocol() -> None:
    assert isinstance(DeferredAppOnboarding(), AppOnboardingPort)
