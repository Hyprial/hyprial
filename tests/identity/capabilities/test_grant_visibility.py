"""AT09: see-actors / send-to decisions over the capability ledger."""

from __future__ import annotations

import json

import pytest

from hyprial.identity.impl.agents.runtime.visibility import (
    CallerIdentity,
    GrantRecord,
    GrantScopeError,
    SEE_ACTORS,
    SEND_TO,
    VisibilityError,
    explain_target,
    may_send,
    parse_scope,
    visible_targets,
)

VISITOR = "agent:guest@vip.example.com:devusr-dev:codex-sw"
SELF = VISITOR
OTHER = "agent:host@example.com:hq:worker-1"
OWNER = "user:guest@vip.example.com"


def _rows(*uris: str) -> list[dict[str, object]]:
    return [
        {"targetUri": uri, "targetKind": "agent", "status": "live"} for uri in uris
    ]


def test_host_local_operator_is_unrestricted() -> None:
    rows = _rows(SELF, OTHER, OWNER)
    assert visible_targets(rows) == rows
    assert may_send(OTHER).allowed is True


def test_hosted_caller_without_a_grant_sees_only_itself() -> None:
    caller = CallerIdentity(VISITOR, hosted=True)
    visible = visible_targets(_rows(SELF, OTHER, OWNER), caller=caller)

    assert [row["targetUri"] for row in visible] == [SELF]

    denied = may_send(OTHER, caller=caller)
    assert denied.allowed is False
    assert "only itself" in denied.reason


def test_an_unverified_caller_gets_nothing() -> None:
    caller = CallerIdentity(VISITOR, hosted=True, verified=False)

    decision = explain_target(SELF, capability=SEE_ACTORS, caller=caller)
    assert decision.allowed is False
    assert "not verified" in decision.reason
    assert may_send(SELF, caller=caller).allowed is False


def test_see_actors_grant_expands_the_visible_set() -> None:
    caller = CallerIdentity(VISITOR, hosted=True)
    grant = GrantRecord(
        actor=VISITOR, capability=SEE_ACTORS, scope=json.dumps([OTHER]), revision=2
    )

    visible = visible_targets(_rows(SELF, OTHER, OWNER), caller=caller, grants=[grant])
    assert [row["targetUri"] for row in visible] == [SELF, OTHER]
    assert may_send(OWNER, caller=caller, grants=[grant]).allowed is False


def test_send_to_is_separate_from_see_actors() -> None:
    caller = CallerIdentity(VISITOR, hosted=True)
    see = GrantRecord(actor=VISITOR, capability=SEE_ACTORS, scope=json.dumps([OTHER]))

    assert may_send(OTHER, caller=caller, grants=[see]).allowed is False

    send = GrantRecord(actor=VISITOR, capability=SEND_TO, scope=json.dumps([OWNER]))
    assert may_send(OWNER, caller=caller, grants=[see, send]).allowed is True


def test_another_actors_grant_does_not_leak() -> None:
    caller = CallerIdentity(VISITOR, hosted=True)
    someone_else = GrantRecord(
        actor=OTHER, capability=SEE_ACTORS, scope=json.dumps([OWNER])
    )

    decision = explain_target(OWNER, capability=SEE_ACTORS, caller=caller, grants=[someone_else])
    assert decision.allowed is False
    assert OTHER in decision.reason


def test_withdrawal_takes_effect_on_the_next_call() -> None:
    caller = CallerIdentity(VISITOR, hosted=True)
    grant = GrantRecord(actor=VISITOR, capability=SEND_TO, scope=json.dumps([OWNER]))

    assert may_send(OWNER, caller=caller, grants=[grant]).allowed is True
    # The ledger record is gone: the very next call must deny, with no cache.
    after = may_send(OWNER, caller=caller, grants=[])
    assert after.allowed is False
    assert "no send-to grant" in after.reason


def test_revision_is_reported_in_the_reason() -> None:
    caller = CallerIdentity(VISITOR, hosted=True)
    grant = GrantRecord(
        actor=VISITOR, capability=SEE_ACTORS, scope=json.dumps([OTHER]), revision=7
    )
    assert "revision 7" in explain_target(
        OTHER, capability=SEE_ACTORS, caller=caller, grants=[grant]
    ).reason


@pytest.mark.parametrize(
    "scope",
    ["", "not json", "{}", "[]", '[""]', "[1]", '[" padded "]'],
)
def test_invalid_scopes_are_refused(scope: str) -> None:
    with pytest.raises(GrantScopeError):
        parse_scope(SEE_ACTORS, scope)


def test_unknown_capability_is_refused() -> None:
    with pytest.raises(GrantScopeError):
        parse_scope("tool-surface", '["agent:x"]')
    with pytest.raises(GrantScopeError):
        GrantRecord(actor=VISITOR, capability="channel", scope='["x"]')


def test_visible_targets_requires_a_uri_key() -> None:
    with pytest.raises(VisibilityError):
        visible_targets([{"actor": SELF}])


def test_channel_routes_are_filtered_the_same_way() -> None:
    caller = CallerIdentity(VISITOR, hosted=True)
    rows = [
        {"targetUri": "route:lark:ops", "targetKind": "channel_route"},
        {"targetUri": SELF, "targetKind": "agent"},
    ]
    visible = visible_targets(rows, caller=caller)
    assert [row["targetUri"] for row in visible] == [SELF]


def test_decision_serialises_for_audit() -> None:
    caller = CallerIdentity(VISITOR, hosted=True)
    payload = may_send(OTHER, caller=caller).as_dict()
    assert payload["allowed"] is False
    assert payload["capability"] == SEND_TO
    assert payload["caller"] == VISITOR
    assert payload["target"] == OTHER


# -- production entry points -------------------------------------------------


def test_cli_visibility_audit() -> None:
    import json as _json

    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    runner = CliRunner()

    hosted = runner.invoke(
        cli.app,
        [
            "agent",
            "grant",
            "synthetic-caller",
            "visibility",
            "--caller",
            VISITOR,
            "--hosted",
            "--target",
            SELF,
            "--target",
            OTHER,
            "--json",
        ],
    )
    assert hosted.exit_code == 0, hosted.output
    body = _json.loads(hosted.stdout)
    assert body["visible"] == [SELF]
    decisions = {entry["target"]: entry for entry in body["decisions"]}
    assert decisions[OTHER]["seeActors"]["allowed"] is False
    assert decisions[OTHER]["sendTo"]["allowed"] is False
    assert decisions[SELF]["seeActors"]["allowed"] is True


def test_cli_visibility_audit_applies_grants_and_the_operator_exemption() -> None:
    import json as _json

    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    runner = CliRunner()

    granted = runner.invoke(
        cli.app,
        [
            "agent",
            "grant",
            "synthetic-caller",
            "visibility",
            "--caller",
            VISITOR,
            "--hosted",
            "--see-actors",
            _json.dumps([OTHER]),
            "--send-to",
            _json.dumps([OWNER]),
            "--target",
            SELF,
            "--target",
            OTHER,
            "--target",
            OWNER,
            "--json",
        ],
    )
    assert granted.exit_code == 0, granted.output
    body = _json.loads(granted.stdout)
    assert body["visible"] == [SELF, OTHER]
    decisions = {entry["target"]: entry for entry in body["decisions"]}
    assert decisions[OWNER]["seeActors"]["allowed"] is False
    assert decisions[OWNER]["sendTo"]["allowed"] is True

    operator = runner.invoke(
        cli.app,
        ["agent", "grant", "synthetic-caller", "visibility", "--target", OTHER, "--json"],
    )
    assert operator.exit_code == 0, operator.output
    assert _json.loads(operator.stdout)["visible"] == [OTHER]
