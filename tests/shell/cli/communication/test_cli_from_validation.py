"""``hyprial message send --from`` command-time identity validation.

The check asks the daemon (``agent.resolve``, the same pipeline as
``message.send``) and fails open on any uncertainty: a validator that
blocks a legitimate send is worse than no validator.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.support import CliError
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.commands.message.send as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _request_recorder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolve_answer: dict[str, Any] | None = None,
    resolve_error: CliError | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(
        method: str, params: dict[str, Any], timeout: float = 15.0
    ) -> dict[str, Any]:
        calls.append((method, params))
        if method == "agent.resolve":
            if resolve_error is not None:
                raise resolve_error
            assert resolve_answer is not None
            return resolve_answer
        return {"ok": True, "deliveries": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return calls


def _invoke_send(source: str) -> Any:
    return CliRunner().invoke(
        _cli_root_app,
        [
            "message",
            "send",
            "--from",
            source,
            "--to",
            "node-2",
            "--summary",
            "s",
            "hello",
            "--json",
        ],
    )


def test_resolvable_bare_from_reports_daemon_canonical_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((method, params))
        if method == "agent.resolve":
            return {
                "ok": True,
                "input": "hq-adjutant",
                "resolved": "agent:bobadmin:hq:hq-adjutant",
                "known": True,
                "reason": "alias",
            }
        return {
            "ok": True,
            "deliveries": [],
            "actingPrincipal": "agent:bobadmin:hq:hq-adjutant",
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = _invoke_send("hq-adjutant")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in calls] == ["agent.resolve", "message.send"]
    assert json.loads(result.stdout)["actingPrincipal"] == (
        "agent:bobadmin:hq:hq-adjutant"
    )


def test_unknown_bare_from_fails_at_command_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _request_recorder(
        monkeypatch,
        resolve_answer={
            "ok": True,
            "input": "hq-adjutnat",
            "resolved": "hq-adjutnat",
            "known": False,
            "reason": "unknown",
        },
    )

    result = _invoke_send("hq-adjutnat")

    assert result.exit_code == 2, result.output
    assert "--from" in result.output
    assert "hq-adjutnat" in result.output
    assert "agent:<owner>:<node>:<actor>" in result.output
    # Nothing was sent: the typo never left the CLI.
    assert [method for method, _ in calls] == ["agent.resolve"]


def test_daemon_unavailable_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _request_recorder(
        monkeypatch,
        resolve_error=CliError("DAEMON_UNAVAILABLE", "not running"),
    )

    result = _invoke_send("whatever-name")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in calls] == ["agent.resolve", "message.send"]


def test_resolve_timeout_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _request_recorder(
        monkeypatch,
        resolve_error=CliError("IPC_TIMEOUT", "timed out"),
    )

    result = _invoke_send("whatever-name")

    assert result.exit_code == 0, result.output


def test_malformed_resolve_answer_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request_recorder(monkeypatch, resolve_answer={"ok": True})

    result = _invoke_send("whatever-name")

    assert result.exit_code == 0, result.output


def test_ambiguous_from_is_surfaced_not_silenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _request_recorder(
        monkeypatch,
        resolve_error=CliError(
            "AMBIGUOUS_TARGET",
            "actor name 'hq-adjutant' matches more than one agent; "
            "address the canonical URI directly",
        ),
    )

    result = _invoke_send("hq-adjutant")

    assert result.exit_code == 2, result.output
    assert "--from" in result.output
    assert "matches more than one" in result.output
    assert "canonical URI directly" in result.output
    assert [method for method, _ in calls] == ["agent.resolve"]


def test_scheme_from_skips_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _request_recorder(monkeypatch)

    result = _invoke_send("agent:alice:mac:worker")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in calls] == ["message.send"]


def test_resilient_parsing_never_touches_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("daemon must not be queried during completion")

    monkeypatch.setattr(_cli_support, "_daemon_request", forbidden)
    ctx = SimpleNamespace(resilient_parsing=True)

    assert _cli_owner_1._validate_from_identity(ctx, None, "hq-adjutant") == "hq-adjutant"


@pytest.mark.parametrize(
    ("command", "method"),
    [
        (["message", "send", "--to", "agent:op:node-1:reader", "--summary", "s", "hello"], "message.send"),
        (["message", "reply", "message-1", "--summary", "s", "done"], "message.reply"),
        (["message", "ack", "message-1"], "message.ack"),
    ],
)
def test_worker_cli_message_commands_forward_only_their_own_session(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    method: str,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:op:node-1:worker")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session-worker")
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda called, params, **_kwargs: calls.append((called, params))
        or {
            "ok": True,
            "deliveries": [],
            "actingPrincipal": "agent:op:node-1:worker",
        },
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [*command, "--from", "worker", "--json"],
    )

    assert result.exit_code == 0, result.output
    sent = next(params for called, params in calls if called == method)
    assert sent["sessionRef"] == "session-worker"
    if method == "message.send":
        assert json.loads(result.stdout)["actingPrincipal"] == "agent:op:node-1:worker"


def test_interactive_carrier_file_is_forwarded_by_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    binding_file = tmp_path / "cli-principal.json"
    binding_file.write_text("session-interactive", encoding="utf-8")
    binding_file.chmod(0o600)
    monkeypatch.setenv("HYPRIAL_AGENT_SESSION", "1")
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:op:node-1:interactive")
    monkeypatch.delenv("HYPRIAL_WORKER_SESSION_REF", raising=False)
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF_FILE", str(binding_file))
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda called, params, **_kwargs: calls.append((called, params))
        or {
            "ok": True,
            "deliveries": [],
            "actingPrincipal": "agent:op:node-1:interactive",
        },
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "message",
            "send",
            "--to",
            "agent:op:node-1:reader",
            "--summary",
            "s",
            "hello",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    sent = next(params for called, params in calls if called == "message.send")
    assert sent["from"] == "agent:op:node-1:interactive"
    assert sent["sessionRef"] == "session-interactive"
    assert json.loads(result.stdout)["actingPrincipal"] == "agent:op:node-1:interactive"


def test_worker_session_rejects_a_different_from_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:op:node-1:worker")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session-worker")
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda called, params, **_kwargs: calls.append((called, params))
        or {"ok": True, "deliveries": []},
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "message",
            "send",
            "--from",
            "agent:op:node-1:someone-else",
            "--to",
            "agent:op:node-1:reader",
            "--summary",
            "s",
            "hello",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "PAC_PRINCIPAL_UNVERIFIED"
    assert calls == []


def test_operator_on_behalf_flags_are_forwarded_visibly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda called, params, **_kwargs: calls.append((called, params))
        or {"ok": True, "deliveries": []},
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "message",
            "send",
            "--from",
            "user:op",
            "--on-behalf-of",
            "agent:op:node-1:worker",
            "--to",
            "agent:op:node-1:reader",
            "--summary",
            "hello",
            "hello",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "message.send",
            {
                "from": "user:op",
                "onBehalfOf": "agent:op:node-1:worker",
                "to": ["agent:op:node-1:reader"],
                "message": "hello",
                "summary": "hello",
            },
        )
    ]


def test_probe_delivery_paths_records_three_tagged_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """The explicit probe writes the same families doctor reads."""

    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((method, params))
        return {
            "ok": True,
            "deliveries": [
                {
                    "target": params["to"][0],
                    "messageId": f"probe-{len(calls)}",
                    "accepted": True,
                    "queued": False,
                }
            ],
        }

    from hyprial.shell.impl.cli.commands.common.services import get_services

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(get_services(), "_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(_cli_owner_1, "_local_operator_identity", lambda: "user:owner")
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "message",
            "probe-delivery-paths",
            "--from",
            "agent:owner:node:probe",
            "--route",
            "route:adapter:target",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["tag"] == "hyprial-probe"
    assert payload["per_family"]["agent"]["accepted"] is True
    assert payload["per_family"]["user"]["accepted"] is True
    assert payload["per_family"]["route"]["accepted"] is True
    probe = tmp_path / "state" / "delivery-paths-probe.json"
    written = json.loads(probe.read_text())
    assert written["per_family"]["route"]["target"] == "route:adapter:target"
    assert written["per_family"]["user"]["target"] == "user:owner"
    assert written["per_family"]["agent"]["target"] == "agent:owner:node:probe"
    assert len(calls) == 3
