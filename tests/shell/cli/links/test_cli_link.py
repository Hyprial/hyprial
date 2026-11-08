"""WP6: the ``hyprial agent link`` command group reads and mutates through IPC only.

Each subcommand forwards exactly one method with the actor selector (and a
managed worker's session fence); the human renderers tolerate missing daemon
fields with the em dash placeholder, and a hold stays a successful queued
result rather than an error.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon import IPC_LINK_ADD, IPC_LINK_DROP, IPC_LINK_LIST, IPC_LINK_STATS
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SOURCE = "agent:op:node-1:alice"
PEER = "agent:op:node-1:bob"
OTHER = "agent:op:node-2:carol"
DASH = "\u2014"


def _record(
    monkeypatch: pytest.MonkeyPatch, *, answer: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append((method, params))
        return {"ok": True, "deliveries": []} if answer is None else answer

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return calls


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(_cli_root_app, ["agent", *args])


def _list_answer() -> dict[str, Any]:
    return {
        "ok": True,
        "actor": SOURCE,
        "k": 4,
        "W": 6,
        "inFlight": 3,
        "nowMs": 1_800_000_000_000,
        "owner": {"peer": "user:op"},
        "links": [
            {
                "peer": OTHER,
                "state": "pending",
                "lifecycle": "regular",
                "revision": 1,
            },
            {
                "peer": PEER,
                "state": "established",
                "cwnd": 2,
                "inFlight": 1,
                "lifecycle": "regular",
                "lastUsedAtMs": 1_800_000_000_000 - 180_000,
                "revision": 3,
            },
        ],
    }


def test_link_list_calls_the_query_method_with_the_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, answer=_list_answer())

    result = _invoke("link", "list", "--from", SOURCE, "--json")

    assert result.exit_code == 0, result.stdout
    assert calls == [(IPC_LINK_LIST, {"from": SOURCE})]
    payload = json.loads(result.stdout)
    assert payload["k"] == 4
    assert payload["links"][1]["peer"] == PEER


def test_link_list_human_render_shows_owner_window_and_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, answer=_list_answer())

    result = _invoke("link", "list", "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    assert "k 4" in lines[0]
    assert "W 6" in lines[0]
    assert "in-flight 3/6" in lines[0]
    assert "agent links 1/4" in lines[0]
    assert lines[1].startswith("PEER")
    assert "IN-FLIGHT" in lines[1]
    # The owner row is first (design §3.2); established sorts before pending.
    assert lines[2].startswith("user:op")
    assert "owner" in lines[2]
    peer_row = next(line for line in lines if PEER in line)
    pending_row = next(line for line in lines if OTHER in line)
    assert lines.index(peer_row) < lines.index(pending_row)
    assert "established" in peer_row
    assert "2" in peer_row
    assert "3m" in peer_row


def test_link_list_tolerates_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, answer={"ok": True})

    result = _invoke("link", "list", "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    assert DASH in result.stdout


def test_link_list_does_not_count_task_bound_edges_toward_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = _list_answer()
    answer["links"].append(
        {
            "peer": "agent:op:node-1:pac-worker",
            "state": "established",
            "lifecycle": "pac-task",
        }
    )
    _record(monkeypatch, answer=answer)

    result = _invoke("link", "list", "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    assert "agent links 1/4" in result.stdout.splitlines()[0]
    assert "1 task-bound" in result.stdout.splitlines()[0]


def test_link_stats_calls_the_stats_method_and_renders_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {
        "ok": True,
        "actor": SOURCE,
        "nActive": 23,
        "k": 4,
        "W": 6,
        "wTarget": 8,
        "tMinMs": 42_000,
        "tBarMs": 70_000,
        "muMax": 0.12,
        "diff": 2.4,
        "grad": -3_000,
        "links": [{"peer": PEER, "rttMinMs": 30_000, "rttMs": 45_000, "cwnd": 2, "inFlight": 1}],
    }
    calls = _record(monkeypatch, answer=answer)

    result = _invoke("link", "stats", "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    assert calls == [(IPC_LINK_STATS, {"from": SOURCE})]
    out = result.stdout
    assert "N_active 23" in out
    assert "W 6 (target 8)" in out
    assert "T_min 42s" in out
    # The shared ``_format_top_age`` duration format is deliberately coarse
    # (spec §1.1): 70000 ms renders as ``1m``.
    assert "T_bar 1m" in out
    assert "mu_max 0.12/s" in out
    assert "grad -3s" in out
    assert "RTT_MIN" in out


def test_link_stats_preserves_zero_and_negative_and_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {
        "ok": True,
        "actor": SOURCE,
        "nActive": 0,
        "k": 4,
        "W": 6,
        "diff": 0,
        "grad": -3000,
        "links": [{"peer": PEER, "cwnd": 0, "inFlight": 0}],
    }
    _record(monkeypatch, answer=answer)

    result = _invoke("link", "stats", "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    row = next(line for line in result.stdout.splitlines() if PEER in line)
    assert "0" in row
    assert "N_active 0" in result.stdout
    assert "grad -3s" in result.stdout
    # Absent fields are dashed, never re-derived by the shell.
    assert DASH in result.stdout


def test_link_stats_tolerates_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, answer={"ok": True})

    result = _invoke("link", "stats", "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    assert DASH in result.stdout


def test_link_add_reports_a_pending_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(
        monkeypatch,
        answer={"ok": True, "actor": SOURCE, "peer": PEER, "state": "pending"},
    )

    result = _invoke("link", "add", PEER, "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    assert calls == [(IPC_LINK_ADD, {"from": SOURCE, "peer": PEER})]
    assert "pending" in result.stdout
    assert "waiting for the peer to reference back" in result.stdout


def test_link_add_hold_is_a_queued_result_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(
        monkeypatch,
        answer={
            "ok": True,
            "peer": PEER,
            "queued": True,
            "holdReason": ipc_errors.LINK_SELF_FULL,
        },
    )

    result = _invoke("link", "add", PEER, "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    assert "held" in result.stdout
    assert ipc_errors.LINK_SELF_FULL in result.stdout


def test_link_drop_reports_the_drained_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(
        monkeypatch,
        answer={
            "ok": True,
            "actor": SOURCE,
            "peer": PEER,
            "state": "reclaimed",
            "drained": 2,
        },
    )

    result = _invoke("link", "drop", PEER, "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    assert calls == [(IPC_LINK_DROP, {"from": SOURCE, "peer": PEER})]
    assert "dropped link" in result.stdout
    assert "drained 2 pending request(s) as ack" in result.stdout


def test_link_drop_omits_the_drained_clause_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(
        monkeypatch,
        answer={"ok": True, "actor": SOURCE, "peer": PEER, "state": "reclaimed"},
    )

    result = _invoke("link", "drop", PEER, "--from", SOURCE)

    assert result.exit_code == 0, result.stdout
    assert "dropped link" in result.stdout
    assert "drained" not in result.stdout


def test_link_rejection_exits_one_and_keeps_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, answer={"ok": False, "code": ipc_errors.LINK_UNKNOWN})

    result = _invoke("link", "add", PEER, "--from", SOURCE, "--json")

    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["code"] == ipc_errors.LINK_UNKNOWN
    assert [method for method, _ in calls] == [IPC_LINK_ADD]


def test_link_operator_on_behalf_of_selects_the_managed_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, answer={"ok": True, "state": "established"})

    result = _invoke(
        "link", "drop", PEER, "--from", "user:op", "--on-behalf-of", SOURCE
    )

    assert result.exit_code == 0, result.stdout
    assert calls == [
        (
            IPC_LINK_DROP,
            {"from": "user:op", "onBehalfOf": SOURCE, "peer": PEER},
        )
    ]


def test_a_managed_worker_link_command_carries_its_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:op:node-1:worker")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session-worker")
    calls = _record(monkeypatch, answer=_list_answer())

    result = _invoke(
        "link", "list", "--from", "agent:op:node-1:worker", "--json"
    )

    assert result.exit_code == 0, result.stdout
    assert calls == [
        (
            IPC_LINK_LIST,
            {"from": "agent:op:node-1:worker", "sessionRef": "session-worker"},
        )
    ]


def test_link_constants_are_the_daemon_public_face_objects() -> None:
    from hyprial.daemon import (
        HOLD_REASONS as public_hold_reasons,
        IPC_MESSAGE_READ_BODY as public_read_body,
        SUMMARY_MAX_BYTES as public_max_bytes,
    )
    from hyprial.daemon.impl.links import ports

    assert IPC_LINK_LIST is ports.IPC_LINK_LIST
    assert IPC_LINK_STATS is ports.IPC_LINK_STATS
    assert IPC_LINK_ADD is ports.IPC_LINK_ADD
    assert IPC_LINK_DROP is ports.IPC_LINK_DROP
    assert public_read_body is ports.IPC_MESSAGE_READ_BODY
    assert public_hold_reasons is ports.HOLD_REASONS
    assert public_max_bytes is ports.SUMMARY_MAX_BYTES
