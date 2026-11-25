"""``hyprial agent provider allow|revoke|status``: --actor maps to IPC ``agent``.

Contract §5 / §6: the IPC param is ``agent`` because an ``actor`` key makes the
router treat the caller as an agent; the CLI must never send ``actor``.
"""

from __future__ import annotations

import json

import pytest

from .conftest import KEY_ID, FakeDaemon, grant_view, invoke


def _answers(daemon: FakeDaemon, *, restart: str = "not-running") -> None:
    daemon.answers.update({
        "agent.provider.allow": {"grant": grant_view(), "changed": True},
        "agent.provider.revoke": {"grant": grant_view(state="revoked"), "changed": True,
                                  "restart": {"state": restart}},
        "agent.provider.status": {
            "agent": "00000000-0000-0000-0000-000000000001", "agentId": "a.aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "actor": "pi-a",
            "grants": [{"grantId": "openai", "provider": "openai", "state": "ok", "code": None,
                        "owner": "owner-sub", "keyId": KEY_ID, "seq": 1, "dekVersion": 1,
                        "expiresAt": 9, "unsealable": True},
                       {"grantId": "deepseek", "provider": "deepseek", "state": "revoked",
                        "code": "PROVIDER_GRANT_REVOKED", "owner": "owner-sub", "keyId": None,
                        "seq": 2, "dekVersion": None, "expiresAt": None, "unsealable": False}],
        },
    })


@pytest.mark.parametrize(("extra", "params"), [
    ([], {"agent": "pi-a", "provider": "openai"}),
    (["--key", KEY_ID], {"agent": "pi-a", "provider": "openai", "keyId": KEY_ID}),
    (["--env", "OPENAI_API_KEY", "--env", "OPENAI_KEY"],
     {"agent": "pi-a", "provider": "openai", "environmentNames": ["OPENAI_API_KEY", "OPENAI_KEY"]}),
    (["--replace"], {"agent": "pi-a", "provider": "openai", "replace": True}),
    (["--key", KEY_ID, "--env", "OPENAI_API_KEY", "--replace"],
     {"agent": "pi-a", "provider": "openai", "keyId": KEY_ID,
      "environmentNames": ["OPENAI_API_KEY"], "replace": True}),
])
def test_allow_sends_exact_params(daemon: FakeDaemon, extra: list[str], params: dict) -> None:
    _answers(daemon)

    result = invoke(["agent", "provider", "allow", "--actor", "pi-a", "--provider", "openai",
                     *extra, "--json"])

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("agent.provider.allow", params)]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["changed"] is True


def test_allow_human_view(daemon: FakeDaemon) -> None:
    _answers(daemon)

    result = invoke(["agent", "provider", "allow", "--actor", "pi-a", "--provider", "openai"])

    assert result.exit_code == 0, result.output
    assert "pi-a" in result.stdout and KEY_ID in result.stdout


def test_revoke_sends_exact_params(daemon: FakeDaemon) -> None:
    _answers(daemon)

    result = invoke(["agent", "provider", "revoke", "--actor", "pi-a", "--provider", "openai", "--json"])

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("agent.provider.revoke", {"agent": "pi-a", "provider": "openai"})]
    assert json.loads(result.stdout)["restart"] == {"state": "not-running"}
    assert result.stderr == ""


def test_revoke_warns_on_stderr_when_restart_is_not_supported(daemon: FakeDaemon) -> None:
    _answers(daemon, restart="not-supported")

    result = invoke(["agent", "provider", "revoke", "--actor", "pi-a", "--provider", "openai"])

    assert result.exit_code == 0, result.output
    assert "restart" in result.stderr and "pi-a" in result.stderr
    assert result.stdout.startswith("Revoked: pi-a openai revoked")


def test_revoke_without_running_agent_does_not_warn(daemon: FakeDaemon) -> None:
    _answers(daemon)

    result = invoke(["agent", "provider", "revoke", "--actor", "pi-a", "--provider", "openai"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""


@pytest.mark.parametrize(("extra", "params"), [
    ([], {"agent": "pi-a"}),
    (["--provider", "openai"], {"agent": "pi-a", "provider": "openai"}),
])
def test_status_sends_exact_params(daemon: FakeDaemon, extra: list[str], params: dict) -> None:
    _answers(daemon)

    result = invoke(["agent", "provider", "status", "--actor", "pi-a", *extra, "--json"])

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("agent.provider.status", params)]
    assert [grant["state"] for grant in json.loads(result.stdout)["grants"]] == ["ok", "revoked"]


def test_status_human_view_lists_every_slot(daemon: FakeDaemon) -> None:
    _answers(daemon)

    result = invoke(["agent", "provider", "status", "--actor", "pi-a"])

    assert result.exit_code == 0, result.output
    assert "openai" in result.stdout and "deepseek" in result.stdout
    assert "PROVIDER_GRANT_REVOKED" in result.stdout


@pytest.mark.parametrize("command", [
    ["allow", "--actor", "pi-a", "--provider", "openai", "--key", KEY_ID, "--env", "OPENAI_API_KEY",
     "--replace"],
    ["revoke", "--actor", "pi-a", "--provider", "openai"],
    ["status", "--actor", "pi-a", "--provider", "openai"],
])
def test_actor_option_is_sent_as_agent_and_never_as_actor(daemon: FakeDaemon, command: list[str]) -> None:
    _answers(daemon)

    result = invoke(["agent", "provider", *command, "--json"])

    assert result.exit_code == 0, result.output
    ((_method, params),) = daemon.calls
    assert params["agent"] == "pi-a"
    assert "actor" not in params and "sessionRef" not in params


@pytest.mark.parametrize("command", [
    ["allow", "--provider", "openai"],
    ["revoke", "--provider", "openai"],
    ["status"],
])
def test_actor_is_required(daemon: FakeDaemon, command: list[str]) -> None:
    result = invoke(["agent", "provider", *command])

    assert result.exit_code != 0
    assert daemon.calls == []
