"E2E: the F3 fetch fence does not reject the headless pi worker path.\n\nReview point for fix/mcp-actor-by-name: F3 rejects an explicit inbox fetch\n(``fetched=true``) that signs no sessionRef. This file proves, against a REAL\nisolated daemon, that the daemon-managed pi headless worker path is unaffected:\n\n* the daemon launches the worker (``lifecycle.start``, harness ``pi``,\n  ``--mode rpc``) with the provisioned worker-channel identity\n  (``HYPRIAL_WORKER_ACTOR`` / ``HYPRIAL_WORKER_SESSION_REF`` — exactly what\n  ``build_worker_channel`` injects and ``pi_harness_bridge.ts`` signs with);\n* a fetch signed with that identity — the same ``python -m hyprial.daemon.impl.mcp.entry agent-channel``\n  command the daemon composes for the worker — succeeds (the fence's\n  worker-session exemption, not a rejection);\n* the SAME actor's unfenced explicit fetch is rejected with STALE_SESSION\n  (that is the hole F3 closes, and the rejection is what the signed path is\n  exempt from);\n* one real dispatched request reaches the worker process (the in-tree\n  ``tests/fixtures/harnesses/fake_pi_rpc.py`` pi-RPC double records the prompt) and is\n  consumed by the harness path.\n"

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import IsolatedHarness, _wait_until

pytestmark = pytest.mark.anyio

OWNER = "owner-a"
NODE = "node-e2e"
WORKER = "pi-worker"


@pytest.fixture()
def harness() -> Iterator[IsolatedHarness]:
    # Under /tmp like the sibling e2e file: AF_UNIX paths are length-capped
    # and pytest's tmp_path is too long for the daemon socket.
    root = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprial-piw-"))
    value = IsolatedHarness(root, membership=True)
    value.environment.update(
        {
            # The daemon must carry these: the worker process inherits its
            # environment, and the fake pi double reads them at spawn.
            "FAKE_PI_MODE": "settled",
            "FAKE_PI_LOG": str(root / "pi-cmds.jsonl"),
            "FAKE_PI_ARGV_LOG": str(root / "pi-argv.jsonl"),
        }
    )
    value.write_owner(OWNER)
    try:
        value.start_daemon()
        # The real link gate needs an org directory. The explicit membership
        # double lets org.create initialize its authority before creating links.
        value.ipc("org.create", {"org": "group-links-pi-headless"})
        yield value
    finally:
        try:
            value.ipc("down", {"name": WORKER, "provider": "pi"})
        except Exception:  # noqa: BLE001 - teardown must not mask the failure
            pass
        if value.daemon is not None:
            value.stop_daemon()
        shutil.rmtree(root, ignore_errors=True)


async def test_pi_headless_worker_fetch_path_is_not_rejected(
    harness: IsolatedHarness,
) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    fake_pi = Path(__file__).resolve().parents[5] / 'tests/fixtures/harnesses/fake_pi_rpc.py'
    # Env-dump wrapper around the in-tree pi-RPC double: the daemon spawns this
    # as the worker process, so the provisioned worker-channel identity (what
    # the real pi extension would read) becomes observable to the test.
    wrapper = harness.root / "fake_pi_env_wrapper.py"
    wrapper.write_text(
        "import json, os, sys\n"
        f"log = {str(harness.root / 'pi-env.json')!r}\n"
        "with open(log, 'w') as handle:\n"
        "    json.dump(\n"
        "        {k: v for k, v in os.environ.items() if k.startswith('HYPRIAL_')},\n"
        "        handle,\n"
        "    )\n"
        f"os.execv(sys.executable, [sys.executable, {str(fake_pi)!r}, *sys.argv[1:]])\n"
    )
    harness.ipc("agent.create", {"name": "sender"})

    started = harness.ipc(
        "lifecycle.start",
        {
            "provider": "pi",
            "name": WORKER,
            "headless": True,
            "command": [sys.executable, str(wrapper)],
            "cwd": str(harness.root),
            "args": [],
        },
    )
    assert started["ok"] is True

    def worker_env() -> dict[str, str] | None:
        path = harness.root / "pi-env.json"
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        return record or None

    def worker_running() -> bool:
        connectors = harness.ipc("ps", {}).get("connectors", [])
        return any(
            isinstance(item, dict) and item.get("name") == WORKER
            for item in connectors
        )

    await _wait_until(worker_env, timeout=45.0)
    await _wait_until(worker_running, timeout=45.0)
    identity = worker_env()
    assert identity is not None
    worker_uri = identity["HYPRIAL_WORKER_ACTOR"]
    worker_ref = identity["HYPRIAL_WORKER_SESSION_REF"]
    assert worker_uri == f"agent:{OWNER}:{NODE}:{WORKER}"

    # 1. The worker-channel fetch, signed exactly as the daemon provisioned
    #    it (build_worker_channel composes this very command for the worker;
    #    the pi extension signs with the same identity).
    agent_channel_log = (harness.root / "agent-channel.log").open("w")
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "hyprial.daemon.impl.mcp.entry",
            "agent-channel",
            "--actor",
            worker_uri,
            "--session-ref",
            worker_ref,
        ],
        env=harness.environment,
        errlog=agent_channel_log,
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                read_result = await session.call_tool("harness_read", {})
                payload = json.loads(read_result.content[0].text)  # type: ignore[index, union-attr]
                assert payload["ok"] is True, payload
    finally:
        agent_channel_log.close()

    # 2. The same actor's UNFENCED explicit fetch is rejected -- that is the
    #    boundary F3 closes, and the signed worker path above is exempt from
    #    it via the worker-session fence, not by luck.
    with pytest.raises(AssertionError) as rejected:
        harness.ipc(
            "message.pending.list",
            {"actor": worker_uri, "fetched": True},
        )
    assert "STALE_SESSION" in str(rejected.value)

    # 3. One real dispatched request reaches the headless worker process and
    #    is consumed by the harness path (fake pi records the prompt, the
    #    turn settles, the delivery reaches a terminal state).
    sent = harness.ipc(
        "message.send",
        {"actor": "sender", "to": [worker_uri], "summary": "worker, report.", "message": "worker, report."},
    )
    assert sent["ok"] is True

    def prompt_arrived() -> bool:
        path = harness.root / "pi-cmds.jsonl"
        if not path.exists():
            return False
        return "prompt" in path.read_text()

    await _wait_until(prompt_arrived, timeout=45.0)

    def terminal_state() -> str | None:
        status = harness.ipc(
            "message.status",
            {"from": "sender", "messageId": sent["messageId"]},
        )
        records = status.get("records")
        if not isinstance(records, list) or not records:
            return None
        state = records[0].get("state")
        return state if state != "pending" else None

    await _wait_until(terminal_state, timeout=45.0)
    assert terminal_state() == "fetched"
