"""updates: sources tracks scenarios."""

from __future__ import annotations
import json
from pathlib import Path
import pytest
from typer.testing import CliRunner
import hyprial.daemon.impl.updates as updates
from urllib.parse import urlsplit

from .support import (
    _OFFICIAL_GIT_URL,
    _OLD_GIT_URL,
    _installation,
    _isolate_home,
    remote as remote,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

_FORGEJO_HOST = urlsplit(updates.FORGEJO_GIT_URL).hostname


# --- repository source compatibility -------------------------------------- #


@pytest.mark.parametrize(
    "old_url",
    (
        _OLD_GIT_URL,
        f"ssh://git@{_FORGEJO_HOST}/HyprialOS/harness-bridge-py",
        f"ssh://git@{_FORGEJO_HOST.upper()}:22/HyprialOS/harness-bridge-py.git/",
        f"git+ssh://git@{_FORGEJO_HOST}/HyprialOS/harness-bridge-py.git",
        f"git@{_FORGEJO_HOST}:HyprialOS/harness-bridge-py.git",
    ),
)
def test_installation_git_url_ignores_known_pre_rename_sources(
    old_url: str,
) -> None:
    assert updates.installation_git_url(_installation(url=old_url)) == _OFFICIAL_GIT_URL


@pytest.mark.parametrize(
    "custom_url",
    (
        "file:///srv/hyprial.git",
        "ssh://git@example.invalid/team/harness-bridge-py.git",
        "https://mirror.invalid/HyprialOS/harness-bridge-py.git",
        f"ssh://deploy@{_FORGEJO_HOST}/HyprialOS/harness-bridge-py.git",
        f"ssh://git@{_FORGEJO_HOST}/Other/harness-bridge-py.git",
    ),
)
def test_installation_git_url_ignores_unknown_custom_registries(
    custom_url: str,
) -> None:
    installation = _installation(url=custom_url)
    assert updates.installation_git_url(installation) == _OFFICIAL_GIT_URL
    assert updates.installation_origin(installation) == custom_url


def test_installation_git_url_uses_default_without_pep610_source() -> None:
    assert updates.installation_git_url(_installation(url=None)) == _OFFICIAL_GIT_URL


# --- retired track compatibility ------------------------------------------ #


def test_legacy_track_residue_warns_and_is_never_rewritten(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    settings = home / "settings.json"
    settings_bytes = b'{"version":1,"update_track":"dev","keep":true}\n'
    settings.write_bytes(settings_bytes)

    warning = updates.retired_track_warning(home)

    assert warning is not None
    assert "retired" in warning
    assert "settings.json" in warning
    assert settings.read_bytes() == settings_bytes


def test_legacy_config_json_has_no_effect_on_the_warning(tmp_path: Path) -> None:
    """``config.json`` is a TypeScript leftover this package must not read.

    Kept as a live assertion rather than deleted along with the code it
    covered: a deleted test proves nothing, while this one goes red the
    moment anyone reintroduces a read of ``config.json`` -- including one
    added "only to warn", which is exactly how it stayed wired in long after
    the client that wrote it was gone.

    Both directions are asserted on purpose. Without the first case, a
    function that always warned would satisfy the second, and the test would
    be measuring nothing.
    """

    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.json"
    config_bytes = b'{"updateTrack":"nightly","update_track":"dev"}\n'
    config.write_bytes(config_bytes)

    # A home whose ONLY track residue lives in config.json warns about nothing.
    assert updates.retired_track_warning(home) is None

    # And when settings.json does produce a warning, config.json contributes
    # no text to it.
    (home / "settings.json").write_text('{"update_track":"dev"}\n', encoding="utf-8")
    warning = updates.retired_track_warning(home)
    assert warning is not None and "settings.json" in warning
    assert "config.json" not in warning

    # Never rewritten, in either case.
    assert config.read_bytes() == config_bytes


def test_malformed_settings_json_warns_and_will_refuse_upgrade(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("[not json", encoding="utf-8")
    warning = updates.retired_track_warning(home)
    assert warning is not None and "refuse" in warning


# --- updateTrack selection (opt-in three-track resolution) ---------------- #


def test_read_update_track_absent_file_or_key_keeps_legacy(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert updates.read_update_track(home) is None
    home.mkdir()
    (home / "settings.json").write_text('{"version":1}\n', encoding="utf-8")
    assert updates.read_update_track(home) is None


@pytest.mark.parametrize("track", updates.TRACK_TAGS)
def test_read_update_track_accepts_exact_track_values(
    tmp_path: Path, track: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"updateTrack": track}), encoding="utf-8"
    )
    assert updates.read_update_track(home) == track


@pytest.mark.parametrize(
    "content",
    (
        '{"updateTrack":"canary"}',
        '{"updateTrack":"DEV"}',
        '{"updateTrack":["dev"]}',
        "[not json",
        "[1,2]",
    ),
)
def test_read_update_track_refuses_to_guess(tmp_path: Path, content: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(content, encoding="utf-8")
    with pytest.raises(updates.UpdateProbeError):
        updates.read_update_track(home)


def test_valid_update_track_produces_no_retired_warning(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        '{"version":1,"updateTrack":"nightly"}\n', encoding="utf-8"
    )
    assert updates.retired_track_warning(home) is None


def test_invalid_update_track_warns_without_throwing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        '{"updateTrack":"canary"}\n', encoding="utf-8"
    )
    warning = updates.retired_track_warning(home)
    assert warning is not None and "canary" in warning and "refuse" in warning


def test_set_update_track_is_no_longer_a_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    result = CliRunner().invoke(_cli_root_app, ["set", "update_track", "dev"])
    assert result.exit_code != 0
    assert not (tmp_path / "home" / "settings.json").exists()
    help_result = CliRunner().invoke(_cli_root_app, ["help", "--json"])
    assert help_result.exit_code == 0
    assert "  hyprial set" not in json.loads(help_result.stdout)["usage"].splitlines()
