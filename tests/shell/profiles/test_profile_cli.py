"""``hyprial profile list|use|create`` surface (login U1, T7-CLI + T8 + T10).

Isolation: every test repoints ``Path.home`` at ``tmp_path`` so ``~/.hyprial``
becomes a scratch home — these commands address ``Path.home()/.hyprial`` per the
spec, so patching home is the honest seam (the autouse fixture's ``HYPRIAL_HOME``
only selects the *active* home, it must not relocate the profiles root).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner


import hyprial.daemon.impl.configuration.network_profile as network_profile
from hyprial.daemon.impl.configuration.network_profile import DEFAULT_INVITE_BASE_URL, PROFILE_FILENAME, NetworkProfile
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.commands.profile.commands as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

runner = CliRunner()

_CREATE_ARGS = [
    "--issuer",
    "https://auth.acme.io",
    "--client-id",
    "test-client-id",
]


def _scratch_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "scratch-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _create(*extra: str, name: str = "acme") -> Any:
    return runner.invoke(_cli_root_app, ["profile", "create", name, *_CREATE_ARGS, *extra])


def _tree(root: Path) -> dict[str, bytes | None]:
    """Path → content (``None`` for directories) for write-detection."""

    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        snapshot[str(path.relative_to(root))] = (
            path.read_bytes() if path.is_file() else None
        )
    return snapshot


_TOKENISH = re.compile(r"token|secret|credential|password|device_?code", re.I)


def _assert_no_tokenish(value: object, where: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert not _TOKENISH.search(str(key)), (
                f"{where}: token-class key {key!r} must not appear in "
                "profile output (D: --json 绝不输出机密)"
            )
            _assert_no_tokenish(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_tokenish(item, f"{where}[{index}]")


# -- T7 (CLI half): create refuses a differing value --------------------------


def test_t7_create_same_values_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _scratch_home(monkeypatch, tmp_path)

    first = _create("--json")
    assert first.exit_code == 0, first.output
    record_path = (
        tmp_path / "scratch-home" / ".hyprial" / "profiles" / "acme" / PROFILE_FILENAME
    )
    bytes_after_first = record_path.read_bytes()
    assert json.loads(first.stdout)["created"] is True

    second = _create("--json")
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["created"] is False
    assert record_path.read_bytes() == bytes_after_first


def test_t7_create_conflicting_values_is_refused_and_file_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _scratch_home(monkeypatch, tmp_path)
    assert _create().exit_code == 0
    record_path = (
        tmp_path / "scratch-home" / ".hyprial" / "profiles" / "acme" / PROFILE_FILENAME
    )
    before = record_path.read_bytes()

    conflict = runner.invoke(
        _cli_root_app,
        [
            "profile",
            "create",
            "acme",
            "--issuer",
            "https://auth.other.io",  # same name, different issuer
            "--client-id",
            "test-client-id",
            "--json",
        ],
    )

    assert conflict.exit_code == 1, conflict.output
    payload = json.loads(conflict.stdout)
    assert payload["code"] == "PROFILE_EXISTS"
    assert record_path.read_bytes() == before, "the refused create must not touch the file"


def test_t7_create_refuses_broken_existing_record_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _scratch_home(monkeypatch, tmp_path)
    record_path = (
        tmp_path / "scratch-home" / ".hyprial" / "profiles" / "acme" / PROFILE_FILENAME
    )
    record_path.parent.mkdir(parents=True)
    record_path.write_text('{"version": 3}\n', encoding="utf-8")

    result = _create("--json")

    assert result.exit_code == 1
    assert "version must be 2" in result.stdout
    assert record_path.read_text(encoding="utf-8") == '{"version": 3}\n'


# -- T8: CLI surface -----------------------------------------------------------


def test_d4_list_json_fields_and_no_tokenish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _scratch_home(monkeypatch, tmp_path)
    assert _create().exit_code == 0
    assert (
        runner.invoke(
            _cli_root_app,
            [
                "profile",
                "create",
                "tslab",
                "--issuer",
                "https://auth.tslab.io",
                "--client-id",
                "tslab-client-id",
                "--json",
            ],
        ).exit_code
        == 0
    )

    listed = runner.invoke(_cli_root_app, ["profile", "list", "--json"])

    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.stdout)
    _assert_no_tokenish(payload)
    rows = payload["profiles"]
    assert [row["name"] for row in rows] == ["hyprial", "acme", "tslab"]
    default_row, acme_row, tslab_row = rows
    assert default_row["issuer"] == "https://auth.hyprial.com"
    # The tailnet cutover retired controlPlane/join from the record and the
    # list rows: reachability is the Tailcat sidecar's business.
    for row in rows:
        assert "controlPlane" not in row
        assert "join" not in row
    assert default_row["home"] == str(home / ".hyprial")
    assert default_row["current"] is False  # HYPRIAL_HOME points at the fixture home
    assert acme_row["clientId"] == "test-client-id"
    assert tslab_row["issuer"] == "https://auth.tslab.io"


def test_t8_list_marks_the_configured_home_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _scratch_home(monkeypatch, tmp_path)
    assert _create().exit_code == 0
    acme_home = home / ".hyprial" / "profiles" / "acme"
    monkeypatch.setenv("HYPRIAL_HOME", str(acme_home))

    listed = runner.invoke(_cli_root_app, ["profile", "list", "--json"])

    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)["profiles"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["acme"]["current"] is True
    assert by_name["hyprial"]["current"] is False


def test_t8_use_text_prints_only_the_export_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _scratch_home(monkeypatch, tmp_path)
    assert _create().exit_code == 0

    used = runner.invoke(_cli_root_app, ["profile", "use", "acme"])

    assert used.exit_code == 0, used.output
    acme_home = (home / ".hyprial" / "profiles" / "acme").resolve()
    assert used.output == f"export HYPRIAL_HOME={acme_home}\n", (
        "text output must be exactly one eval-safe export line (D-U1-1)"
    )


def test_t8_use_json_shape_and_no_tokenish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _scratch_home(monkeypatch, tmp_path)
    assert _create().exit_code == 0

    used = runner.invoke(_cli_root_app, ["profile", "use", "acme", "--json"])

    assert used.exit_code == 0, used.output
    payload = json.loads(used.stdout)
    _assert_no_tokenish(payload)
    assert set(payload) >= {"home", "profile"}
    acme_home = (home / ".hyprial" / "profiles" / "acme").resolve()
    assert payload["home"] == str(acme_home)
    assert payload["profile"] == {
        "version": 2,
        "org": "acme",
        "issuer": "https://auth.acme.io",
        "clientId": "test-client-id",
        "inviteBaseUrl": DEFAULT_INVITE_BASE_URL,
    }


def test_t8_use_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _scratch_home(monkeypatch, tmp_path)
    assert _create().exit_code == 0
    home_root = tmp_path / "scratch-home"
    before = _tree(home_root)

    used = runner.invoke(_cli_root_app, ["profile", "use", "acme"])
    assert used.exit_code == 0, used.output

    assert _tree(home_root) == before, (
        "profile use must not write any file: no machine-level pointer, no "
        "state, no settings (D-U1-1)"
    )


def test_t8_create_json_shape_and_directory_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _scratch_home(monkeypatch, tmp_path)

    created = _create("--json")

    assert created.exit_code == 0, created.output
    payload = json.loads(created.stdout)
    _assert_no_tokenish(payload)
    assert payload["created"] is True
    profile_home = tmp_path / "scratch-home" / ".hyprial" / "profiles" / "acme"
    assert payload["home"] == str(profile_home)
    mode = profile_home.stat().st_mode & 0o777
    assert mode == 0o700, f"profile home must be 0700; got {oct(mode)}"
    record = json.loads((profile_home / PROFILE_FILENAME).read_text(encoding="utf-8"))
    assert record == NetworkProfile(
        org="acme",
        issuer="https://auth.acme.io",
        client_id="test-client-id",
    ).as_record()


def test_t8_create_never_writes_settings_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _scratch_home(monkeypatch, tmp_path)
    hyprial_root = home / ".hyprial"
    hyprial_root.mkdir(exist_ok=True)
    settings = hyprial_root / "settings.json"
    settings.write_text('{"owner": "keep-me"}\n', encoding="utf-8")

    assert _create().exit_code == 0

    assert settings.read_text(encoding="utf-8") == '{"owner": "keep-me"}\n'


# -- T10: every command the guidance names actually exists ---------------------

# Same shape as tests/test_identity_guidance.py: a cited remediation command
# is advice an operator with no other way out will follow, so it is checked
# for existing, not for being good intentions.
# Rich wraps error guidance according to terminal width and the temporary
# path's length. Whitespace between command words is not a command change.
_COMMAND = re.compile(r"hyprial\s+((?:[a-z][a-z-]*)(?:\s+[a-z][a-z-]*)?)\s+(--[a-z][a-z-]*)")


@pytest.mark.parametrize("separator", [" ", "\n", " \n"])
def test_guidance_command_extraction_survives_line_wrapping(separator: str) -> None:
    command, option = _COMMAND.findall(f"hyprial profile{separator}create --issuer <url>")[0]
    assert command.split() == ["profile", "create"]
    assert option == "--issuer"


def _guidance_surfaces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Module docstring, profile command docstrings, and the real error texts."""

    _scratch_home(monkeypatch, tmp_path)
    errors: list[str] = []

    missing = runner.invoke(_cli_root_app, ["profile", "use", "ghost"])
    errors.append(missing.output)
    invalid_name = runner.invoke(
        _cli_root_app, ["profile", "use", "a/b", "--json"]
    )
    errors.append(invalid_name.stdout)
    assert _create().exit_code == 0
    conflict = runner.invoke(
        _cli_root_app,
        [
            "profile",
            "create",
            "acme",
            "--issuer",
            "https://auth.other.io",
            "--client-id",
            "test-client-id",
        ],
    )
    errors.append(conflict.output)

    docs = "\n".join(
        [
            network_profile.__doc__ or "",
            _cli_owner_1.profile_use.__doc__ or "",
            _cli_owner_1.profile_create.__doc__ or "",
            _cli_owner_1.profile_list.__doc__ or "",
        ]
    )
    return "\n".join(errors) + "\n" + docs


def test_t10_every_command_named_by_profile_guidance_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = _guidance_surfaces(monkeypatch, tmp_path)

    cited = _COMMAND.findall(text)
    assert cited, f"guidance names no command to check: {text!r}"
    for command, option in cited:
        result = runner.invoke(_cli_root_app, [*command.split(), "--help"])
        assert result.exit_code == 0, (
            f"guidance cites 'hyprial {command}', which has no --help: {result.output}"
        )
        assert option in result.output, (
            f"guidance cites 'hyprial {command} {option}', but {option} is not in "
            f"its --help -- following the instruction yields NoSuchOption.\n"
            f"{result.output}"
        )
