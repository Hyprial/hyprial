"""AT10: frozen capabilities bound to their real entry points."""

from __future__ import annotations

import pytest

from hyprial.identity.impl.agents.runtime.enforcement import (
    CAPABILITY_ENTRY_POINTS,
    CapabilityRecord,
    EnforcementScopeError,
    Principal,
    check_channel,
    check_isolation,
    check_org_context,
    check_shared_path,
    check_tool,
    explain,
)

GUEST = "agent:guest@vip.example.com:devusr-dev:codex-sw"


def _record(capability: str, scope: str, *, revision: int = 1, actor: str = GUEST):
    return CapabilityRecord(
        actor=actor, capability=capability, scope=scope, revision=revision
    )


def test_every_grantable_capability_names_an_entry_point() -> None:
    for capability in ("tool-surface", "channel", "shared-path", "org-context", "isolation"):
        assert capability in CAPABILITY_ENTRY_POINTS
        assert CAPABILITY_ENTRY_POINTS[capability]


def test_host_local_operator_is_allowed_everywhere() -> None:
    assert check_tool("shell").allowed is True
    assert check_channel("lark:ops").allowed is True
    assert check_shared_path("/tmp/x", write=True).allowed is True
    assert check_org_context().allowed is True
    assert check_isolation("container").allowed is True


def test_unverified_identity_is_refused_everywhere() -> None:
    guest = Principal(GUEST, hosted=True, verified=False)
    for verdict in (
        check_tool("shell", caller=guest, records=[_record("tool-surface", '["shell"]')]),
        check_channel("lark:ops", caller=guest),
        check_shared_path("/tmp/x", caller=guest),
        check_org_context(caller=guest),
        check_isolation("container", caller=guest),
    ):
        assert verdict.allowed is False
        assert "not verified" in verdict.reason


def test_no_grant_denies_and_names_the_entry_point() -> None:
    guest = Principal(GUEST, hosted=True)
    verdict = check_tool("shell", caller=guest)

    assert verdict.allowed is False
    assert verdict.entry_point == "harness tool broker: the agent's tool allow-list"
    assert "no live tool-surface grant" in verdict.reason


def test_tool_surface_grant_is_a_named_allow_list() -> None:
    guest = Principal(GUEST, hosted=True)
    records = [_record("tool-surface", '["shell", "read"]')]

    assert check_tool("shell", caller=guest, records=records).allowed is True
    denied = check_tool("write", caller=guest, records=records)
    assert denied.allowed is False
    assert "but none covers write" in denied.reason


def test_channel_grant_names_routes() -> None:
    guest = Principal(GUEST, hosted=True)
    records = [_record("channel", '["lark:ops"]')]

    assert check_channel("lark:ops", caller=guest, records=records).allowed is True
    assert check_channel("lark:sales", caller=guest, records=records).allowed is False


def test_shared_path_read_and_write_need_different_modes() -> None:
    guest = Principal(GUEST, hosted=True)
    ro = [_record("shared-path", '{"path": "/srv/shared", "mode": "ro"}')]

    assert check_shared_path("/srv/shared/doc.md", caller=guest, records=ro).allowed is True
    assert check_shared_path("/srv/shared", caller=guest, records=ro).allowed is True
    write_with_ro = check_shared_path("/srv/shared/doc.md", write=True, caller=guest, records=ro)
    assert write_with_ro.allowed is False

    rw = [_record("shared-path", '{"path": "/srv/shared", "mode": "rw"}')]
    assert check_shared_path("/srv/shared/doc.md", write=True, caller=guest, records=rw).allowed is True
    assert check_shared_path("/srv/other/doc.md", caller=guest, records=rw).allowed is False


def test_shared_path_scope_must_be_absolute_and_dotdot_free() -> None:
    assert _record("shared-path", '{"path": "/srv/a", "mode": "ro"}')
    with pytest.raises(EnforcementScopeError):
        check_shared_path("/srv/a", caller=Principal(GUEST), records=[
            _record("shared-path", '{"path": "/srv/../etc", "mode": "ro"}')
        ])
    with pytest.raises(EnforcementScopeError):
        check_shared_path("/srv/a", caller=Principal(GUEST), records=[
            _record("shared-path", '{"path": "/srv/a", "mode": "rwx"}')
        ])


def test_org_context_scope_is_the_accepted_document_only() -> None:
    guest = Principal(GUEST, hosted=True)
    assert check_org_context(caller=guest, records=[_record("org-context", "accepted")]).allowed is True

    with pytest.raises(EnforcementScopeError):
        check_org_context(caller=guest, records=[_record("org-context", "everything")])


def test_isolation_scope_is_directory_or_container() -> None:
    guest = Principal(GUEST, hosted=True)
    records = [_record("isolation", "container")]

    assert check_isolation("container", caller=guest, records=records).allowed is True
    assert check_isolation("directory", caller=guest, records=records).allowed is False

    with pytest.raises(EnforcementScopeError):
        check_isolation("vm", caller=guest, records=[_record("isolation", "vm")])


def test_withdrawal_takes_effect_on_the_next_call() -> None:
    guest = Principal(GUEST, hosted=True)
    records = [_record("tool-surface", '["shell"]', revision=3)]

    assert check_tool("shell", caller=guest, records=records).allowed is True
    after = check_tool("shell", caller=guest, records=[])
    assert after.allowed is False
    assert "no live tool-surface grant" in after.reason


def test_another_actors_record_never_grants() -> None:
    guest = Principal(GUEST, hosted=True)
    other = _record("tool-surface", '["shell"]', actor="agent:someone-else@vip")

    verdict = check_tool("shell", caller=guest, records=[other])
    assert verdict.allowed is False
    assert "no live tool-surface grant" in verdict.reason


def test_agent_home_is_inherent_but_bounded() -> None:
    guest = Principal(GUEST, hosted=True)

    assert explain("agent-home", GUEST, caller=guest).allowed is True
    assert explain("agent-home", f"{GUEST}/workspace", caller=guest).allowed is True
    assert explain("agent-home", "agent:other@vip", caller=guest).allowed is False
    with pytest.raises(EnforcementScopeError):
        _record("agent-home", "self")


def test_unknown_capability_is_refused() -> None:
    with pytest.raises(EnforcementScopeError):
        explain("teleport", "anywhere")
    with pytest.raises(EnforcementScopeError):
        _record("see-actors", '["agent:x"]')


def test_verdict_serialises_for_audit() -> None:
    guest = Principal(GUEST, hosted=True)
    payload = check_tool("shell", caller=guest).as_dict()
    assert payload["allowed"] is False
    assert payload["capability"] == "tool-surface"
    assert payload["entryPoint"].startswith("harness tool broker")
    assert payload["caller"] == GUEST
    assert payload["resource"] == "shell"


# -- production entry points -------------------------------------------------


def test_cli_grants_check_allowed_and_denied() -> None:
    import json

    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    runner = CliRunner()

    denied = runner.invoke(
        cli.app,
        [
            "agent",
            "grant",
            "synthetic-caller",
            "check",
            "--capability",
            "tool-surface",
            "--resource",
            "shell",
            "--caller",
            GUEST,
            "--hosted",
            "--json",
        ],
    )
    assert denied.exit_code == 0, denied.output
    body = json.loads(denied.stdout)
    assert body["allowed"] is False
    assert body["entryPoint"].startswith("harness tool broker")

    allowed = runner.invoke(
        cli.app,
        [
            "agent",
            "grant",
            "synthetic-caller",
            "check",
            "--capability",
            "tool-surface",
            "--resource",
            "shell",
            "--caller",
            GUEST,
            "--hosted",
            "--scope",
            '["shell"]',
            "--json",
        ],
    )
    assert allowed.exit_code == 0, allowed.output
    assert json.loads(allowed.stdout)["allowed"] is True


def test_cli_grants_check_shared_path_write_and_operator() -> None:
    import json

    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    runner = CliRunner()

    write_denied = runner.invoke(
        cli.app,
        [
            "agent",
            "grant",
            "synthetic-caller",
            "check",
            "--capability",
            "shared-path",
            "--resource",
            "/srv/shared/doc.md",
            "--write",
            "--caller",
            GUEST,
            "--scope",
            '{"path": "/srv/shared", "mode": "ro"}',
            "--json",
        ],
    )
    assert write_denied.exit_code == 0, write_denied.output
    assert json.loads(write_denied.stdout)["allowed"] is False

    operator = runner.invoke(
        cli.app,
        [
            "agent",
            "grant",
            "synthetic-caller",
            "check",
            "--capability",
            "isolation",
            "--resource",
            "container",
            "--json",
        ],
    )
    assert operator.exit_code == 0, operator.output
    assert json.loads(operator.stdout)["allowed"] is True


def test_shared_path_refuses_traversal_out_of_the_granted_root() -> None:
    guest = Principal(GUEST, hosted=True)
    rw = [_record("shared-path", '{"path": "/srv/shared", "mode": "rw"}')]

    assert check_shared_path("/srv/shared/doc.md", write=True, caller=guest, records=rw).allowed is True

    escaped = check_shared_path(
        "/srv/shared/../../etc/passwd", write=True, caller=guest, records=rw
    )
    assert escaped.allowed is False
    assert "absolute path with no .." in escaped.reason

    # A sibling whose name merely shares the prefix is not inside either.
    assert check_shared_path("/srv/shared-other/x", caller=guest, records=rw).allowed is False


def test_shared_path_scope_must_be_absolute() -> None:
    guest = Principal(GUEST, hosted=True)
    with pytest.raises(EnforcementScopeError) as caught:
        check_shared_path(
            "/srv/shared/x",
            caller=guest,
            records=[_record("shared-path", '{"path": "shared", "mode": "rw"}')],
        )
    assert "absolute" in str(caught.value)


def test_relative_resource_is_refused_not_resolved() -> None:
    guest = Principal(GUEST, hosted=True)
    rw = [_record("shared-path", '{"path": "/srv/shared", "mode": "rw"}')]

    verdict = check_shared_path("../etc/passwd", caller=guest, records=rw)
    assert verdict.allowed is False
    assert "absolute path with no .." in verdict.reason


def test_hosted_default_deny_says_so() -> None:
    guest = Principal(GUEST, hosted=True)
    verdict = check_tool("shell", caller=guest)
    assert "hosted visitor: default deny" in verdict.reason
