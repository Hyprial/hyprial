"""S3c: the user profile, users.json readers and the squire probe label are gone.

Decision notes: ``users.json`` may be named only by
``identity/impl/onboarding/home_binding.py`` (the list of business files an
adopted home leaves alone); nothing reads it.  The scan is textual on purpose:
it also catches a reader hidden behind a variable or a docstring-only mention
that is really a path builder.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import hyprial
import hyprial.daemon
import hyprial.identity
import hyprial.kernel

SOURCE_ROOT = Path(hyprial.__file__).resolve().parent
USERS_JSON_ALLOWED = {Path("identity/impl/onboarding/home_binding.py")}


def product_files() -> list[Path]:
    return [
        path for path in sorted(SOURCE_ROOT.rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".json", ".md", ".txt", ".toml"}
        and "__pycache__" not in path.parts
        and "_gui" not in path.parts
    ]


@pytest.mark.parametrize(
    "name",
    [
        "UserProfile", "UserProfileStore", "UserProfileError",
        "PreferredReceiver", "NotificationRule",
    ],
)
def test_identity_no_longer_exports_the_profile_vocabulary(name: str) -> None:
    assert not hasattr(hyprial.identity, name)
    assert name not in getattr(hyprial.identity, "__all__", ())


@pytest.mark.parametrize("name", ["active_profile", "probe_profile", "UserProfileStore"])
def test_daemon_no_longer_exports_the_profile_helpers(name: str) -> None:
    assert not hasattr(hyprial.daemon, name)
    assert name not in getattr(hyprial.daemon, "__all__", ())


def test_kernel_has_no_parse_channel_uri() -> None:
    assert not hasattr(hyprial.kernel, "parse_channel_uri")
    assert "parse_channel_uri" not in getattr(hyprial.kernel, "__all__", ())


@pytest.mark.parametrize(
    "module",
    [
        "hyprial.identity.impl.users.profile",
        "hyprial.identity.impl.users._store_lock",
        "hyprial.daemon.impl.identity.profiles",
    ],
)
def test_retired_modules_do_not_import(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_the_capability_store_module_is_the_one_survivor_of_users() -> None:
    module = importlib.import_module("hyprial.identity.impl.users.runtime_capabilities")

    for name in ("RuntimeCapability", "RuntimeCapabilityError", "RuntimeCapabilityStore"):
        assert hasattr(module, name)
    assert not hasattr(module, "UserProfileStore")


def test_only_home_binding_names_users_json() -> None:
    offenders = sorted(
        str(path.relative_to(SOURCE_ROOT))
        for path in product_files()
        if "users.json" in path.read_text(encoding="utf-8", errors="ignore")
        and path.relative_to(SOURCE_ROOT) not in USERS_JSON_ALLOWED
    )

    assert offenders == []


def test_home_binding_only_lists_users_json_as_a_file_to_leave_alone() -> None:
    path = SOURCE_ROOT / "identity/impl/onboarding/home_binding.py"
    text = path.read_text(encoding="utf-8")

    assert text.count("users.json") == 1
    assert 'LEGACY_BUSINESS_TEXT_FILES = ("desired-state.json.v1", "users.json")' in text


def test_the_squire_probe_label_is_gone_everywhere() -> None:
    offenders = sorted(
        str(path.relative_to(SOURCE_ROOT))
        for path in product_files()
        if "squire-runtime-capability"
        in path.read_text(encoding="utf-8", errors="ignore")
    )

    assert offenders == []


def test_the_new_probe_label_is_declared() -> None:
    hits = [
        path for path in product_files()
        if "user-runtime-capability" in path.read_text(encoding="utf-8", errors="ignore")
    ]

    assert hits
