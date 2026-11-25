from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from tests.shell.cli.org.test_cli_org import _org_ipc_env
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import (
    get_services as _cli_get_services,
)
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_org_remove_says_the_user_also_left_the_casdoor_group(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)

    def request(method, params=None, **_kwargs):
        return {"org": "group-acme", "user": "bob", "phase": "complete",
                "operationId": "preview-id", "deviceIds": ["b1"],
                "casdoorGroup": "removed from hyprial/group-acme"}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "remove", "group-acme", "bob", "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["casdoorGroup"] == "removed from hyprial/group-acme"
    assert "lose them too" in payload["note"]
    help_text = CliRunner().invoke(_cli_root_app, ["org", "remove", "--help"]).output
    assert "Casdoor" in help_text


def test_org_create_publish_failure_is_reported_and_exits_nonzero(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {
            "org": "group-foo",
            "published": False,
            "devicePublished": False,
        },
    )

    result = CliRunner().invoke(_cli_root_app, ["org", "create", "foo", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "ok": False,
        "org": "group-foo",
        "published": False,
        "devicePublished": False,
    }


def test_org_create_isolated_local_result_is_success(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {
            "org": "group-foo",
            "spaceId": "space-1",
            "tailnetInitialized": False,
            "published": None,
            "devicePublished": None,
        },
    )

    result = CliRunner().invoke(_cli_root_app, ["org", "create", "foo", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "org": "group-foo",
        "spaceId": "space-1",
        "tailnetInitialized": False,
        "published": None,
        "devicePublished": None,
    }


def test_org_create_accepts_a_bare_name_and_derives_the_space_id(
    monkeypatch, tmp_path: Path
) -> None:
    """`org create foo` must reach IPC as `group-foo` (one rule set, new spelling)."""
    seen: list[dict[str, object]] = []

    def _record(_command: str, payload: dict[str, object]) -> dict[str, object]:
        seen.append(payload)
        return {"org": payload["org"], "spaces": [], "acl": []}

    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_cli_support, "_daemon_request", _record)

    result = CliRunner().invoke(_cli_root_app, ["org", "create", "foo", "--json"])
    assert result.exit_code == 0, result.stdout
    assert seen and seen[-1]["org"] == "group-foo"

    # The prefixed spelling keeps working and is not doubled.
    result = CliRunner().invoke(_cli_root_app, ["org", "create", "group-foo", "--json"])
    assert result.exit_code == 0, result.stdout
    assert seen[-1]["org"] == "group-foo"


def test_org_group_invite_aliases_org_invite() -> None:
    """`org group invite` exposes the same contract as `org invite`.

    What is asserted: the option set (and the required-argument names) are the
    same on both paths, so a copied-then-drifted body is caught here.  What is
    NOT asserted: object identity of the callback.  The message: "reuse the same
    callable" is the construction requirement, but identity is not a checkable
    contract from a test -- the test process can hold two copies of this module
    (checked-out tree + installed package), and two copies of one source are not
    one object.  So the docstring states the property the assertions prove.
    """
    from typer.testing import CliRunner as _Runner

    direct = _Runner().invoke(_cli_root_app, ["org", "invite", "--help"])
    alias = _Runner().invoke(_cli_root_app, ["org", "group", "invite", "--help"])
    assert direct.exit_code == 0, direct.stdout
    assert alias.exit_code == 0, alias.stdout

    # The alias must expose the SAME option set as the command it aliases: a
    # copied body that later drifts shows up here as an option that exists on
    # one path only.  (Comparing whole help text would differ in the usage line,
    # which legitimately names the path.)
    def _options(text: str) -> set[str]:
        return set(re.findall(r"--[a-z][a-z0-9-]*", text))

    assert _options(direct.stdout) == _options(alias.stdout)
    assert "--user" in _options(alias.stdout)


def _login_next_steps(monkeypatch, tmp_path: Path, org_list_effect):
    """Drive the login hint helper directly: it is the only new decision point."""
    from hyprial.shell.impl.cli.commands.login import commands as login_commands

    class _Runtime:
        def request(self, method, params, **_kwargs):
            assert method == "org.list"
            if isinstance(org_list_effect, Exception):
                raise org_list_effect
            return org_list_effect

    class _Services:
        runtime = _Runtime()

    return login_commands._organization_next_steps(_Services())


def test_login_next_steps_when_no_organization(monkeypatch, tmp_path: Path) -> None:
    """Empty list is the ONLY state that earns a hint, and the first item is fixed."""
    steps = _login_next_steps(monkeypatch, tmp_path, {"orgs": []})
    assert steps is not None
    assert steps[0] == "hyprial org create <name>"
    assert all(isinstance(step, str) and step for step in steps)


def test_login_next_steps_silent_when_organizations_exist(
    monkeypatch, tmp_path: Path
) -> None:
    """Non-empty: silence.  A member must never be told to create a second org."""
    assert _login_next_steps(monkeypatch, tmp_path, {"orgs": [{"name": "group-a"}]}) is None


def test_login_next_steps_silent_when_the_read_failed(
    monkeypatch, tmp_path: Path
) -> None:
    """UNKNOWN is not EMPTY: a failed read must not be rendered as 'you have none'."""
    assert _login_next_steps(monkeypatch, tmp_path, RuntimeError("ipc down")) is None
    # An undocumented payload shape is also unknown, not empty.
    assert _login_next_steps(monkeypatch, tmp_path, {"unexpected": 1}) is None


def _create_name(monkeypatch, tmp_path: Path, name: str) -> str:
    """Return the org value that reaches IPC for a given user-supplied name."""
    seen: list[str] = []

    def _record(_command: str, payload: dict[str, object]) -> dict[str, object]:
        seen.append(str(payload["org"]))
        return {"org": payload["org"]}

    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_cli_support, "_daemon_request", _record)
    result = CliRunner().invoke(_cli_root_app, ["org", "create", name, "--json"])
    assert result.exit_code == 0, result.stdout
    return seen[-1]


def test_org_create_prefix_edges(monkeypatch, tmp_path: Path) -> None:
    """The prefix is stripped once and re-added, so the mapping is idempotent.

    Defined edges (they were previously left to chance):
      foo            -> group-foo
      group-foo      -> group-foo        (not group-group-foo)
      group-group-x  -> group-group-x    (stripped once only)
      GROUP-foo      -> rejected         (prefix match is case sensitive)
      group-         -> rejected         (prefix with no name behind it)
    """
    assert _create_name(monkeypatch, tmp_path, "foo") == "group-foo"
    assert _create_name(monkeypatch, tmp_path, "group-foo") == "group-foo"
    assert _create_name(monkeypatch, tmp_path, "group-group-x") == "group-group-x"

    for bad in ("GROUP-foo", "group-"):
        _org_ipc_env(monkeypatch, tmp_path)
        monkeypatch.setattr(
            _cli_support,
            "_daemon_request",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("an invalid name must not reach IPC")
            ),
        )
        result = CliRunner().invoke(_cli_root_app, ["org", "create", bad, "--json"])
        assert result.exit_code == 1, (bad, result.stdout)
        payload = json.loads(result.stdout)
        assert payload["code"] == "INVALID_ARGUMENT"
        # The requirement is "no prefix GRAMMAR in the message", not "the literal
        # never appears": the message echoes what the user typed, and for the
        # input "group-" that echo necessarily contains it.  Assert the grammar
        # phrasing is absent and the actionable sentence is present instead.
        assert "must start with 'group-'" not in payload["error"], payload["error"]
        assert "letters, digits and hyphens" in payload["error"], payload["error"]


def _delete_preview() -> dict:
    return {
        "org": "group-acme",
        "spaceId": "space-1",
        "operationId": "op-1",
        "phase": "pending-confirmation",
        "deviceIds": ["node-b"],
        "confirmed": False,
        "irreversible": True,
    }


def _delete_complete() -> dict:
    return {**_delete_preview(), "phase": "complete", "confirmed": True}


def _two_phase_request(calls: list[dict]):
    def request(method, params=None, **_kwargs):
        calls.append(params)
        return _delete_complete() if params.get("confirmed") else _delete_preview()

    return request


def test_org_delete_requires_yes_off_a_tty(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(_cli_support, "_daemon_request", _two_phase_request(calls))

    result = CliRunner().invoke(_cli_root_app, ["org", "delete", "group-acme", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "USER_ACTION_REQUIRED"
    assert calls == [{"org": "group-acme", "confirmed": False}]


def test_org_delete_with_yes_calls_the_daemon(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(_cli_support, "_daemon_request", _two_phase_request(calls))

    result = CliRunner().invoke(
        _cli_root_app, ["org", "delete", "group-acme", "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["phase"] == "complete"
    assert calls == [
        {"org": "group-acme", "confirmed": False},
        {"org": "group-acme", "confirmed": True, "operationId": "op-1"},
    ]


def test_org_delete_on_a_tty_asks_and_honors_the_answer(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(_cli_support, "_daemon_request", _two_phase_request(calls))
    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: True)

    declined = CliRunner().invoke(_cli_root_app, ["org", "delete", "group-acme"], input="n\n")
    assert declined.exit_code == 1
    assert calls == [{"org": "group-acme", "confirmed": False}]

    confirmed = CliRunner().invoke(_cli_root_app, ["org", "delete", "group-acme"], input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert calls == [
        {"org": "group-acme", "confirmed": False},
        {"org": "group-acme", "confirmed": False},
        {"org": "group-acme", "confirmed": True, "operationId": "op-1"},
    ]


def test_org_create_accepts_parent_flag(monkeypatch, tmp_path: Path) -> None:
    """`hyprial org create <child> --parent <parent>` normalizes and forwards parent."""
    seen: list[tuple[str, dict[str, object]]] = []

    def _record(command: str, payload: dict[str, object], **_kwargs) -> dict[str, object]:
        seen.append((command, dict(payload)))
        return {
            "org": payload["org"],
            "parent": payload["parent"],
            "tailnetId": "sp-root-1",
            "published": True,
            "devicePublished": True,
        }

    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_cli_support, "_daemon_request", _record)
    result = CliRunner().invoke(
        _cli_root_app, ["org", "create", "dept", "--parent", "root", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    assert seen == [("org.create", {"org": "group-dept", "parent": "group-root"})]
    parsed = json.loads(result.stdout)
    assert parsed["parent"] == "group-root"
    assert parsed["tailnetId"] == "sp-root-1"
