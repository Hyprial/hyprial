"""Allen 2026-09-26: when the daemon starts, tell the owner if their squire is
not set up, and how to fix it.  Squire first; user-proxy comes after it.

The check reads two local files (settings.json via the owner resolver,
users.json via the profile store).  These tests pin what it says in each
state, that it is silent where squire cannot apply, and that it never
touches the daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.identity import UserProfileStore
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "allen"
CHANNEL = f"channel:{OWNER}:box-1:allen-squire"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: state)
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: tmp_path)
    monkeypatch.setenv("HYPRIAL_OWNER", OWNER)
    monkeypatch.delenv("HYPRIAL_NETWORK_ISOLATED", raising=False)

    def no_daemon(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the setup check must not talk to the daemon")

    monkeypatch.setattr(_cli_support, "_daemon_request", no_daemon)
    return state


def _profile(state: Path, *, channel: bool, open_id: bool, proxy: bool = False) -> None:
    store = UserProfileStore(state / "users.json")
    store.ensure(owner=OWNER, owner_key=OWNER, login_name=OWNER, machine="box-1", machine_key="box-1")
    if channel:
        store.set_squire_channel(OWNER, CHANNEL)
    if open_id:
        store.bind_owner_open_id(OWNER, channel=CHANNEL, open_id="ou_0123456789abcdef")
    if proxy:
        store.set_delivery_agent(OWNER, f"{OWNER}-proxy")


def test_no_profile_names_the_command_and_what_it_costs(home: Path) -> None:
    warning = _cli_owner_1._squire_setup_warning()
    assert warning is not None
    assert warning["code"] == "SQUIRE_NOT_CONFIGURED"
    assert warning["data"] == {
        "owner": OWNER,
        "missing": ["profile", "channel", "ownerOpenId"],
        "nextStep": "hyprial squire setup",
    }
    message = warning["message"]
    assert "hyprial squire setup" in message
    assert f"user:{OWNER}" in message  # what the person loses, in their terms
    assert "no squire profile yet" in message


def test_a_half_finished_setup_names_the_missing_step(home: Path) -> None:
    _profile(home, channel=True, open_id=False)
    warning = _cli_owner_1._squire_setup_warning()
    assert warning is not None
    assert warning["data"]["missing"] == ["ownerOpenId"]
    assert "your Feishu account is not bound" in warning["message"]


def test_a_complete_squire_then_guides_the_user_proxy(home: Path) -> None:
    """Squire first; once it is complete, the same check moves to user-proxy."""
    _profile(home, channel=True, open_id=True)
    warning = _cli_owner_1._squire_setup_warning()
    assert warning is not None
    assert warning["code"] == "SQUIRE_USER_PROXY_NOT_CONFIGURED"
    next_step = "hyprial user proxy setup --adapter <your-adapter> --route <route>"
    assert warning["data"] == {"owner": OWNER, "machine": "box-1", "nextStep": next_step}
    message = warning["message"]
    assert next_step in message
    assert f"user:{OWNER}" in message
    assert "not the squire's" in message  # setup refuses the squire's adapter
    assert "on box-1" in message  # setup must run on the preferred receiver


def test_a_complete_squire_and_user_proxy_is_silent(home: Path) -> None:
    _profile(home, channel=True, open_id=True, proxy=True)
    assert _cli_owner_1._squire_setup_warning() is None


def test_an_unfinished_squire_is_named_before_the_user_proxy(home: Path) -> None:
    _profile(home, channel=True, open_id=False)
    warning = _cli_owner_1._squire_setup_warning()
    assert warning is not None
    assert warning["code"] == "SQUIRE_NOT_CONFIGURED"


def test_no_owner_yet_is_silent(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Before login there is no owner; login's own next steps cover that."""
    monkeypatch.delenv("HYPRIAL_OWNER")
    monkeypatch.setattr("hyprial.daemon.node_owner_or_none", lambda: None)
    assert _cli_owner_1._squire_setup_warning() is None


@pytest.mark.parametrize("value", ["1", "true"])
def test_an_isolated_daemon_is_silent(home: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """An isolated daemon cannot reach Feishu, so a squire could not help it."""
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", value)
    assert _cli_owner_1._squire_setup_warning() is None


def test_an_unreadable_profile_file_says_so_instead_of_crashing_start(home: Path) -> None:
    (home / "users.json").write_text("{not json", encoding="utf-8")
    warning = _cli_owner_1._squire_setup_warning()
    assert warning is not None
    assert warning["code"] == "SQUIRE_PROFILE_UNREADABLE"
    assert "hyprial squire setup" in warning["message"]


def test_the_check_never_writes(home: Path) -> None:
    _cli_owner_1._squire_setup_warning()
    assert not (home / "users.json").exists()


def test_announce_prints_only_setup_guidance(capsys: pytest.CaptureFixture[str]) -> None:
    _cli_owner_1._announce_setup_guidance(
        {
            "warnings": [
                {"code": "ORG_CONTEXT_ABSENT", "message": "org-context absent"},
                {"code": "SQUIRE_NOT_CONFIGURED", "message": "your squire is not set up"},
            ]
        }
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "hyprial: your squire is not set up\n"


def test_the_user_proxy_reminder_can_be_turned_off_and_back_on(home: Path) -> None:
    """hq review of #962: an owner who doesn't want a user-proxy got this
    warning on every init/restart/start with no way to stop it."""
    _profile(home, channel=True, open_id=True)
    warning = _cli_owner_1._squire_setup_warning()
    assert warning is not None
    assert "hyprial config set hints.userProxy off" in warning["message"]

    settings = home.parent / "settings.json"
    settings.write_text('{"owner": "allen", "other": 1}\n', encoding="utf-8")
    _cli_owner_1._write_setup_hint("userProxy", "OFF", home.parent)
    assert _cli_owner_1._squire_setup_warning() is None
    record = __import__("json").loads(settings.read_text(encoding="utf-8"))
    assert record == {"owner": "allen", "other": 1, "hints": {"userProxy": "off"}}

    _cli_owner_1._write_setup_hint("userProxy", "on", home.parent)
    assert _cli_owner_1._squire_setup_warning() is not None


def test_turning_the_reminder_off_never_hides_an_unfinished_squire(home: Path) -> None:
    _profile(home, channel=True, open_id=False)
    _cli_owner_1._write_setup_hint("userProxy", "off", home.parent)
    warning = _cli_owner_1._squire_setup_warning()
    assert warning is not None
    assert warning["code"] == "SQUIRE_NOT_CONFIGURED"


def test_a_bad_hint_value_is_refused_and_writes_nothing(home: Path) -> None:
    with pytest.raises(ValueError, match="on\\|off"):
        _cli_owner_1._write_setup_hint("userProxy", "maybe", home.parent)
    assert not (home.parent / "settings.json").exists()


def test_config_set_writes_the_hint(home: Path) -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(_cli_root_app, ["config", "set", "hints.userProxy", "off", "--json"])
    assert result.exit_code == 0, result.output
    assert __import__("json").loads(result.stdout)["key"] == "hints.userProxy"
    assert _cli_owner_1._setup_hint_enabled("userProxy") is False
