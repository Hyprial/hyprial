"""AT07 I4: the P2 chain driven across two real daemons.

Three arms, one file:

* the positive arm -- quiesce, the source daemon's crash window, export/ship,
  the target's land+activate, the source's restart **under the hold**, and the
  completion on the target's own report;
* the fence's arm -- the same tree with the proven row *removed* refuses the
  call on both entries, with the P0 code and mode string, before any byte moves
  (a missing row is the exact condition the fence exists to refuse);
* the harness arm -- the run tree with the neighbour's own row deleted refuses
  the same call for the harness nobody ran (the harness axis, isolated from
  the row axis above);
* the mutation arm -- one tampered bundle byte refuses the target landing and
  leaves the source held rather than retired.

Every step is a readable row; where a step is a *window*, the assertion is on
the durable witness (the daemon's own ledger or `ps`), never on a receipt the
chain vouched for itself.

Decision 2026-11-01 (0.5.5.3 shards-green): the Claude credential gate refuses
any launch before the agent holds a credential, and a start-time autostart of
the claude worker is refused permanently before an operator grant could exist
(8135feacde). The claude arm therefore brings the worker up the way the
c3-envkey reading does -- daemon first, then agent.create, one operator
env grant, then lifecycle.start -- while the target side keeps its carried
placeholder ``.credentials.json``. pi keeps its desired-state autostart.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
from hyprial.daemon.impl.state_db import StateDatabase
from datetime import UTC, datetime

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.kernel import HarnessLaunchSpec

from tests.support.transfer.signed import sign_bundle_for_target

from .conftest import (
    FAKE_CLAUDE,
    FAKE_PI,
    NEIGHBOUR,
    OWNER,
    SOURCE_NODE,
    TARGET_NODE,
    WORKER,
)

PLAN_DIGEST = "a" * 64

NODE = shutil.which("node")
PI = shutil.which("pi")


def _pi_package_root() -> Path | None:
    """The installed SDK beside the real pi command, or None when absent."""

    if PI is None:
        return None
    candidate = Path(PI).resolve()
    for parent in (candidate, *candidate.parents):
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        try:
            body = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if body.get("name") == "@earendil-works/pi-coding-agent":
            return parent
    return None


PI_ROOT = _pi_package_root()

if os.environ.get("HYPRIAL_REQUIRE_PI_SDK") and (NODE is None or PI_ROOT is None):
    # CI installs the pinned SDK; a missing one there is a broken guard, not a skip.
    raise RuntimeError("HYPRIAL_REQUIRE_PI_SDK is set but node or the pi SDK is missing")


def ipc_raw(node, method: str, params: dict) -> dict:
    """A raw request, so a typed rejection keeps its error code and data."""

    frame = json.dumps(
        {"version": 1, "id": uuid.uuid4().hex, "method": method, "params": params},
        separators=(",", ":"),
    ).encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(30.0)
    try:
        client.connect(str(node.socket_path))
        client.sendall(frame + b"\n")
        data = bytearray()
        while b"\n" not in data:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise OSError("daemon closed IPC before responding")
            data.extend(chunk)
    finally:
        client.close()
    return json.loads(data.partition(b"\n")[0])


def fake_for(harness: str) -> Path:
    """The repository's protocol double for one harness -- the only fake here."""

    double = FAKE_CLAUDE if harness == "claude" else FAKE_PI
    assert double.is_file(), "test fixture missing, not a transfer product failure"
    return double


def seed_worker(
    node, name: str = WORKER, *, session_ref: str | None = None, harness: str = "pi"
) -> None:
    """Persist a real launch spec whose only double is the model protocol."""

    store = DesiredStateStore(
        node.state / "desired-state.json",
        state_db=StateDatabase(node.state / "lifecycle-operations.sqlite3"),
    )
    store.save(
        DesiredState(
            harnesses=(
                HarnessLaunchSpec(
                    harness=harness, name=name, headless=True, cwd=str(node.root),
                    session_ref=session_ref or f"test-{name}",
                    command=(sys.executable, str(fake_for(harness))),
                ),
            )
        )
    )


def connectors(node) -> dict[str, dict]:
    return {item["name"]: item for item in node.ipc("ps", {})["connectors"]}


def bring_up_worker(node, harness: str) -> None:
    """Start the daemon and put the worker into the running state.

    pi autostarts from the seeded desired state. The claude credential gate
    refuses any launch before the agent holds a credential, and a start-time
    autostart would quarantine the worker permanently before an operator
    grant could exist; so claude follows the c3-envkey shape instead: daemon
    first, then agent.create, one operator env grant, then lifecycle.start.
    The target side still receives the carried placeholder credential.
    """

    if harness == "pi":
        seed_worker(node, harness=harness)
        node.start_daemon()
        return
    node.start_daemon()
    node.ipc("agent.create", {"name": WORKER})
    entry = "claude-env"
    node.ipc(
        "agent.secret.provider-write",
        {"entryId": entry, "fieldName": "token", "value": "sk-dummy-at07-shards"},
    )
    node.ipc(
        "agent.secret.grant",
        {
            "actor": WORKER,
            "grantId": "g-token",
            "source": "user-provider",
            "entryId": entry,
            "fieldName": "token",
            "environmentNames": ["ANTHROPIC_AUTH_TOKEN"],
            "revision": 1,
        },
    )
    node.ipc(
        "lifecycle.start",
        {
            "provider": "claude",
            "name": WORKER,
            "headless": True,
            "cwd": work_dir(node),
            "command": [sys.executable, str(FAKE_CLAUDE)],
        },
    )


def p2_params(operation_id: str, harness: str = "pi") -> dict:
    return {
        "operationId": operation_id,
        "name": WORKER,
        "provider": harness,
        "targetNode": TARGET_NODE,
        "targetActor": WORKER,
        "planDigest": PLAN_DIGEST,
    }


def worker_spec(node, harness: str = "pi") -> dict:
    return HarnessLaunchSpec(
        harness=harness, name=WORKER, headless=True, cwd=work_dir(node),
        command=(sys.executable, str(fake_for(harness))),
    ).to_json()


def work_dir(node) -> str:
    """A cwd beside the agent home, never over it, inside a throwaway repo.

    The identity owner refuses a launch whose config directory and cwd contain
    (or alias) one another, and a landed home lives under ``home/agents``; the
    worker's working directory is therefore its own sibling.  It is also a Git
    repository on purpose: a landed worker's runtime context resolves to the P2
    SDK bridge, whose trust receipt is "cwd is inside an approved repository".
    """

    directory = node.root / "work"
    directory.mkdir(exist_ok=True)
    if not (directory / ".git").exists():
        subprocess.run(["git", "init", "-q", str(directory)], check=True)
    return str(directory)


def neighbour_spec(node) -> dict:
    """A spec for a harness the proven row does NOT name."""

    return HarnessLaunchSpec(
        harness=NEIGHBOUR, name=WORKER, headless=True, cwd=work_dir(node),
        command=(sys.executable, str(FAKE_PI)),
    ).to_json()


def source_admission(node, operation_id: str):
    """Read this daemon's own P2 ledger directly (the witness, not the report)."""

    with sqlite3.connect(node.state / "lifecycle-operations.sqlite3") as conn:
        return conn.execute(
            "SELECT operation_id, actor, phase, step, generation "
            "FROM transfer_p2_steps WHERE operation_id=?",
            (operation_id,),
        ).fetchone()


def running(node, name: str = WORKER) -> bool:
    """Is the worker live on this daemon?  Absent counts as not running."""

    connector = connectors(node).get(name)
    return bool(connector is not None and connector["running"])


def wait_running(node, name: str = WORKER, *, timeout: float = 30.0) -> bool:
    """Give a just-launched worker its moment; the witness is still ``ps``."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running(node, name):
            return True
        time.sleep(0.2)
    return False


def entity_token(node, name: str = WORKER) -> str:
    with sqlite3.connect(node.state / "agents.sqlite3") as conn:
        row = conn.execute(
            "SELECT entity_token FROM agents WHERE actor=?", (name,)
        ).fetchone()
    assert row is not None, f"{name!r} has no identity row on this daemon"
    return row[0]


def agent_home(node, name: str = WORKER) -> Path:
    return node.home / "agents" / name


def ship(node, peer, bundle: Path, harness: str = "pi") -> None:
    """The quiesced home gains one honest config artifact, then it is exported.

    A real agent home carries config artifacts; the protocol double creates
    none, and a landing refuses a payload that is only the custody receipt, so
    the run gives the quiesced home the config file the harness would own.

    The claude arm additionally carries the placeholder the launch auth gate
    requires: a 0600 regular ``secrets/native/claude/.credentials.json``.  It
    is NOT a credential and the reading says so -- the vendor identity is
    NOT_RUN (TC012 BLOCKED); it only satisfies
    ``validate_claude_auth_environment`` so the target launch can be read.  The
    landing preserves the mode, so the target sees the same 0600 file.

    The export is also where attribution is stamped: the source machine signs
    the sealed digest with its own key (never a key inside the payload), and
    the target's own home is told the public half -- the two halves a real
    operator drives before the bundle can land at all.
    """

    config_item = agent_home(node) / "config" / "native" / harness / "settings.json"
    config_item.parent.mkdir(parents=True, exist_ok=True)
    config_item.write_text(
        json.dumps({"harness": harness, "sessionRef": f"test-{WORKER}"})
    )
    if harness == "claude":
        credentials = (
            agent_home(node) / "secrets" / "native" / "claude" / ".credentials.json"
        )
        credentials.parent.mkdir(parents=True, exist_ok=True)
        # The P2 runtime owner requires every directory between the home and
        # the native root to be a real 0700 directory; the bundle carries the
        # modes, so the target read sees the same.  A 0755 placeholder would
        # be refused as "not private" before any launch.
        for directory in (
            agent_home(node) / "secrets",
            agent_home(node) / "secrets" / "native",
            credentials.parent,
        ):
            os.chmod(directory, 0o700)
        credentials.write_text(json.dumps({"placeholder": "c3-claude-first-proof"}))
        os.chmod(credentials, 0o600)
    export_bundle(
        agent_home(node),
        bundle,
        owner=OWNER,
        actor=WORKER,
        source_machine=SOURCE_NODE,
        created_at=datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        source_epoch=entity_token(node),
    )
    sign_bundle_for_target(
        bundle,
        actor=WORKER,
        source_machine=SOURCE_NODE,
        source_home=node.home,
        target_home=peer.home,
    )


def test_the_source_half_is_recorded_and_the_worker_stops(source_node, harness) -> None:
    """Positive arm: the chain opens on a tree whose table proves this harness."""

    bring_up_worker(source_node, harness)
    assert wait_running(source_node) is True

    answer = source_node.ipc("transfer.quiesce", p2_params("at07-op-1", harness))

    assert answer["stopped"] is True
    assert answer["chain"]["operationId"] == "at07-op-1"
    assert answer["chain"]["targetNode"] == TARGET_NODE
    assert answer["chain"]["targetActor"] == WORKER
    assert answer["chain"]["planDigest"] == PLAN_DIGEST
    assert answer["chain"]["quiesce"]["state"] == "quiesced"
    # The one fact the target's admission binds travels back to the caller --
    # and it is this daemon's own registry row, read here from the daemon's
    # durable identity, not a value the caller could have supplied.
    assert answer["chain"]["sourceEntityToken"] == entity_token(source_node)
    assert running(source_node) is False
    row = source_admission(source_node, "at07-op-1")
    assert row is not None and row[2] == "quiesced" and row[3] == "quiesce"


def test_an_unproven_daemon_refuses_with_the_p0_code_and_mode(
    unproven_node, harness
) -> None:
    """Negative arm: without the row the capability stays closed.

    The tree is the run tree with the proven row deleted, so the only thing
    separating admit from refuse is that row.  Both entries of one fence speak
    the same refusal, and the target entry says it *before* it looks at a
    bundle -- the `bundleDir` here does not exist.  Parameterized over the
    harnesses the table now proves, so the closed table refuses both of them:
    a row is what opens a harness, not the harness's own name.
    """

    bring_up_worker(unproven_node, harness)
    assert wait_running(unproven_node) is True

    document = ipc_raw(
        unproven_node, "transfer.quiesce", p2_params("at07-op-closed", harness)
    )

    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "TRANSFER_UNSUPPORTED_HARNESS"
    assert error["data"]["mode"] == "agent-home-p2"
    assert error["data"]["harness"] == harness
    # Refused before anything: the worker never stopped, no row exists.
    assert running(unproven_node) is True
    assert source_admission(unproven_node, "at07-op-closed") is None

    document = ipc_raw(
        unproven_node,
        "transfer.receive",
        {
            "operationId": "at07-op-closed-target",
            "planDigest": PLAN_DIGEST,
            "sourceNode": SOURCE_NODE,
            "sourceActor": WORKER,
            "sourceEntityToken": "0" * 64,
            "targetActor": WORKER,
            "bundleDir": str(unproven_node.root / "no-such-bundle"),
            "spec": worker_spec(unproven_node, harness),
        },
    )

    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "TRANSFER_UNSUPPORTED_HARNESS"
    assert error["data"]["mode"] == "agent-home-p2"
    # And nothing landed: the fence spoke before the bundle was ever read, so
    # this daemon's own ledger has no row for the operation (the worker's home
    # above is the *source-side* agent this daemon restored, not a landing).
    assert source_admission(unproven_node, "at07-op-closed-target") is None


def test_a_neighbouring_unproven_harness_still_refuses(neighbour_node) -> None:
    """The harness axis, isolated from the row axis the fence arm moves.

    This tree is the run tree with one entry deleted -- the neighbour's own
    row -- so the table still proves the other harnesses.  The only difference
    here is the harness whose chain nobody ran, and the target entry must
    speak the same refusal -- with the same code and mode string -- before it
    ever reads a bundle.
    """

    neighbour_node.start_daemon()

    document = ipc_raw(
        neighbour_node,
        "transfer.receive",
        {
            "operationId": "at07-op-neighbour",
            "planDigest": PLAN_DIGEST,
            "sourceNode": SOURCE_NODE,
            "sourceActor": WORKER,
            "sourceEntityToken": "0" * 64,
            "targetActor": WORKER,
            "bundleDir": str(neighbour_node.root / "no-such-bundle"),
            "spec": neighbour_spec(neighbour_node),
        },
    )

    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "TRANSFER_UNSUPPORTED_HARNESS"
    assert error["data"]["mode"] == "agent-home-p2"
    assert error["data"]["harness"] == NEIGHBOUR
    # Refused before anything landed: this daemon's own ledger has no row.
    assert source_admission(neighbour_node, "at07-op-neighbour") is None


def test_the_chain_lands_on_the_target_and_retires_the_source(
    source_node, target_node, tmp_path, harness
) -> None:
    """The whole chain, driven across two real daemons, step by step."""

    operation = "at07-op-chain"
    bring_up_worker(source_node, harness)
    assert wait_running(source_node) is True
    token = entity_token(source_node)

    # 1. the source records its half and stops the worker through the hold.
    quiesce = source_node.ipc("transfer.quiesce", p2_params(operation, harness))
    assert quiesce["stopped"] is True
    assert quiesce["chain"]["quiesce"]["state"] == "quiesced"
    assert running(source_node) is False

    # 2. the window the chain is built for: the source daemon dies with the
    #    hold armed.  No receipt is written for this; the witness is the row.
    source_node.stop_daemon()

    bundle = tmp_path / "at07-bundle"
    ship(source_node, target_node, bundle, harness)

    # 3. the target admits its half, lands the bundle and binds the context.
    target_node.start_daemon()
    received = target_node.ipc(
        "transfer.receive",
        {
            "operationId": operation,
            "planDigest": PLAN_DIGEST,
            "sourceNode": SOURCE_NODE,
            "sourceActor": WORKER,
            "sourceEntityToken": token,
            "targetActor": WORKER,
            "bundleDir": str(bundle),
            "spec": worker_spec(target_node, harness),
        },
    )
    assert received["ok"] is True
    assert received["phases"] == ["landed", "active"]
    assert received["actor"] == WORKER
    assert received["entityToken"] != token
    report = received["chain"]["report"]
    assert report["operationId"] == operation

    # 4. the source comes back: the hold keeps it down, and its own half is
    #    still the row it wrote, not a retirement.  Both daemons are now alive,
    #    so the isolation this run claims is read from the daemons themselves:
    #    two processes, two sockets, two node ids.
    source_node.start_daemon()
    source_ping = source_node.ipc("ping", {})
    target_ping = target_node.ipc("ping", {})
    assert (source_ping["nodeId"], target_ping["nodeId"]) == (SOURCE_NODE, TARGET_NODE)
    assert source_ping["pid"] != target_ping["pid"]
    assert source_ping["socket"] != target_ping["socket"]
    assert running(source_node) is False
    row = source_admission(source_node, operation)
    assert row is not None and row[2] == "quiesced"
    assert entity_token(source_node) == token

    # 5. completion on the target's own report, on this daemon's own rules.
    completed = source_node.ipc(
        "transfer.complete",
        {
            "name": WORKER,
            "provider": harness,
            "operationId": operation,
            "chain": {"report": report},
        },
    )
    assert completed["removedAgent"] is True
    assert completed["chain"]["action"] == "complete"
    assert completed["chain"]["targetPhase"] == "active"

    # 6. the landed context is the P2 SDK bridge, not the legacy home.  For pi
    #    the bridge checks an approved Git project, so the same launch without
    #    the explicit approval is refused by name, and nothing runs -- the arm
    #    fails for the reason it is supposed to fail for.  That gate is a pi
    #    property, not a chain property, so the claude arm reads only the
    #    anti-dual-active half and the reading says so.
    if harness == "pi":
        refusal = ipc_raw(
            target_node,
            "lifecycle.start",
            {
                "provider": "pi",
                "name": WORKER,
                "headless": True,
                "cwd": work_dir(target_node),
                "command": [sys.executable, str(FAKE_PI)],
            },
        )
        error = refusal.get("error")
        assert isinstance(error, dict), refusal
        # The trust gate is synchronous now: the daemon refuses the launch
        # itself, with the typed code, before any process is spawned.
        assert error.get("code") == "PI_P2_LAUNCH_UNTRUSTED", refusal
        assert "explicit --approve trust" in str(error.get("message")), refusal
        assert running(source_node) is False

    # 7. the anti-dual-active read, from the two monitors and not a receipt:
    #    after the target's worker is launched, exactly one side runs it.  The
    #    pi arm launches the real pi installation so it exercises the P2 SDK
    #    bridge (a protocol double has no adjacent SDK and must fail closed);
    #    it asserts only launch -> running -> source not running, never a model.
    if harness == "pi":
        if NODE is None or PI_ROOT is None:
            pytest.skip(
                "the real node and installed Pi SDK are required for the pi "
                "positive arm"
            )
        positive_command = [str(PI)]
    else:
        positive_command = [sys.executable, str(fake_for(harness))]
    target_node.ipc(
        "lifecycle.start",
        {
            "provider": harness,
            "name": WORKER,
            "headless": True,
            "cwd": work_dir(target_node),
            "args": ["--approve"],
            "command": positive_command,
        },
    )
    assert wait_running(target_node), connectors(target_node)
    assert running(source_node) is False


def test_a_tampered_bundle_refuses_and_leaves_the_source_held(
    source_node, target_node, tmp_path, harness
) -> None:
    """Mutation: one flipped payload byte ends the chain at the target."""

    operation = "at07-op-tamper"
    seed_worker(source_node, harness=harness)
    source_node.start_daemon()
    source_node.ipc("transfer.quiesce", p2_params(operation, harness))
    source_node.stop_daemon()

    bundle = tmp_path / "at07-tampered"
    ship(source_node, target_node, bundle, harness)
    payload_file = next(
        path for path in sorted((bundle / "payload").rglob("*")) if path.is_file()
    )
    corrupted = bytearray(payload_file.read_bytes())
    corrupted[0] ^= 0xFF
    payload_file.write_bytes(bytes(corrupted))

    target_node.start_daemon()
    document = ipc_raw(
        target_node,
        "transfer.receive",
        {
            "operationId": operation,
            "planDigest": PLAN_DIGEST,
            "sourceNode": SOURCE_NODE,
            "sourceActor": WORKER,
            "sourceEntityToken": entity_token(source_node),
            "targetActor": WORKER,
            "bundleDir": str(bundle),
            "spec": worker_spec(target_node, harness),
        },
    )

    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "TRANSFER_LANDING_FAILED"
    assert not agent_home(target_node).exists()

    # The source is still held, not retired: a second daemon start does not run
    # the worker, and the half is still the quiesce row.
    source_node.start_daemon()
    assert running(source_node) is False
    row = source_admission(source_node, operation)
    assert row is not None and row[2] == "quiesced"
    assert not agent_home(target_node).exists()
