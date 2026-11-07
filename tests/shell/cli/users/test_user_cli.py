"""CLI contract for the daemon-owned identity resolver surface."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


SOURCES = (
    "casdoor-login",
    "org-directory",
    "local-override",
    "legacy-user-bind",
    "legacy-identities",
)


def _binding(
    user: str,
    source: str,
    *,
    kind: str = "member",
    union_id: str | None = None,
    open_id: str | None = None,
) -> dict[str, Any]:
    return {
        "user": user,
        "kind": kind,
        "platform": "lark",
        "unionId": union_id or f"on_{user}",
        "source": source,
        "confirmedBy": "directory" if source == "org-directory" else "allen",
        "updatedAt": "2026-10-07T10:00:00Z",
        "accounts": [
            {
                "adapter": "cli-developer",
                "openId": open_id or f"ou_{user}",
                "observedAtMs": 1791348000000,
            }
        ],
    }


def _legacy_open_binding(user: str, open_id: str) -> dict[str, Any]:
    return {
        "user": user,
        "kind": "member",
        "platform": "lark",
        "unionId": None,
        "source": "legacy-identities",
        "confirmedBy": "allen",
        "updatedAt": "2026-10-07T10:00:00Z",
        "accounts": [
            {
                "adapter": "cli-developer",
                "openId": open_id,
                "observedAtMs": 1791348000000,
            }
        ],
    }


class FakeIdentityDaemon:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, str] = {}
        self.failure_details: dict[str, str] = {}
        self.filter_user_list = True
        self.bindings = [
            _binding("owner", "casdoor-login"),
            _binding("member", "org-directory"),
            _binding("guest-1", "local-override", kind="guest"),
            _binding("legacy", "legacy-identities"),
        ]
        self.users = [
            {
                "user": "owner",
                "kind": "member",
                "owner": "owner@example.com",
                "nickname": "Owner",
            },
            {"user": "guest-1", "kind": "guest", "owner": None, "nickname": "Guest"},
        ]

    def __call__(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        **_kwargs: Any,
    ) -> Any:
        payload = dict(params or {})
        self.calls.append((method, payload))
        if method in self.failures:
            code = self.failures[method]
            detail = self.failure_details.get(
                method, f"{code.lower().replace('_', ' ')}"
            )
            raise _cli_get_services().CliError(code, detail)
        if method == "identity.bindings.list":
            source = payload.get("source")
            return {
                "bindings": [
                    row for row in self.bindings
                    if source is None or row["source"] == source
                ]
            }
        if method == "identity.resolve":
            if payload.get("openId") == "ou_outbound":
                return {
                    "user": "owner",
                    "kind": "member",
                    "source": "local-override",
                    "confirmedBy": "allen",
                    "unionId": None,
                    "verified": False,
                    "accounts": [
                        {
                            "adapter": "cli-developer",
                            "openId": "ou_outbound",
                            "observedAtMs": 1791348000000,
                        }
                    ],
                }
            if payload.get("unionId") == "on_guest":
                return {
                    "user": "guest-1",
                    "kind": "guest",
                    "owner": None,
                    "source": "local-override",
                    "confirmedBy": "allen",
                    "verified": True,
                    "accounts": self.bindings[2]["accounts"],
                }
            return {
                "user": "owner",
                "kind": "member",
                "source": "casdoor-login",
                "verified": True,
                "accounts": self.bindings[0]["accounts"],
            }
        if method == "identity.override.set":
            if payload.get("openId") == "ou_outbound":
                return {
                    "user": str(payload["user"]),
                    "kind": "member",
                    "platform": "lark",
                    "unionId": None,
                    "source": "local-override",
                    "confirmedBy": str(payload["confirmedBy"]),
                    "updatedAt": "2026-10-07T10:00:00Z",
                    "accounts": [
                        {
                            "adapter": str(payload["adapter"]),
                            "openId": str(payload["openId"]),
                            "observedAtMs": 1791348000000,
                        }
                    ],
                }
            return _binding(
                str(payload["user"]),
                "local-override",
                union_id=payload.get("unionId"),
                open_id=payload.get("openId"),
            )
        if method == "identity.override.clear":
            return {"cleared": True}
        if method in {"identity.binding.withdraw", "identity.binding.publish"}:
            orgs = [payload["org"]] if "org" in payload else ["group-a"]
            return {"orgs": orgs, "operatorVerified": False}
        if method == "identity.users.add":
            # Design §5 round 4: the store generates the key; enveloped result.
            return {"user": {"user": "guest-new", **payload}}
        if method == "identity.users.list":
            key = payload.get("user")
            return {
                "users": [
                    row
                    for row in self.users
                    if not self.filter_user_list or key is None or row["user"] == key
                ]
            }
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture
def identity_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeIdentityDaemon:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    (home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": "owner@example.com"}), encoding="utf-8"
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    daemon = FakeIdentityDaemon()
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    return daemon


def _run_json(*args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(_cli_root_app, ["user", *args, "--json"])
    assert result.stdout, result.stderr
    return result.exit_code, json.loads(result.stdout)


@pytest.mark.parametrize("source", SOURCES)
def test_list_calls_bindings_list_and_preserves_every_source_row(
    identity_cli: FakeIdentityDaemon, source: str
) -> None:
    code, result = _run_json("list", "--source", source)

    assert code == 0
    assert result == {
        "ok": True,
        "rows": [row for row in identity_cli.bindings if row["source"] == source],
    }
    assert identity_cli.calls[-1] == ("identity.bindings.list", {"source": source})


def test_list_human_columns_are_the_frozen_contract(
    identity_cli: FakeIdentityDaemon,
) -> None:
    result = CliRunner().invoke(_cli_root_app, ["user", "list"])

    assert result.exit_code == 0, result.stderr
    header = result.stdout.splitlines()[0]
    for name in (
        "user",
        "kind",
        "platform",
        "unionId",
        "source",
        "confirmedBy",
        "updatedAt",
        "accounts",
    ):
        assert name in header


def test_account_only_rows_render_as_outbound_only_with_empty_union_id(
    identity_cli: FakeIdentityDaemon,
) -> None:
    account_only = {
        "user": "owner",
        "kind": "member",
        "platform": "lark",
        "unionId": None,
        "source": "local-override",
        "confirmedBy": "allen",
        "updatedAt": "2026-10-07T10:00:00Z",
        "accounts": [
            {
                "adapter": "cli-developer",
                "openId": "ou_outbound",
                "observedAtMs": 1791348000000,
            }
        ],
    }
    identity_cli.bindings = [account_only]

    listed = CliRunner().invoke(_cli_root_app, ["user", "list"])
    bound = CliRunner().invoke(
        _cli_root_app,
        [
            "user",
            "bind",
            "owner",
            "--adapter",
            "cli-developer",
            "--open-id",
            "ou_outbound",
            "--confirmed-by",
            "allen",
        ],
    )
    resolved = CliRunner().invoke(
        _cli_root_app, ["user", "whois", "cli-developer", "ou_outbound"]
    )

    for result in (listed, bound, resolved):
        assert result.exit_code == 0, result.stderr
        assert "account-only (outbound only)" in result.stdout
    listed_row = next(line for line in listed.stdout.splitlines() if "ou_outbound" in line)
    assert "null" not in listed_row
    assert "verified: false" in resolved.stdout
    assert "unionId: \n" in resolved.stdout


def test_bind_and_unbind_use_exact_override_payloads(
    identity_cli: FakeIdentityDaemon,
) -> None:
    assert _run_json(
        "bind", "owner", "--union-id", "on_owner", "--confirmed-by", "allen"
    )[0] == 0
    assert identity_cli.calls[-1] == (
        "identity.override.set",
        {
            "platform": "lark",
            "unionId": "on_owner",
            "user": "owner",
            "confirmedBy": "allen",
        },
    )

    assert _run_json(
        "bind",
        "guest-1",
        "--adapter",
        "cli-developer",
        "--open-id",
        "ou_guest",
        "--confirmed-by",
        "allen",
    )[0] == 0
    assert identity_cli.calls[-1] == (
        "identity.override.set",
        {
            "adapter": "cli-developer",
            "openId": "ou_guest",
            "user": "guest-1",
            "confirmedBy": "allen",
        },
    )

    assert _run_json("unbind", "--union-id", "on_owner")[1]["cleared"] is True
    assert identity_cli.calls[-1] == (
        "identity.override.clear",
        {"platform": "lark", "unionId": "on_owner"},
    )
    assert _run_json(
        "unbind", "--adapter", "cli-developer", "--open-id", "ou_guest"
    )[0] == 0
    assert identity_cli.calls[-1] == (
        "identity.override.clear",
        {"adapter": "cli-developer", "openId": "ou_guest"},
    )


def test_removed_bind_flags_and_old_unbind_shape_are_rejected(
    identity_cli: FakeIdentityDaemon,
) -> None:
    runner = CliRunner()
    removed = runner.invoke(
        _cli_root_app,
        [
            "user",
            "bind",
            "owner",
            "--adapter",
            "cli-developer",
            "--open-id",
            "ou_owner",
            "--confirmed-by",
            "allen",
            "--dm-chat-id",
            "oc_old",
            "--source",
            "manual:old",
        ],
    )
    old_unbind = runner.invoke(
        _cli_root_app,
        [
            "user",
            "unbind",
            "owner",
            "--adapter",
            "cli-developer",
            "--open-id",
            "ou_owner",
            "--confirmed-by",
            "allen",
        ],
    )

    assert removed.exit_code == 2
    assert "No such option" in removed.output
    assert old_unbind.exit_code == 2
    assert identity_cli.calls == []


def test_whois_supports_account_and_union_id_and_warns_for_guest(
    identity_cli: FakeIdentityDaemon,
) -> None:
    account = CliRunner().invoke(
        _cli_root_app, ["user", "whois", "cli-developer", "ou_owner"]
    )
    assert account.exit_code == 0, account.stderr
    assert "source: casdoor-login" in account.stdout
    assert "verified: true" in account.stdout
    assert identity_cli.calls[-1] == (
        "identity.resolve",
        {"adapter": "cli-developer", "openId": "ou_owner"},
    )

    guest = CliRunner().invoke(
        _cli_root_app, ["user", "whois", "--union-id", "on_guest"]
    )
    assert guest.exit_code == 0, guest.stderr
    assert "source: local-override" in guest.stdout
    assert "verified: true" in guest.stdout
    assert "owner: null" in guest.stdout
    assert "never authorizes" in guest.stdout
    assert identity_cli.calls[-1] == (
        "identity.resolve",
        {"platform": "lark", "unionId": "on_guest"},
    )


def test_add_and_show_use_users_ipc(identity_cli: FakeIdentityDaemon) -> None:
    code, added = _run_json(
        "add",
        "--kind",
        "guest",
        "--display-name",
        "Visitor",
        "--confirmed-by",
        "allen",
    )
    assert code == 0
    assert added["user"]["user"] == "guest-new"
    assert identity_cli.calls[-1] == (
        "identity.users.add",
        {"kind": "guest", "displayName": "Visitor", "confirmedBy": "allen"},
    )
    human = CliRunner().invoke(
        _cli_root_app,
        ["user", "add", "--kind", "guest", "--display-name", "Visitor", "--confirmed-by", "allen"],
    )
    assert human.exit_code == 0, human.stderr
    assert "added guest guest-new" in human.stdout

    code, shown = _run_json("show", "owner")
    assert code == 0
    assert shown["user"] == "owner"
    assert identity_cli.calls[-1] == ("identity.users.list", {"user": "owner"})


def test_show_selects_the_requested_user_when_daemon_returns_the_full_list(
    identity_cli: FakeIdentityDaemon,
) -> None:
    identity_cli.filter_user_list = False
    identity_cli.users = [
        {"userKey": "guest-1", "kind": "guest", "owner": None},
        {"userKey": "owner", "kind": "member", "owner": "owner@example.com"},
    ]

    code, shown = _run_json("show", "owner")

    assert code == 0
    assert shown["userKey"] == "owner"
    assert identity_cli.calls[-1] == ("identity.users.list", {"user": "owner"})


def test_bindings_audit_is_read_only_and_classifies_every_legacy_row(
    identity_cli: FakeIdentityDaemon,
) -> None:
    identity_cli.bindings = [
        _binding("same", "legacy-identities", union_id="on_same"),
        _binding("same", "org-directory", union_id="on_same"),
        _binding("old", "legacy-identities", union_id="on_conflict"),
        _binding("new", "local-override", union_id="on_conflict"),
        _binding("only", "legacy-identities", union_id="on_only"),
        _binding("bound", "legacy-user-bind", union_id="on_bound"),
    ]

    code, result = _run_json("bindings", "audit")

    assert code == 0
    # Both legacy stores are audited and keep their own label (§5 @ 80dea557).
    assert [(row["source"], row["audit"]) for row in result["rows"]] == [
        ("legacy-user-bind", "only-legacy"),
        ("legacy-identities", "confirmed elsewhere"),
        ("legacy-identities", "conflicting"),
        ("legacy-identities", "only-legacy"),
    ]
    assert identity_cli.calls == [
        ("identity.bindings.list", {"source": "legacy-user-bind"}),
        ("identity.bindings.list", {"source": "legacy-identities"}),
        ("identity.bindings.list", {}),
    ]


def test_bindings_audit_grandfathers_open_id_rows_and_suggests_read_only_migration(
    identity_cli: FakeIdentityDaemon,
) -> None:
    identity_cli.bindings = [
        _legacy_open_binding("observed", "ou_observed"),
        _binding(
            "observed",
            "org-directory",
            union_id="on_observed",
            open_id="ou_observed",
        ),
        _legacy_open_binding("unknown", "ou_unknown"),
    ]

    code, result = _run_json("bindings", "audit")

    assert code == 0
    rows = {row["user"]: row for row in result["rows"]}
    assert rows["observed"]["category"] == "confirmed-elsewhere"
    assert rows["observed"]["audit"] == "confirmed elsewhere"
    assert rows["observed"]["suggestedCommand"] is None
    assert "suggestedUnionIdSource" not in rows["observed"]
    assert rows["unknown"]["category"] == "open-id-only-grandfathered"
    assert rows["unknown"]["suggestedCommand"] == (
        "hyprial user bind unknown --union-id <UNION_ID> --confirmed-by allen"
    )
    assert "suggestedUnionIdSource" not in rows["unknown"]
    assert {method for method, _payload in identity_cli.calls} == {
        "identity.bindings.list"
    }

    human = CliRunner().invoke(_cli_root_app, ["user", "bindings", "audit"])
    assert human.exit_code == 0, human.stderr
    assert "confirmed elsewhere: 1" in human.stdout
    assert "open-id-only (grandfathered inbound): 1" in human.stdout
    assert "unknown until this person messages the adapter; see user whois" in human.stdout


def test_bindings_audit_marks_open_id_only_account_on_another_user_conflicting(
    identity_cli: FakeIdentityDaemon,
) -> None:
    identity_cli.bindings = [
        _legacy_open_binding("legacy-owner", "ou_shared"),
        _binding(
            "different-owner",
            "org-directory",
            union_id="on_different",
            open_id="ou_shared",
        ),
    ]

    code, result = _run_json("bindings", "audit")

    assert code == 0
    assert result["rows"][0]["category"] == "conflicting"
    assert result["rows"][0]["audit"] == "conflicting"
    assert result["rows"][0]["suggestedCommand"] is None


@pytest.mark.parametrize(
    ("command", "method", "params"),
    [
        (("withdraw", "--org", "group-a"), "identity.binding.withdraw", {"org": "group-a"}),
        (("publish", "--all"), "identity.binding.publish", {"all": True}),
    ],
)
def test_binding_publication_commands_use_the_exact_daemon_contract(
    identity_cli: FakeIdentityDaemon,
    command: tuple[str, ...],
    method: str,
    params: dict[str, object],
) -> None:
    code, result = _run_json("bindings", *command)

    assert code == 0
    assert result == {"ok": True, "orgs": ["group-a"], "operatorVerified": False}
    assert identity_cli.calls[-1] == (method, params)


@pytest.mark.parametrize(
    "arguments",
    [
        ("withdraw",),
        ("publish",),
        ("withdraw", "--org", "group-a", "--all"),
        ("publish", "--org", "group-a", "--all"),
    ],
)
def test_binding_publication_commands_require_exactly_one_scope(
    identity_cli: FakeIdentityDaemon, arguments: tuple[str, ...]
) -> None:
    code, result = _run_json("bindings", *arguments)

    assert code == 1
    assert result["code"] == "INVALID_ARGUMENT"
    assert "exactly one" in result["error"]
    assert identity_cli.calls == []


@pytest.mark.parametrize("command", ["withdraw", "publish"])
def test_binding_publication_help_states_its_actual_scope(
    identity_cli: FakeIdentityDaemon, command: str
) -> None:
    result = CliRunner().invoke(_cli_root_app, ["user", "bindings", command, "--help"])

    assert result.exit_code == 0, result.stderr
    text = " ".join(result.stdout.split())
    assert "applies only to your own binding proof (the current node owner)" in text
    # M3: the help must not promise more refusal than the daemon performs.
    assert "agent sessions are refused" not in text
    assert "codex app-server session" in text
    assert "an agent's shell running this CLI" in text
    assert "unverified operator" in text
    assert identity_cli.calls == []


@pytest.mark.parametrize(
    ("command", "method"),
    [
        ("withdraw", "identity.binding.withdraw"),
        ("publish", "identity.binding.publish"),
    ],
)
def test_binding_publication_agent_refusal_shows_code_verbatim(
    identity_cli: FakeIdentityDaemon, command: str, method: str
) -> None:
    identity_cli.failures[method] = "CALLER_NOT_AUTHORIZED"
    identity_cli.failure_details[method] = (
        "identity binding publication is available only to the local operator"
    )

    result = CliRunner().invoke(
        _cli_root_app, ["user", "bindings", command, "--org", "group-a"]
    )

    assert result.exit_code == 1
    assert "CALLER_NOT_AUTHORIZED" in result.stderr


@pytest.mark.parametrize(
    ("code", "command", "method"),
    [
        ("IDENTITY_UNBOUND", ("whois", "cli-developer", "ou_missing"), "identity.resolve"),
        ("IDENTITY_CONFLICT", ("whois", "--union-id", "on_conflict"), "identity.resolve"),
        (
            "IDENTITY_OVERRIDE_FORBIDDEN",
            (
                "bind",
                "owner",
                "--adapter",
                "cli-developer",
                "--open-id",
                "ou_unknown",
                "--confirmed-by",
                "allen",
            ),
            "identity.override.set",
        ),
        (
            "IDENTITY_SOURCE_UNAVAILABLE",
            ("list", "--source", "org-directory"),
            "identity.bindings.list",
        ),
    ],
)
def test_identity_errors_exit_nonzero_with_the_frozen_code(
    identity_cli: FakeIdentityDaemon,
    code: str,
    command: tuple[str, ...],
    method: str,
) -> None:
    identity_cli.failures[method] = code

    exit_code, result = _run_json(*command)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["code"] == code
    if code == "IDENTITY_CONFLICT":
        assert "user" not in result
        human = CliRunner().invoke(_cli_root_app, ["user", *command])
        assert human.exit_code == 1
        assert "conflict" in human.stderr.lower()
        assert "owner" not in human.stderr


def test_whois_preserves_daemon_conflict_detail_in_json_and_human_output(
    identity_cli: FakeIdentityDaemon,
) -> None:
    detail = "account ou_shared resolves to alice and bob; no user selected"
    identity_cli.failures["identity.resolve"] = "IDENTITY_CONFLICT"
    identity_cli.failure_details["identity.resolve"] = detail

    code, result = _run_json("whois", "cli-developer", "ou_shared")

    assert code == 1
    assert result == {
        "ok": False,
        "code": "IDENTITY_CONFLICT",
        "error": detail,
    }
    human = CliRunner().invoke(
        _cli_root_app, ["user", "whois", "cli-developer", "ou_shared"]
    )
    assert human.exit_code == 1
    assert detail in human.stderr


def test_user_list_help_names_the_cli_json_rows_envelope(
    identity_cli: FakeIdentityDaemon,
) -> None:
    result = CliRunner().invoke(_cli_root_app, ["user", "list", "--help"])

    assert result.exit_code == 0, result.stderr
    assert '{"rows": [...]}' in result.stdout
    assert identity_cli.calls == []


@pytest.mark.parametrize(
    ("command", "method"),
    [
        (
            ("bind", "owner", "--union-id", "on_owner", "--confirmed-by", "allen"),
            "identity.override.set",
        ),
        (("unbind", "--union-id", "on_owner"), "identity.override.clear"),
        (
            ("add", "--kind", "guest", "--display-name", "V", "--confirmed-by", "allen"),
            "identity.users.add",
        ),
    ],
)
def test_identity_override_forbidden_is_a_clear_permission_error(
    identity_cli: FakeIdentityDaemon,
    command: tuple[str, ...],
    method: str,
) -> None:
    identity_cli.failures[method] = "IDENTITY_OVERRIDE_FORBIDDEN"

    exit_code, result = _run_json(*command)

    assert exit_code == 1
    assert result == {
        "ok": False,
        "code": "IDENTITY_OVERRIDE_FORBIDDEN",
        "error": "identity override forbidden",
    }
    human = CliRunner().invoke(_cli_root_app, ["user", *command])
    assert human.exit_code == 1
    assert "permission denied" in human.stderr.lower()


def test_outputs_do_not_leak_identity_keys_beyond_each_commands_contract(
    identity_cli: FakeIdentityDaemon,
) -> None:
    whois = CliRunner().invoke(
        _cli_root_app, ["user", "whois", "cli-developer", "ou_secret"]
    )
    added = CliRunner().invoke(
        _cli_root_app,
        ["user", "add", "--kind", "guest", "--display-name", "Visitor", "--confirmed-by", "allen"],
    )
    shown = CliRunner().invoke(_cli_root_app, ["user", "show", "owner"])

    for result in (whois, added, shown):
        assert result.exit_code == 0, result.stderr
        assert "ou_secret" not in result.stdout
        assert "ou_owner" not in result.stdout
        assert "on_owner" not in result.stdout
    listed = CliRunner().invoke(_cli_root_app, ["user", "list"])
    assert "on_owner" in listed.stdout
    assert "ou_owner" in listed.stdout


def test_outbound_only_flag_marks_account_only_without_union_id_key() -> None:
    # Design §5 @ 7db7e5c6: resolve returns {user, verified:false,
    # outboundOnly:true} for an account-only override, with no unionId key.
    from hyprial.shell.impl.cli.commands.user import commands as users

    resolved = {
        "user": "owner",
        "kind": "member",
        "source": "local-override",
        "verified": False,
        "outboundOnly": True,
    }
    rendered = users._render_whois(resolved)
    assert "account-only (outbound only)" in rendered
    assert "verified: false" in rendered
    assert users._account_only({"user": "owner", "unionId": "on_x", "verified": True}) is False


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--kind", "member", "--confirmed-by", "allen"), "--owner"),
        (("--kind", "guest", "--confirmed-by", "allen"), "--display-name"),
    ],
)
def test_add_requires_the_kinds_key_anchor_and_never_sends_a_key(
    identity_cli: FakeIdentityDaemon, arguments: tuple[str, ...], message: str
) -> None:
    # Design §5 round 4: members are keyed by slug(owner), guests randomly.
    exit_code, result = _run_json("add", *arguments)
    assert exit_code == 1
    assert result["code"] == "INVALID_ARGUMENT"
    assert message in result["error"]
    assert not [call for call in identity_cli.calls if call[0] == "identity.users.add"]


def test_list_shows_publication_suppression_and_who_set_it(
    identity_cli: FakeIdentityDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: the operator can see what is withheld and by whom, in JSON and text."""

    suppression = {
        "all": False,
        "except": [],
        "orgs": ["group-a"],
        "setBy": {
            "orgs": {
                "group-a": {
                    "operator": "unverified",
                    "operatorVerified": False,
                    "callerPid": 5151,
                }
            }
        },
    }
    original = identity_cli.__call__

    def with_suppression(method: str, params=None, **kwargs):
        result = original(method, params, **kwargs)
        if method == "identity.bindings.list":
            result = {**result, "publicationSuppression": suppression}
        return result

    monkeypatch.setattr(_cli_support, "_daemon_request", with_suppression)

    code, result = _run_json("list")
    assert code == 0
    assert result["publicationSuppression"] == suppression

    human = CliRunner().invoke(_cli_root_app, ["user", "list"])
    assert human.exit_code == 0, human.stderr
    assert (
        "publication withheld: group-a (set by unverified operator, pid 5151)"
        in human.stdout
    )
