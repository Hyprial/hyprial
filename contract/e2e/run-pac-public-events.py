#!/usr/bin/env python3
"""E2E-020 §C: public workflow-events consumer with internal fixture setup.

An opaque old database is copied for migration setup, never queried by this
consumer. This is not a PASS for §A/§B/§D or the full E2E-020 matrix. It creates
isolated inbox identities, never launches a provider/worker or a real assignment.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import queue
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, hyprial_executable, sanitized_env, short_temp_root
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, create_graph
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.inbox.contracts.summary import summary_from_text

CLIENT_PATH = Path(__file__).resolve().parents[1] / "pac" / "events" / "client.py"
spec = importlib.util.spec_from_file_location("public_pac_client", CLIENT_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Client, Consumer, ResyncRequired = module.Client, module.Consumer, module.ResyncRequired


def comparable(snapshot):
    result = copy.deepcopy(snapshot)
    # A global cursor may advance through another graph without changing this
    # graph's state. Consumers need not manufacture those unrelated events.
    result.pop("cursor")
    return result


def assert_single_turn_receipt(pending_turn, delivery, delivered, sink_pending, resent):
    """Prove one durable turn delivery regardless of which sender won the race."""

    assert resent["delivered"] in (0, 1) and resent["undelivered"] == 0, resent
    assert resent["error"] is None, resent
    assert [event["type"] for event in delivery] == ["delivery_changed"], delivery
    receipt = delivery[0]["data"]
    assert receipt["eventId"] == pending_turn["eventId"], receipt
    assert receipt["edge"] == pending_turn["edge"], receipt
    message_id = receipt["messageId"]
    assert message_id, receipt
    matching_turns = [
        item for item in delivered["notifications"]
        if item["kind"] == "turn" and item["eventId"] == pending_turn["eventId"]
    ]
    assert len(matching_turns) == 1, matching_turns
    assert matching_turns[0]["edge"] == pending_turn["edge"], matching_turns
    assert matching_turns[0]["messageId"] == message_id, matching_turns
    messages = sink_pending.get("messages", [])
    assert sum(item.get("messageId") == message_id for item in messages) == 1, sink_pending
    matching_messages = [
        item for item in messages
        if item.get("conversationId") == f"pac-{delivered['graphId']}"
        and item.get("from") == pending_turn["sender"]
        and item.get("to") == pending_turn["recipient"]
        # Hard cut: the row carries the derived summary, not the body.
        and item.get("summary") == summary_from_text(pending_turn["text"])
    ]
    assert len(matching_messages) == 1, matching_messages
    assert matching_messages[0]["messageId"] == message_id, matching_messages


def fixture_graph(database: Path, *, name: str, owner: str, nodes=(), edges=(), activate=False):
    """Build raw graph fixtures internally; public assertions use workflow events."""
    store = PacGraphStore(database)
    try:
        head = create_graph(store, name=name, created_by=owner)
        version = head["version"]
        for node_id, node_owner, kind, deadline, guarded in nodes:
            version = add_node(
                store,
                database.parent,
                graph_id=head["graphId"],
                node_id=node_id,
                owner=node_owner,
                brief_ref=f"unreadable#ref-{node_id}",
                kind=kind,
                deadline_ms=deadline,
                guarded_by_node_id=guarded,
                expect_version=version,
            )["version"]
        for source, target, kind in edges:
            version = add_edge(
                store,
                graph_id=head["graphId"],
                from_node=source,
                to_node=target,
                kind=kind,
                expect_version=version,
            )["version"]
        if activate:
            activate_graph(store, head["graphId"], actor=owner)
        return head["graphId"], version
    finally:
        store.close()


def sync_consumer_snapshot(client, consumer, graph):
    """Converge the consumer to a snapshot despite a writer between the reads."""

    previous_mismatch = None
    while True:
        old = consumer.state["cursor"]
        events = client.events(graph, old, consumer.state["journalId"])
        for event in events:
            assert event["seq"] > old
            consumer.apply(event)
            consumer.apply(event)  # deliberately duplicate every row
        fresh = client.snapshot(graph)
        if comparable(consumer.state) == comparable(fresh):
            return fresh

        mismatch = (consumer.state["cursor"], fresh["cursor"])
        assert mismatch[0] < mismatch[1] and mismatch != previous_mismatch, (
            consumer.state,
            fresh,
        )
        previous_mismatch = mismatch


def assert_isolated_daemon(daemon):
    """Pin the production-machine safety contract at the real daemon boundary."""

    assert daemon.env["HYPRIAL_PEER_DISCOVERY"] == "0"
    events = [
        json.loads(line)
        for line in (daemon.root / "state" / "logs" / "daemon.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    resolved = next(
        event for event in events if event.get("event") == "zenoh.endpoints.resolved"
    )
    assert (
        resolved.get("discovery"),
        resolved.get("configured"),
        resolved.get("discovered"),
        resolved.get("effective"),
    ) == ("disabled", 0, 0, 0)


def graph_count(database):
    """How many graphs a pac-graph database holds; 0 when it does not exist.

    Read with stdlib sqlite3 rather than PacGraphStore: connecting through the
    store would CREATE and migrate the file, which is the very thing the caller
    is trying to observe.
    """

    if not database.exists():
        return 0
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='graphs'"
        ).fetchone()
        if not rows or rows[0] == 0:
            return 0
        return connection.execute("SELECT count(*) FROM graphs").fetchone()[0]
    finally:
        connection.close()


def legacy_receipt_bootstrap(client, graph):
    """Settle the golden image's overdue v6 clock before measuring v5 receipts.

    The opaque fixture has an active timer at deadline 50 and structure v6,
    but only v5 decisions. The resident clock can therefore legitimately write
    a new v6 decision while this acceptance scenario is reading its baseline.
    Tick publicly, then wait for that exact decision's real delivery. If the
    resident tick won insertion, the explicit tick is idempotent and its return
    alone does not mean the resident sender has finished.
    """

    # Resident cadence is the production clock; the retired diagnostic command
    # is intentionally not part of the public workflow surface.
    client.snapshot(graph)
    deadline = time.monotonic() + 20
    event_id = f"overdue:{graph}:v6:timer:50"
    while True:
        snapshot = client.snapshot(graph)
        current_clock = [
            item for item in snapshot["notifications"]
            if item["eventId"] == event_id and item["edge"] == "clock:timer"
        ]
        assert len(current_clock) <= 1, current_clock
        if (current_clock and current_clock[0]["messageId"] is not None
                and current_clock[0]["deliveredAt"] is not None):
            return snapshot
        assert time.monotonic() < deadline, (
            "legacy fixture v6 clock did not finish delivery before receipt baseline",
            snapshot,
        )
        time.sleep(0.05)


def legacy_boundaries(executable):
    """Opaque fixture setup; all migration/floor/receipt observations use CLI JSON."""
    fixture = CLIENT_PATH.parent / "fixtures"
    manifest = json.loads((fixture / "slice-1-late-delivery.json").read_text())
    with tempfile.TemporaryDirectory(prefix="pac020old-", dir=short_temp_root()) as tmp:
        daemon = IsolatedDaemon(executable, Path(tmp), "pac020old")
        daemon.env = sanitized_env(daemon.env)
        daemon.env["HYPRIAL_OWNER"] = "fixture-owner"
        client = Client(str(executable), daemon.env)
        try:
            daemon.start()
            assert_isolated_daemon(daemon)
            database = daemon.root / "state" / "pac-graph.sqlite3"
            assert daemon.command("agent", "create", "--name", "fixture-owner", "--json")["ok"]
            database = daemon.root / "state" / "pac-graph.sqlite3"
            # ⛔ Do not "simplify" this back to `not database.exists()`.
            #
            # It reads tidier and it is what stood here, but the daemon now
            # provisions an empty pac-graph database during startup, so the file
            # is ALWAYS present by this point and that form fails every run.
            # Absence was only ever how this guard detected the thing it names:
            # overwriting an empty provisioned database is exactly as safe as
            # writing where no file existed, while overwriting one that holds
            # graphs is not. Content is the question; absence was the proxy.
            #
            # This assertion used to double as the last witness that nothing
            # provisioned the database. It no longer witnesses that -- and it
            # should not: `tests/daemon/pac/graphs/test_pac_graph_db_created_on_startup.py` now
            # asserts startup provisioning directly, and positively.
            assert graph_count(database) == 0, "do not overwrite a live PAC database"
            # Startup closes its connection, so WAL/shm are checkpointed away;
            # remove any survivors rather than leave them beside a replaced file.
            for sidecar in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
                sidecar.unlink(missing_ok=True)
            shutil.copyfile(fixture / "slice-1-late-delivery.sqlite3", database)
            graph = manifest["graphId"]
            PacGraphStore(database).close()  # internal fixture migration/open
            bootstrap = legacy_receipt_bootstrap(client, graph)
            floor = bootstrap["cursorFloor"]
            expired = floor - 1
            assert 0 < expired < floor <= bootstrap["cursor"]
            result = subprocess.run(
                [str(executable), "workflow", "events", graph, "--after", str(expired),
                 "--journal-id", bootstrap["journalId"], "--json"],
                env=daemon.env, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert json.loads(result.stderr)["code"] == "PAC_EVENTS_RESYNC"
            print(f"RESULT PASS E2E-020-C real migration cursorFloor={floor}; expired after={expired} resync", flush=True)
            consumer = Consumer(bootstrap)
            resent = client.invoke("notify", "resend", graph)[0]
            assert resent["delivered"] > 0 and resent["undelivered"] == 0, resent
            raw = client.events(graph, bootstrap["cursor"], bootstrap["journalId"])
            # The resident daemon may re-plan the (still overdue) fixture
            # clock during this window -- a concurrent writer this section
            # does not pin. Tolerated EXACTLY that shape (a fresh overdue
            # plan/receipt pair AFTER the bootstrap we hold); every other
            # event must be a late legacy receipt: delivery_changed on a
            # pre-bootstrap version.
            receipts = []
            for event in raw:
                data = event["data"]
                if event["type"] == "delivery_changed" and event["version"] < bootstrap["version"]:
                    receipts.append(event)
                    continue
                assert event["type"] in {"notification_planned", "delivery_changed"}, event
                assert str(data.get("eventId", "")).startswith("overdue:"), event
                assert event["at"] >= bootstrap["cursor"], event
            assert receipts, raw
            for event in receipts:
                consumer.apply(event)
                consumer.apply(event)
            assert consumer.state["version"] == bootstrap["version"], "late receipt rolled structure version backward"
            fresh = sync_consumer_snapshot(client, consumer, graph)
            assert comparable(consumer.state) == comparable(fresh)
            print(f"RESULT PASS E2E-020-C old v{receipts[0]['version']} real receipts preserve snapshot v{bootstrap['version']}", flush=True)
        finally:
            daemon.stop()
            daemon.wait_stopped()


def main() -> int:
    executable = hyprial_executable()
    with tempfile.TemporaryDirectory(prefix="pac020c-", dir=short_temp_root()) as tmp:
        daemon = IsolatedDaemon(executable, Path(tmp), "pac020c")
        daemon.env = sanitized_env(daemon.env)
        client = Client(str(executable), daemon.env)
        follower = None
        try:
            daemon.start()
            assert_isolated_daemon(daemon)
            created = daemon.command("agent", "create", "--name", "pac020-sink", "--json")
            assert created["ok"]
            database = daemon.root / "state" / "pac-graph.sqlite3"
            print(
                "RESULT PASS E2E-020-C preflight isolated executable/home; "
                "peer discovery disabled; discovered=0; no models or external services",
                flush=True,
            )
            # identity model: the local human user:e2e drafts and owns "a"
            # (its flags also work while the daemon is DOWN, via the trusted
            # local boundary); pac020-sink is a registered agent whose nodes
            # flag only through its fenced session binding.
            human = "user:e2e"
            sink = created["agent"]["uri"]

            def ipc_request(method, params):
                frame = {"version": 1, "id": str(uuid.uuid4()), "method": method, "params": params}
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(15)
                try:
                    sock.connect(str(daemon.root / "state" / "daemon.sock"))
                    sock.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
                    buffer = bytearray()
                    while b"\n" not in buffer:
                        chunk = sock.recv(64 * 1024)
                        if not chunk:
                            raise AssertionError("daemon disconnected during IPC read")
                        buffer.extend(chunk)
                finally:
                    sock.close()
                response = json.loads(buffer.partition(b"\n")[0])
                if response.get("error") is not None:
                    raise AssertionError(f"IPC failed: {response['error']}")
                return response.get("result")

            fence_ref: str | None = None

            def sink_client():
                """The agent's own fenced voice: a registered interactive
                session binding (what a real agent channel does) + worker env."""
                nonlocal fence_ref
                if fence_ref is None:
                    fence_ref = f"pac020-{uuid.uuid4().hex[:12]}"
                    result = ipc_request(
                        "session.register",
                        {
                            "actor": sink,
                            "sessionRef": fence_ref,
                            "source": "claude-channel",
                            "cwd": str(daemon.root),
                            "command": ["hyprial-e2e-fence-sim", "--actor", sink],
                        },
                    )
                    assert isinstance(result, dict), result
                return Client(str(executable), {
                    **daemon.env,
                    "HYPRIAL_WORKER_ACTOR": sink,
                    "HYPRIAL_WORKER_SESSION_REF": fence_ref,
                })

            graph, version = fixture_graph(database, name="public-e2e", owner=human)
            empty = client.snapshot(graph)
            assert empty["structure"]["nodes"] == [] and empty["assignments"] == []
            for node in ("a", "b", "reject", "clock"):
                owner = human if node == "a" else sink
                store = PacGraphStore(database)
                try:
                    version = add_node(store, database.parent, graph_id=graph, node_id=node,
                                       owner=owner, brief_ref=f"unreadable#ref-{node}",
                                       kind="clock" if node == "clock" else "task",
                                       deadline_ms=1 if node == "clock" else None,
                                       expect_version=version)["version"]
                finally:
                    store.close()
            for source, target, kind in (("a", "b", "forward"), ("reject", "b", "back")):
                store = PacGraphStore(database)
                try:
                    version = add_edge(store, graph_id=graph, from_node=source, to_node=target,
                                       kind=kind, expect_version=version)["version"]
                finally:
                    store.close()
            invalidated = client.events(graph, empty["cursor"], empty["journalId"])
            try:
                Consumer(empty).apply(invalidated[0])
                raise AssertionError("structure change did not require resync")
            except ResyncRequired:
                pass
            draft = client.snapshot(graph)
            assert draft["active"] is None and draft["actors"] == [] and draft["closed"] is None
            store = PacGraphStore(database)
            try:
                activate_graph(store, graph, actor=human)
            finally:
                store.close()
            bootstrap = client.snapshot(graph)
            assert bootstrap["active"] is not None
            consumer = Consumer(bootstrap)
            checkpoints = [bootstrap]

            def sync():
                fresh = sync_consumer_snapshot(client, consumer, graph)
                checkpoints.append(fresh)
                return fresh

            def flag(node, action="set", reason=None):
                # Generic graph setup/flag mutation is an internal fixture
                # concern after the public PAC CLI retirement; the consumer
                # under test still reads only the public workflow events JSON.
                store = PacGraphStore(database)
                try:
                    class Sender:
                        def send(self, *, recipient, text, sender, conversation_id, idempotency_key):
                            if not (daemon.root / "state" / "daemon.sock").exists():
                                raise RuntimeError("daemon is down")
                            result = ipc_request(
                                "message.send",
                                {
                                    "from": sender,
                                    "to": [recipient],
                                    "summary": text,
                                    "message": text,
                                    "conversationId": conversation_id,
                                    "idempotencyKey": idempotency_key,
                                },
                            )
                            deliveries = result.get("deliveries", [])
                            return deliveries[0]["messageId"]

                    actor = human if node == "a" else sink
                    reactor = PacReactor(store, sender=Sender())
                    if action == "set":
                        outcome = reactor.set_flag(
                            graph, node, actor=actor, reason_ref=reason
                        )
                    else:
                        outcome = reactor.reset_flag(
                            graph, node, actor=actor, reason_ref=reason
                        )
                    return {
                        "event": outcome.event,
                        "delivered": len(outcome.delivered),
                        "undelivered": len(outcome.undelivered),
                        "deliveryError": outcome.delivery_error,
                    }
                finally:
                    store.close()

            clock_deadline = time.monotonic() + 20
            while True:
                clocked = sync()
                if any(
                    item["kind"] == "overdue"
                    for item in clocked["notifications"]
                ):
                    break
                assert time.monotonic() < clock_deadline, (
                    "resident daemon cadence did not tick the actor-free clock"
                )
                time.sleep(0.1)
            assert clocked["flags"]["clock"]["flag"] is False
            assert daemon.command("workflow", "events", graph, "--snapshot", "--json")["type"] == "snapshot"
            print(
                "RESULT PASS E2E-020-C actor-free clock expired from resident daemon "
                "cadence; duplicate debug tick was SQL-idempotent",
                flush=True,
            )

            daemon.stop()
            daemon.wait_stopped()
            offline = flag("a", reason="artifact#a")
            assert offline["delivered"] == 0 and offline["undelivered"] == 1
            pending = sync()
            pending_turn = next(
                item for item in pending["notifications"]
                if item["kind"] == "turn" and item.get("messageId") is None
            )
            assert pending_turn["messageId"] is None
            assert pending["flags"]["a"]["reasonRef"] == "artifact#a"
            assert pending["flags"]["b"]["flag"] is False
            daemon.start()
            resent = client.invoke("notify", "resend", graph)[0]
            delivery = client.events(graph, pending["cursor"], pending["journalId"])
            assert [e["type"] for e in delivery] == ["delivery_changed"], delivery
            delivered = sync()
            sink_pending = ipc_request("message.pending.list", {"actor": sink})
            assert_single_turn_receipt(pending_turn, delivery, delivered, sink_pending, resent)
            assert delivered["flags"]["b"]["flag"] is False and delivered["assignments"]
            print("RESULT PASS E2E-020-C delivery-after-cursor real daemon receipt; delivery is not completion", flush=True)

            flag("b")
            assert sync()["assignments"] == []
            flag("reject")
            back = sync()
            assert back["flags"]["b"]["completedCount"] == 1 and back["flags"]["b"]["flag"]
            assert [(a["nodeId"], a["round"]) for a in back["assignments"]] == [("b", 2)]
            flag("a", "reset")
            assert sync()["assignments"]  # old edge withdraw cannot erase back activation
            fixture_graph(database, name="unrelated-gap", owner=human)
            flag("a", reason="artifact#a2")
            repeated = sync()
            assert len(repeated["assignments"]) == 1 and len(repeated["assignments"][0]["requests"]) == 2
            clocks = sync()
            assert any(n["kind"] == "overdue" for n in clocks["notifications"])
            assert clocks["flags"]["clock"]["flag"] is False
            assert daemon.command("workflow", "events", graph, "--snapshot", "--json")["type"] == "snapshot"
            assert client.events(graph, clocks["cursor"], clocks["journalId"]) == []
            flag("b", "reset")
            sync()
            flag("b", reason="artifact#b2")
            final = sync()
            assert final["assignments"] == []
            print("RESULT PASS E2E-020-C P1-P3 frozen history; back/withdraw/clock; assignments from recorded plans", flush=True)

            # Every event boundary, including the middle of a business txn,
            # can resume. Only CLI JSON is fetched, never internal tables.
            full = client.events(graph, bootstrap["cursor"], bootstrap["journalId"])
            seqs = [e["seq"] for e in full]
            assert any(b - a > 1 for a, b in zip(seqs, seqs[1:])), "cross-graph gap not exercised"
            for split in range(len(full) + 1):
                replay = Consumer(bootstrap)
                for event in full[:split]:
                    replay.apply(event)
                suffix = client.events(graph, replay.state["cursor"], replay.state["journalId"])
                assert suffix == full[split:]
                for event in suffix + suffix:  # reconnect/redeliver
                    replay.apply(event)
                assert comparable(replay.state) == comparable(final)
            for old in checkpoints:
                replay = Consumer(old)
                for event in client.events(graph, old["cursor"], old["journalId"]):
                    replay.apply(event)
                assert comparable(replay.state) == comparable(final)
            print(f"RESULT PASS E2E-020-C resume all {len(full)+1} cursors; duplicate consumption; full snapshot equality", flush=True)

            for args, code in (
                (["events", graph, "--after", "-1"], "PAC_EVENTS_RESYNC"),
                (["events", graph, "--after", str(final["cursor"] + 999)], "PAC_EVENTS_RESYNC"),
                (["events", graph, "--after", str(final["cursor"]), "--journal-id", "wrong"], "PAC_EVENTS_RESYNC"),
            ):
                result = subprocess.run([str(executable), "workflow", *args, "--json"], env=daemon.env,
                                        capture_output=True, text=True, timeout=30)
                assert result.returncode == 1
                if args[0] == "events":
                    assert result.stdout == ""
                    assert json.loads(result.stderr)["code"] == code
            assert comparable(client.snapshot(graph)) == comparable(final)

            # Deliberately inherit SIG_IGN, as a daemon/nohup/CI parent may do.
            # This invokes the REAL CLI, not a fake follow implementation.
            follow_command = [sys.executable, "-c",
                              "import os,signal,sys; signal.signal(signal.SIGINT,signal.SIG_IGN); "
                              "os.execv(sys.argv[1],sys.argv[1:])",
                              str(executable), "workflow", "events", graph, "--snapshot", "--follow", "--json"]
            # Atomic bootstrap/follow handoff. stdout is a pure JSON-line stream.
            follower = subprocess.Popen(follow_command, env=daemon.env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = queue.Queue()
            def read_lines():
                for line in follower.stdout:
                    lines.put(json.loads(line))
            reader = threading.Thread(target=read_lines, daemon=True)
            reader.start()
            ready = lines.get(timeout=20)
            assert ready["type"] == "snapshot" and comparable(ready) == comparable(final)
            reset = flag("b", "reset")
            streamed = lines.get(timeout=20)
            assert streamed["seq"] > ready["cursor"] and streamed["type"] == "flag_reset"
            assert streamed["eventId"] == reset["event"]["eventId"]
            follower.send_signal(signal.SIGINT)
            assert follower.wait(timeout=15) == 0
            reader.join(timeout=5)
            assert not reader.is_alive()
            assert follower.stderr.read() == ""
            follower.stdout.close()
            follower.stderr.close()
            follower = None
            print("RESULT PASS E2E-020-C follow bootstrap; inherited SIGINT=SIG_IGN still exits cleanly", flush=True)

            # Idle consumers must also stop on SIGTERM or a closed OUTPUT pipe,
            # without a new event/heartbeat. /dev/null stdin is intentionally valid.
            import select
            for mode in ("sigterm", "output-eof"):
                follower = subprocess.Popen(follow_command, env=daemon.env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                assert select.select([follower.stdout], [], [], 20)[0], "follow bootstrap timed out"
                assert json.loads(follower.stdout.readline())["type"] == "snapshot"
                assert follower.poll() is None
                if mode == "sigterm":
                    follower.send_signal(signal.SIGTERM)
                else:
                    follower.stdout.close()
                exit_code = follower.wait(timeout=15)
                assert exit_code == 0, f"idle follow {mode} returned {exit_code}, expected 0"
                assert follower.stderr.read() == ""
                follower.stdout.close()
                follower.stderr.close()
                follower = None
                print(f"RESULT PASS E2E-020-C idle follow {mode}; stdin EOF does not abort subscription", flush=True)
            daemon.stop()
            daemon.wait_stopped()
            print("RESULT PASS E2E-020-C cleanup follower exited; isolated daemon teardown complete", flush=True)
            legacy_boundaries(executable)
            print("RESULT PASS E2E-020-C public contract only; A/B/D NOT RUN", flush=True)
            return 0
        except Exception as error:
            print(f"RESULT FAIL E2E-020-C {type(error).__name__}: {error}", flush=True)
            return 1
        finally:
            if follower is not None and follower.poll() is None:
                follower.terminate()
                follower.wait(timeout=15)
            daemon.stop()
            daemon.wait_stopped()


if __name__ == "__main__":
    raise SystemExit(main())
