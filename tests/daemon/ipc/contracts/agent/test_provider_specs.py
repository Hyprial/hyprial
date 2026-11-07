"""M-02: the provider declarations keep the existing handler and write profile.

The declarations are exercised through a registry assembled from the provider
binding tuple only, so this file does not depend on the shared Agent registry
aggregation owned by the read-declaration slice.  The real application fixture
is the same one the provider handler tests use.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from hyprial.daemon.impl.request_catalog.agent.provider import (
    AGENT_PROVIDER_BINDINGS,
    AGENT_PROVIDER_DEFAULT_SPEC,
    AGENT_PROVIDER_LOGIN_SPEC,
    AGENT_PROVIDER_LOGOUT_SPEC,
    AGENT_PROVIDER_SPECS,
)
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY, MethodRegistry
from hyprial.identity import AgentError
from hyprial.kernel import DaemonRequestError, export_openapi, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.runtime import entry as cli
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the CLI composition)
from tests.daemon.harnesses.providers.test_daemon_provider_login import (
    ACTOR,
    PAYLOAD,
    _files,
    _target,
    provider_app as provider_app,
)
from tests.support.runtime._owned_applications import owned_applications as owned_applications

NAMES = (
    "agent.provider.default",
    "agent.provider.login",
    "agent.provider.logout",
)
TOKEN = "synthetic-setup-token-not-a-real-credential"


def _accepts(schema: Any, value: Any) -> bool:
    return list(Draft202012Validator(schema).iter_errors(value)) == []


def _rejects(schema: Any, value: Any) -> bool:
    return not _accepts(schema, value)


def _fresh_app(base: Path) -> Any:
    base.mkdir(parents=True, exist_ok=True)
    return provider_app.__wrapped__(base)


def _login_projection(result: Any) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "actor": result.get("actor"),
        "provider": result.get("provider"),
        "credentialSource": result.get("credentialSource"),
        "files": [
            {
                "bytes": item["bytes"],
                "replaced": item["replaced"],
                "unchanged": item["unchanged"],
            }
            for item in result.get("files", [])
        ],
        "default": result.get("default"),
    }


def _default_projection(result: Any) -> dict[str, Any]:
    projected = dict(result)
    reference = projected.get("defaultCredential")
    if isinstance(reference, dict):
        projected["defaultCredential"] = {
            key: value
            for key, value in reference.items()
            if key != "ownerEntityToken"
        }
    return projected


def _login_outcome(call: Any) -> Any:
    try:
        return _login_projection(call())
    except DaemonRequestError as error:
        return ("error", error.code)


@pytest.fixture
def provider_registry() -> MethodRegistry:
    return MethodRegistry.from_bindings(AGENT_PROVIDER_BINDINGS)


def test_the_three_provider_methods_are_declared_and_bound_one_to_one() -> None:
    assert tuple(spec.name for spec in AGENT_PROVIDER_SPECS) == NAMES
    registry = MethodRegistry.from_bindings(AGENT_PROVIDER_BINDINGS)
    assert registry.names == tuple(sorted(NAMES))

    for spec in AGENT_PROVIDER_SPECS:
        assert spec.owner == "daemon.agents"
        assert spec.mutates is True
        assert spec.retry == "never"
        assert spec.audience == ("local-operator", "bound-agent")
        assert spec.http_exposed is (spec.name == "agent.provider.default")
        assert spec.confirmation == "none"
        assert spec.idempotency is not None
        assert spec.idempotency.mode == "never"
        assert spec.errors


def test_the_declared_errors_cover_the_provider_surface() -> None:
    common = {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.TARGET_NOT_AUTHORIZED,
        ipc_errors.AGENT_NOT_FOUND,
        AgentError.code,
        ipc_errors.DAEMON_ERROR,
    }
    assert {error.code for error in AGENT_PROVIDER_DEFAULT_SPEC.errors} == (
        common | {ipc_errors.PROVIDER_UNSUPPORTED}
    )
    assert {error.code for error in AGENT_PROVIDER_LOGIN_SPEC.errors} == (
        common
        | {
            ipc_errors.PROVIDER_UNSUPPORTED,
            ipc_errors.PROVIDER_CREDENTIAL_EXISTS,
            ipc_errors.PROVIDER_UNSAFE_PATH,
            ipc_errors.UNSUPPORTED,
            "STALE_AGENT_SECRET_GRANT",
        }
    )
    assert {error.code for error in AGENT_PROVIDER_LOGOUT_SPEC.errors} == (
        common
        | {
            ipc_errors.PROVIDER_UNSUPPORTED,
            ipc_errors.PROVIDER_UNSAFE_PATH,
            ipc_errors.UNSUPPORTED,
        }
    )


def test_http_projection_takes_the_write_profile_not_a_read_only_disguise() -> None:
    document = export_openapi(AGENT_PROVIDER_SPECS)
    assert set(document["paths"]) == {"/daemon/request/agent.provider.default"}
    operation = document["paths"]["/daemon/request/agent.provider.default"]["post"]
    assert operation["x-hyprial-mutates"] is False
    assert operation["x-hyprial-retry"] == "safe"
    assert all(entry["mutates"] for entry in document["x-hyprial-methods"])

    assert sorted(
        entry["name"] for entry in document["x-hyprial-methods"]
    ) == list(NAMES)


def test_default_params_keep_the_legacy_value_domain() -> None:
    schema = AGENT_PROVIDER_DEFAULT_SPEC.params_schema
    for params in (
        {},
        {"clear": True},
        {"clear": False},
        {"provider": "claude"},
        {"provider": "codex", "clear": False},
        {"provider": None},
        {"clear": None},
        {"clear": "yes"},
        {"clear": 1},
        {"provider": None, "clear": "yes", "unknown": True},
        {"provider": ""},
        {"provider": 3},
        {"unknown": True},
    ):
        assert _accepts(schema, params), params
    assert _rejects(schema, [])


def test_login_params_keep_the_legacy_value_domain() -> None:
    schema = AGENT_PROVIDER_LOGIN_SPEC.params_schema
    native = {
        "actor": ACTOR,
        "provider": "claude",
        "files": [
            {
                "filename": ".credentials.json",
                "contentBase64": base64.b64encode(PAYLOAD).decode("ascii"),
            }
        ],
        "force": False,
    }
    setup_token = {
        "actor": ACTOR,
        "provider": "claude",
        "setupToken": TOKEN,
        "makeDefault": True,
    }
    for params in (
        native,
        setup_token,
        {**native, "force": "yes"},
        {**native, "force": None},
        {**native, "unknown": True},
        {
            "actor": ACTOR,
            "provider": "claude",
            "files": [{"filename": "x", "extra": 1}],
        },
        {"provider": "claude", "files": native["files"]},
        {"actor": ACTOR, "files": native["files"]},
        {"actor": ACTOR, "provider": "claude", "makeDefault": "yes"},
        {"actor": ACTOR, "provider": "claude", "files": []},
    ):
        assert _accepts(schema, params), params
    assert _rejects(schema, [])


def test_logout_params_name_the_target_and_ignore_unknown_keys() -> None:
    schema = AGENT_PROVIDER_LOGOUT_SPEC.params_schema
    assert _accepts(schema, {"actor": ACTOR, "provider": "claude"})
    assert _accepts(
        schema, {"actor": ACTOR, "provider": "claude", "unknown": True}
    )
    assert _accepts(schema, {"actor": ACTOR})
    for params in (
        {"provider": "claude"},
        {"actor": "", "provider": "claude"},
        {"actor": None, "provider": "claude"},
        {"actor": ACTOR, "provider": 5},
    ):
        assert _rejects(schema, params), params


def test_missing_actor_and_empty_files_keep_the_legacy_messages(
    provider_app: Any, provider_registry: MethodRegistry
) -> None:
    with pytest.raises(DaemonRequestError) as missing_actor:
        provider_registry.invoke(
            provider_app, "agent.provider.login", {"provider": "claude"}
        )
    assert missing_actor.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(missing_actor.value) == "actor must be a non-empty string"

    with pytest.raises(DaemonRequestError) as empty_files:
        provider_registry.invoke(
            provider_app,
            "agent.provider.login",
            {"actor": ACTOR, "provider": "claude", "files": []},
        )
    assert empty_files.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(empty_files.value) == "files must be a non-empty array"


def test_default_legacy_value_domain_matches_the_old_handler(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    new_app = _fresh_app(tmp_path / "default-equivalence")
    for params in (
        {"provider": None},
        {"clear": None},
        {"clear": "yes"},
        {"unknown": True},
        {"provider": None, "clear": "yes", "unknown": True},
        {"clear": 1},
        {"clear": False},
        {"provider": "claude"},
        {"provider": None},
        {"provider": "codex", "clear": None},
        {"clear": True},
        {"provider": None},
    ):
        old = provider_app.handle("agent.provider.default", params)
        new = provider_registry.invoke(new_app, "agent.provider.default", params)
        assert old == new, params


def test_default_invalid_values_keep_the_old_error_order(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    new_app = _fresh_app(tmp_path / "default-errors")
    for params in (
        {"provider": ""},
        {"provider": 3},
        {"provider": "claude", "clear": True},
        {"provider": "not-a-harness"},
    ):
        with pytest.raises(DaemonRequestError) as old_error:
            provider_app.handle("agent.provider.default", params)
        with pytest.raises(DaemonRequestError) as new_error:
            provider_registry.invoke(new_app, "agent.provider.default", params)
        assert old_error.value.code == new_error.value.code
        assert str(old_error.value) == str(new_error.value)

    for params in (
        {"provider": ""},
        {"provider": 3},
        {"provider": None, "clear": True},
    ):
        with pytest.raises(DaemonRequestError) as old_worker:
            provider_app.handle(
                "agent.provider.default", params, _trusted_message_origin="worker"
            )
        with pytest.raises(DaemonRequestError) as new_worker:
            provider_registry.invoke(
                new_app,
                "agent.provider.default",
                params,
                trusted_message_origin="worker",
            )
        assert old_worker.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
        assert new_worker.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_default_truthy_non_boolean_clear_with_captured_reference(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    seed = {
        "actor": ACTOR,
        "provider": "claude",
        "setupToken": TOKEN,
        "makeDefault": True,
    }
    for index, clear in enumerate(("yes", 1)):
        old_app = _fresh_app(tmp_path / f"captured-clear-old-{index}")
        new_app = _fresh_app(tmp_path / f"captured-clear-new-{index}")
        old_app.handle("agent.provider.login", seed)
        provider_registry.invoke(new_app, "agent.provider.login", seed)
        assert old_app.handle("agent.provider.default", {})[
            "credentialSource"
        ] == "setup-token"
        assert provider_registry.invoke(new_app, "agent.provider.default", {})[
            "credentialSource"
        ] == "setup-token"

        old = old_app.handle("agent.provider.default", {"clear": clear})
        new = provider_registry.invoke(
            new_app, "agent.provider.default", {"clear": clear}
        )
        assert old == new, clear
        assert old["previous"] == "claude"
        assert "credentialSource" not in old
        assert old_app._agent_registry.default_claude_credential() is None
        assert new_app._agent_registry.default_claude_credential() is None
        old_after = old_app.handle("agent.provider.default", {})
        new_after = provider_registry.invoke(
            new_app, "agent.provider.default", {}
        )
        assert old_after == new_after
        assert "defaultCredential" not in old_after

    for index, clear in enumerate((None, 0, False, "", [], {})):
        old_app = _fresh_app(tmp_path / f"captured-read-old-{index}")
        new_app = _fresh_app(tmp_path / f"captured-read-new-{index}")
        old_app.handle("agent.provider.login", seed)
        provider_registry.invoke(new_app, "agent.provider.login", seed)
        old = old_app.handle("agent.provider.default", {"clear": clear})
        new = provider_registry.invoke(
            new_app, "agent.provider.default", {"clear": clear}
        )
        assert _default_projection(old) == _default_projection(new), clear
        assert old["provider"] == "claude"
        assert old["credentialSource"] == "setup-token"
        assert old["defaultCredential"]["ownerEntityToken"]
        assert new["defaultCredential"]["ownerEntityToken"]
        assert old_app._agent_registry.default_claude_credential() is not None
        assert new_app._agent_registry.default_claude_credential() is not None

    old_app = _fresh_app(tmp_path / "captured-worker-old")
    new_app = _fresh_app(tmp_path / "captured-worker-new")
    old_app.handle("agent.provider.login", seed)
    provider_registry.invoke(new_app, "agent.provider.login", seed)
    with pytest.raises(DaemonRequestError) as old_error:
        old_app.handle(
            "agent.provider.default", {"clear": "yes"}, _trusted_message_origin="worker"
        )
    with pytest.raises(DaemonRequestError) as new_error:
        provider_registry.invoke(
            new_app,
            "agent.provider.default",
            {"clear": "yes"},
            trusted_message_origin="worker",
        )
    assert old_error.value.code == new_error.value.code
    assert old_error.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert old_app._agent_registry.default_claude_credential() is not None
    assert new_app._agent_registry.default_claude_credential() is not None


def test_default_truthy_non_boolean_clear_keeps_a_legacy_provider_file(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    new_app = _fresh_app(tmp_path / "legacy-truthy-clear")
    provider_app.handle("agent.provider.default", {"provider": "codex"})
    provider_registry.invoke(
        new_app, "agent.provider.default", {"provider": "codex"}
    )
    old = provider_app.handle("agent.provider.default", {"clear": "yes"})
    new = provider_registry.invoke(
        new_app, "agent.provider.default", {"clear": "yes"}
    )
    assert old == new == {"ok": True, "provider": "codex", "previous": None}
    assert (provider_app.state_dir / "provider-default.json").is_file()
    assert (new_app.state_dir / "provider-default.json").is_file()


def test_login_force_values_match_the_old_handler(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    for index, force in enumerate((True, False, "yes", "no", None, 1, 0, {}, [])):
        old_app = _fresh_app(tmp_path / f"force-old-{index}")
        new_app = _fresh_app(tmp_path / f"force-new-{index}")
        seed = {
            "actor": ACTOR,
            "provider": "claude",
            "files": _files((".credentials.json", PAYLOAD)),
        }
        old_app.handle("agent.provider.login", seed)
        provider_registry.invoke(new_app, "agent.provider.login", seed)
        params = {
            "actor": ACTOR,
            "provider": "claude",
            "files": _files((".credentials.json", b"second")),
            "force": force,
        }
        old = _login_outcome(
            lambda: old_app.handle("agent.provider.login", params)
        )
        new = _login_outcome(
            lambda: provider_registry.invoke(new_app, "agent.provider.login", params)
        )
        assert old == new, force


def test_login_combined_invalid_inputs_keep_the_old_error_order(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    new_app = _fresh_app(tmp_path / "login-errors")
    cases = (
        (
            {
                "actor": ACTOR,
                "provider": "claude",
                "setupToken": TOKEN,
                "makeDefault": "yes",
                "unknown": 1,
            },
            "worker",
        ),
        (
            {"provider": "claude", "setupToken": TOKEN, "makeDefault": "yes"},
            "worker",
        ),
        (
            {
                "actor": ACTOR,
                "provider": "claude",
                "files": [],
                "makeDefault": "yes",
            },
            None,
        ),
        (
            {
                "actor": "ghost",
                "provider": "claude",
                "files": [],
                "makeDefault": "yes",
            },
            None,
        ),
        ({"actor": "ghost", "provider": "claude", "files": []}, None),
        (
            {
                "actor": ACTOR,
                "provider": "claude",
                "files": [],
                "force": "yes",
            },
            None,
        ),
    )
    for params, origin in cases:
        with pytest.raises(DaemonRequestError) as old_error:
            provider_app.handle(
                "agent.provider.login", params, _trusted_message_origin=origin
            )
        with pytest.raises(DaemonRequestError) as new_error:
            provider_registry.invoke(
                new_app,
                "agent.provider.login",
                params,
                trusted_message_origin=origin,
            )
        assert old_error.value.code == new_error.value.code
        assert str(old_error.value) == str(new_error.value)


def test_login_ignores_unknown_keys_like_the_old_handler(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    new_app = _fresh_app(tmp_path / "login-unknown")
    params = {
        "actor": ACTOR,
        "provider": "claude",
        "files": [
            {
                "filename": ".credentials.json",
                "contentBase64": base64.b64encode(PAYLOAD).decode("ascii"),
                "unknownFileKey": "ignored",
            }
        ],
        "unknownTopLevel": {"nested": True},
        "force": "yes",
    }
    old = _login_projection(
        provider_app.handle("agent.provider.login", params)
    )
    new = _login_projection(
        provider_registry.invoke(new_app, "agent.provider.login", params)
    )
    assert old == new
    assert old["ok"] is True
    assert old["files"][0]["bytes"] == len(PAYLOAD)


def test_logout_unknown_keys_and_field_errors_match_the_old_handler(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    new_app = _fresh_app(tmp_path / "logout-equivalence")
    params = {"actor": ACTOR, "provider": "claude", "unknown": True}
    old = provider_app.handle("agent.provider.logout", params)
    new = provider_registry.invoke(new_app, "agent.provider.logout", params)
    assert old["ok"] == new["ok"] is True
    assert old["actor"] == new["actor"]
    assert old["provider"] == new["provider"]
    assert old["removed"] == new["removed"] == []
    assert [Path(path).name for path in old["absent"]] == [
        Path(path).name for path in new["absent"]
    ]

    for invalid in (
        {"provider": "claude"},
        {"actor": ACTOR},
        {"actor": "", "provider": "claude"},
    ):
        with pytest.raises(DaemonRequestError) as old_error:
            provider_app.handle("agent.provider.logout", invalid)
        with pytest.raises(DaemonRequestError) as new_error:
            provider_registry.invoke(new_app, "agent.provider.logout", invalid)
        assert old_error.value.code == new_error.value.code
        assert str(old_error.value) == str(new_error.value)


def test_logout_combined_invalid_fields_keep_the_old_error_order(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    new_app = _fresh_app(tmp_path / "logout-errors")
    for params in (
        {"actor": 5},
        {"actor": None},
        {"actor": "", "provider": None},
        {"actor": False, "provider": 5},
        {"provider": 5},
        {"actor": ACTOR},
        {"actor": 5, "provider": "claude"},
        {"actor": ACTOR, "provider": 5},
    ):
        with pytest.raises(DaemonRequestError) as old_error:
            provider_app.handle("agent.provider.logout", params)
        with pytest.raises(DaemonRequestError) as new_error:
            provider_registry.invoke(new_app, "agent.provider.logout", params)
        assert old_error.value.code == new_error.value.code, params
        assert str(old_error.value) == str(new_error.value), params


def test_native_login_binding_writes_through_the_old_handler(
    provider_app: Any, provider_registry: MethodRegistry
) -> None:
    result = provider_registry.invoke(
        provider_app,
        "agent.provider.login",
        {
            "actor": ACTOR,
            "provider": "claude",
            "files": _files((".credentials.json", PAYLOAD)),
            "force": False,
        },
    )

    target = _target(provider_app, "claude", ".credentials.json")
    assert result["ok"] is True
    assert result["actor"] == ACTOR
    assert result["provider"] == "claude"
    assert result["files"][0]["path"] == str(target)
    assert target.read_bytes() == PAYLOAD
    assert "test-only" not in json.dumps(result)


def test_setup_token_login_and_default_reference_match_the_declarations(
    provider_app: Any, provider_registry: MethodRegistry
) -> None:
    result = provider_registry.invoke(
        provider_app,
        "agent.provider.login",
        {
            "actor": ACTOR,
            "provider": "claude",
            "setupToken": TOKEN,
            "makeDefault": True,
        },
    )

    assert result["credentialSource"] == "setup-token"
    assert result["default"]["credentialSource"] == "setup-token"
    assert result["default"]["defaultCredential"]["ownerActor"] == ACTOR
    assert result["default"]["previous"] is None
    assert TOKEN not in json.dumps(result)

    shown = provider_registry.invoke(provider_app, "agent.provider.default", {})
    assert shown["provider"] == "claude"
    assert shown["credentialSource"] == "setup-token"
    assert shown["defaultCredential"]["ownerActor"] == ACTOR
    assert TOKEN not in json.dumps(shown)


def test_native_login_records_a_non_claude_default_without_a_reference(
    provider_app: Any, provider_registry: MethodRegistry
) -> None:
    result = provider_registry.invoke(
        provider_app,
        "agent.provider.login",
        {
            "actor": ACTOR,
            "provider": "codex",
            "files": _files(("auth.json", b'{"synthetic": true}\n')),
            "makeDefault": True,
        },
    )

    assert result["files"][0]["path"].endswith("secrets/native/codex/auth.json")
    assert result["default"] == {
        "ok": True,
        "provider": "codex",
        "previous": None,
    }
    assert (provider_app.state_dir / "provider-default.json").is_file()


def test_default_binding_reads_sets_replaces_and_clears(
    provider_app: Any, provider_registry: MethodRegistry
) -> None:
    assert provider_registry.invoke(provider_app, "agent.provider.default", {}) == {
        "ok": True,
        "provider": None,
        "previous": None,
    }

    first = provider_registry.invoke(
        provider_app, "agent.provider.default", {"provider": "claude"}
    )
    assert (first["provider"], first["previous"]) == ("claude", None)

    second = provider_registry.invoke(
        provider_app, "agent.provider.default", {"provider": "codex"}
    )
    assert (second["provider"], second["previous"]) == ("codex", "claude")

    shown = provider_registry.invoke(provider_app, "agent.provider.default", {})
    assert shown["provider"] == "codex"

    cleared = provider_registry.invoke(
        provider_app, "agent.provider.default", {"clear": True}
    )
    assert (cleared["provider"], cleared["previous"]) == (None, "codex")
    assert not (provider_app.state_dir / "provider-default.json").exists()


def test_default_keeps_its_existing_domain_refusals(
    provider_app: Any, provider_registry: MethodRegistry
) -> None:
    with pytest.raises(DaemonRequestError) as both:
        provider_registry.invoke(
            provider_app,
            "agent.provider.default",
            {"provider": "claude", "clear": True},
        )
    assert both.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(DaemonRequestError) as unsupported:
        provider_registry.invoke(
            provider_app, "agent.provider.default", {"provider": "not-a-harness"}
        )
    assert unsupported.value.code == ipc_errors.PROVIDER_UNSUPPORTED


def test_a_worker_cannot_write_the_default_but_can_read_it(
    provider_app: Any, provider_registry: MethodRegistry
) -> None:
    with pytest.raises(DaemonRequestError) as cleared:
        provider_registry.invoke(
            provider_app,
            "agent.provider.default",
            {"clear": True},
            trusted_message_origin="worker",
        )
    assert cleared.value.code == ipc_errors.CALLER_NOT_AUTHORIZED

    with pytest.raises(DaemonRequestError) as seeded:
        provider_registry.invoke(
            provider_app,
            "agent.provider.login",
            {
                "actor": ACTOR,
                "provider": "claude",
                "files": _files((".credentials.json", PAYLOAD)),
                "makeDefault": True,
            },
            trusted_message_origin="worker",
        )
    assert seeded.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert not _target(provider_app, "claude", ".credentials.json").exists()

    read = provider_registry.invoke(
        provider_app,
        "agent.provider.default",
        {},
        trusted_message_origin="worker",
    )
    assert read["provider"] is None


def test_unknown_and_hosted_targets_keep_the_registry_boundary(
    provider_app: Any, provider_registry: MethodRegistry, tmp_path: Path
) -> None:
    for method, params in (
        ("agent.provider.login", {"actor": "ghost", "provider": "claude", "files": _files((".credentials.json", PAYLOAD))}),
        ("agent.provider.logout", {"actor": "ghost", "provider": "claude"}),
    ):
        with pytest.raises(DaemonRequestError) as missing:
            provider_registry.invoke(provider_app, method, params)
        assert missing.value.code == ipc_errors.AGENT_NOT_FOUND

    provider_app.handle(
        "agent.host-invite",
        {"name": "guest", "owner": "visitor", "cwd": str(tmp_path / "guest")},
    )
    for method, params in (
        ("agent.provider.login", {"actor": "guest", "provider": "claude", "files": _files((".credentials.json", PAYLOAD))}),
        ("agent.provider.logout", {"actor": "guest", "provider": "claude"}),
    ):
        with pytest.raises(DaemonRequestError) as hosted:
            provider_registry.invoke(provider_app, method, params)
        assert hosted.value.code == ipc_errors.TARGET_NOT_AUTHORIZED


def test_logout_binding_removes_the_files_the_login_installed(
    provider_app: Any, provider_registry: MethodRegistry
) -> None:
    provider_registry.invoke(
        provider_app,
        "agent.provider.login",
        {
            "actor": ACTOR,
            "provider": "claude",
            "files": _files((".credentials.json", PAYLOAD)),
        },
    )
    target = _target(provider_app, "claude", ".credentials.json")

    result = provider_registry.invoke(
        provider_app,
        "agent.provider.logout",
        {"actor": ACTOR, "provider": "claude"},
    )

    assert result["ok"] is True
    assert result["actor"] == ACTOR
    assert result["provider"] == "claude"
    assert result["removed"] == [str(target)]
    assert result["absent"] == []
    assert not target.exists()


def test_other_agent_methods_still_reach_the_legacy_handler(provider_app: Any) -> None:
    result = provider_app.handle("agent.get", {"name": ACTOR})
    assert result["ok"] is True
    assert result["agent"]["actor"] == ACTOR


def test_cli_requests_and_outputs_are_unchanged_through_the_binding(
    provider_app: Any,
    provider_registry: MethodRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = tmp_path / "operator-home"
    (operator / ".claude").mkdir(parents=True)
    (operator / ".claude" / ".credentials.json").write_bytes(PAYLOAD)
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    monkeypatch.setattr(
        _cli_get_services(), "require_initialized_hyprial_home", lambda: None
    )
    calls: list[tuple[str, object]] = []

    def request(method: str, params: object = None, **_kwargs: object) -> object:
        calls.append((method, params))
        try:
            if method in provider_registry:
                return provider_registry.invoke(
                    provider_app, method, {} if params is None else params
                )
            return provider_app.handle(method, {} if params is None else params)
        except DaemonRequestError as error:
            raise _cli_support.CliError(error.code, str(error)) from error

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()

    seeded = runner.invoke(
        cli.app,
        [
            "agent",
            "provider",
            "login",
            "claude",
            "--actor",
            ACTOR,
            "--default",
            "--json",
        ],
    )
    assert seeded.exit_code == 0, seeded.output
    seeded_payload = json.loads(seeded.stdout)
    assert seeded_payload["default"]["provider"] == "claude"
    assert seeded_payload["revoke"] == (
        f"hyprial agent provider logout claude --actor {ACTOR}"
    )
    assert "test-only" not in seeded.stdout + seeded.stderr

    shown = runner.invoke(cli.app, ["agent", "provider", "default", "--json"])
    assert shown.exit_code == 0, shown.output
    shown_payload = json.loads(shown.stdout)
    assert shown_payload["provider"] == "claude"
    assert shown_payload["defaultCredential"]["ownerActor"] == ACTOR

    cleared = runner.invoke(
        cli.app, ["agent", "provider", "default", "--clear", "--json"]
    )
    assert cleared.exit_code == 0, cleared.output
    assert json.loads(cleared.stdout)["provider"] is None

    logged_out = runner.invoke(
        cli.app,
        ["agent", "provider", "logout", "claude", "--actor", ACTOR, "--json"],
    )
    assert logged_out.exit_code == 0, logged_out.output
    assert json.loads(logged_out.stdout)["removed"]
    assert calls == [
        ("agent.provider.login", {
            "actor": ACTOR,
            "provider": "claude",
            "files": [
                {
                    "filename": ".credentials.json",
                    "contentBase64": base64.b64encode(PAYLOAD).decode("ascii"),
                }
            ],
            "force": False,
            "makeDefault": True,
        }),
        ("agent.provider.default", {}),
        ("agent.provider.default", {"clear": True}),
        ("agent.provider.logout", {"actor": ACTOR, "provider": "claude"}),
    ]

    missing = runner.invoke(
        cli.app,
        ["agent", "provider", "login", "claude", "--actor", "ghost", "--json"],
    )
    assert missing.exit_code == 1, missing.output
    assert json.loads(missing.stdout)["code"] == ipc_errors.AGENT_NOT_FOUND


def test_openapi_projection_carries_no_example_secret_values() -> None:
    document = json.dumps(export_openapi(AGENT_PROVIDER_SPECS))
    assert "test-only" not in document
    assert TOKEN not in document
    assert "accessToken" not in document


def test_published_login_and_logout_success_params_replay_through_the_real_handler(
    provider_app: Any,
) -> None:
    from hyprial.daemon import daemon_openapi

    document = daemon_openapi()
    declarations = {entry["name"]: entry for entry in document["x-hyprial-methods"]}
    login_operation = declarations["agent.provider.login"]
    login = next(
        case
        for case in login_operation["x-hyprial-examples"]
        if case["kind"] == "success"
    )
    params = login["params"]
    assert params["files"][0]["filename"] == ".credentials.json"
    result = METHOD_REGISTRY.invoke(provider_app, "agent.provider.login", params)
    target = _target(provider_app, params["provider"], ".credentials.json")
    assert result["ok"] is True
    assert result["actor"] == params["actor"]
    assert result["provider"] == params["provider"]
    assert target.read_bytes() == base64.b64decode(
        params["files"][0]["contentBase64"]
    )

    logout_operation = declarations["agent.provider.logout"]
    logout = next(
        case
        for case in logout_operation["x-hyprial-examples"]
        if case["kind"] == "success"
    )
    removed = METHOD_REGISTRY.invoke(
        provider_app, "agent.provider.logout", logout["params"]
    )
    assert removed["ok"] is True
    assert removed["removed"] == [str(target)]
    assert not target.exists()
