# Decision notes (E2E-031, 2026-11-19; first real runs 2026-11-20):
# - Opt-in stage (``--harness pi``), run by the hosted job of
#   mock-model-harness-e2e.yml: real headless Pi workers answer a group
#   row through the mock model (``_fake_provider.FakeProvider`` with
#   ``reply_to_harness_requests``), which calls ``harness_read`` and then
#   ``harness_reply`` with the row's message id.  Nothing else is scripted:
#   the mock model cannot mention a member (``harness_send`` takes no
#   conversation or mentions), and it stops replying once a conversation holds
#   a ``harness_reply``, so each worker takes exactly one turn here.
# - The agent and launch setup is the mock-model loopback gate's own
#   (``run-real-harness-loopback.py``), loaded by path so the two cannot drift.
# - The stage uses its own group of two Pi agents, after the base stage, so
#   no worker is alive across the base stage's restart.
# - A pass needs exactly one reply entry per worker in the group history,
#   authored by that worker, replying to the owner's entry and carrying the
#   mock model's text: only the harness's own harness_reply writes that.
# - The reply history write precedes the inbox acknowledgement.  GC-11 polls
#   each worker's durable row after the history barrier so the assertion does
#   not race the acknowledgement, while an unacknowledged row still fails.
#   The first real runs failed 2 of 7 here: both workers replied within
#   milliseconds and the second append failed as stale (fixed in
#   conversations.groups.log); the mock model does not retry a failed reply.
"""The mock-model stage of E2E-031 (GC-11): a woken harness answers in the group."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .fixture import Expected, evidence, member_ids, require_ok, rows_for_entry, run_json, wait_for

LOOPBACK_RUNNER = Path(__file__).resolve().parents[1] / "run-real-harness-loopback.py"
TURN_TIMEOUT = 120.0
WORKERS = ("P1", "P2")
MODEL = "deepseek-flash"
REPLY = "E2E031_REPLY"


def loopback() -> Any:
    """The mock-model loopback gate module, for its agent and launch helpers."""

    spec = importlib.util.spec_from_file_location("e2e031_loopback", LOOPBACK_RUNNER)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {LOOPBACK_RUNNER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def provider_environment(provider: Any, environ: dict[str, str]) -> dict[str, str]:
    """The daemon environment that points every harness at the local mock model."""

    from _deepseek import local_provider_environment

    module = loopback()
    environment = local_provider_environment(
        openai_base_url=provider.openai_base_url,
        anthropic_base_url=provider.anthropic_base_url,
    )
    environment.update(module._loopback_no_proxy(environ))
    return environment


def missing_harness() -> str | None:
    return None if shutil.which("pi") is not None else "pi is not on PATH"


def gc11_harness_turn(daemon: Any, provider: Any, root: Path, owner: str, nonce: str) -> None:
    """GC-11: two Pi workers are woken by one owner message mentioning them and each answers."""

    module = loopback()
    scenario = module.Scenario("pi", "headless", "/v1/chat/completions")
    started: list[str] = []
    try:
        for name in WORKERS:
            workspace = root / "workspaces" / name
            module._git_workspace(workspace)
            module._configure_agent(daemon, scenario, name, MODEL)
            argv = module._start_args(str(daemon.executable), scenario, name, workspace, MODEL)
            result = daemon.command(*argv[1:], timeout=90)
            if result.get("ok") is not True:
                raise Expected(f"agent start pi --name {name} --headless", "ok:true", result)
            started.append(name)
            module._wait_headless_ready(daemon, name, "pi")
        created = require_ok("message group create --member P1 --member P2", run_json(
            daemon, "message", "group", "create", "--title", f"e2e-031-pi-{nonce}",
            "--member", "P1", "--member", "P2"))
        group_id = str(created["conversationId"])
        members = member_ids(created)
        request_start = len(provider.requests)
        sent = require_ok("message send --conversation <pi group> --mention P1 --mention P2",
                          run_json(daemon, "message", "send", "--from", f"user:{owner}",
                                   "--conversation", group_id, "--mention", "P1",
                                   "--mention", "P2", "--summary", "hello", "hello pi workers"))
        entry_id = str(sent["messageId"])

        def replies() -> dict[str, list[dict[str, Any]]]:
            page = run_json(daemon, "message", "history", "--conversation", group_id, "--limit", "200")
            found: dict[str, list[dict[str, Any]]] = {}
            for entry in page.get("entries") or []:
                if (entry.get("replyTo") or {}).get("messageId") == entry_id:
                    found.setdefault(str(entry.get("from")), []).append(entry)
            return found

        found = wait_for("both Pi workers to answer in the group", replies,
                         lambda found: {members[name] for name in WORKERS} <= set(found),
                         timeout=TURN_TIMEOUT, interval=1.0)
        answered: dict[str, dict[str, Any]] = {}
        for name in WORKERS:
            own = found[members[name]]
            if len(own) != 1 or own[0].get("intent") != "reply" or own[0].get("message") != REPLY \
                    or own[0].get("conversationId") != group_id:
                raise Expected(f"{name}'s answer in the group",
                               f"exactly one reply entry with the mock model's text {REPLY!r}", own)
            answered[members[name]] = own[0]
        for name in WORKERS:
            address = next(row["address"] for row in created["members"] if row.get("displayName") == name)
            try:
                rows = wait_for(
                    f"durable row of {name} for the owner message",
                    lambda: rows_for_entry(daemon, group_id, entry_id).get(address, []),
                    lambda candidate: len(candidate) == 1 and candidate[0]["consumed"] == 1,
                    timeout=15.0,
                    interval=0.25,
                )
            except AssertionError as error:
                rows = rows_for_entry(daemon, group_id, entry_id).get(address, [])
                raise Expected(f"durable row of {name} for the owner message",
                               "exactly one row, settled by the reply", rows) from error
            if len(rows) != 1 or rows[0]["consumed"] != 1:
                raise Expected(f"durable row of {name} for the owner message",
                               "exactly one row, settled by the reply", rows)
            reply = answered[members[name]]
            if rows_for_entry(daemon, group_id, str(reply["messageId"])):
                raise Expected(f"rows for {name}'s reply", "none (a reply to the owner wakes nobody)",
                               rows_for_entry(daemon, group_id, str(reply["messageId"])))
        paths = provider.requests[request_start:]
        if not any(path.startswith("/v1/chat/completions") for path in paths):
            raise Expected("mock model requests", "Pi called /v1/chat/completions", paths)
        evidence("GC-11", group=group_id, entry=entry_id,
                 replies={name: answered[members[name]]["messageId"] for name in WORKERS},
                 providerRequests=len(paths))
    except AssertionError as error:
        diagnostics = {name: module._worker_log_tail(daemon, name) for name in started}
        error.add_note("worker logs: " + json.dumps(diagnostics, default=str)[-4000:])
        raise
    finally:
        for name in started:
            daemon.command("agent", "stop", name, "--json", exits=(0, 1))


__all__ = ["gc11_harness_turn", "missing_harness", "provider_environment"]
