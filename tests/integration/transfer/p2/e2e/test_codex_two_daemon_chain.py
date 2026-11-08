"""C3-S1-C: the P2 chain driven across two real daemons for ``codex``.

The structure is the AT07 I4/I5 shape with the pi worker replaced by the
repository's codex app-server double (``fake_codex_app_server.py``).  Every
other piece is the product: two ``hyprial daemon run`` subprocesses with their
own homes, states, sockets and node ids, desired state, the lifecycle manager,
the identity registry and entity tokens, the P2 SQLite ledger, bundle export
and validation, landing into the registry, runtime-context activation, the
custody hold, and the completion decision.

The one disclosure this file inherits from I4 is the model protocol: the codex
*worker* speaks the repository's double.  The target's sandbox smoke is the
product's own codex entry point and therefore runs the machine's real ``codex``
binary (see ``docs/notes/c3-codex-run.txt``); the vendor model credential is
TC012 and stays BLOCKED.

Witnesses are durable artifacts (the daemon's own ledger row, or ``ps``), never
a receipt the chain wrote for itself.
"""

from __future__ import annotations

import os
import sqlite3
import sys

from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
from hyprial.daemon.impl.state_db import StateDatabase
from datetime import UTC, datetime

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.kernel import HarnessLaunchSpec

from tests.support.transfer.signed import sign_bundle_for_target

from .conftest import OWNER, REPO, SOURCE_NODE, TARGET_NODE, WORKER
from .test_two_daemon_chain import (
    PLAN_DIGEST,
    agent_home,
    entity_token,
    ipc_raw,
    p2_params as _pi_p2_params,
    running,
    source_admission,
    wait_running,
    work_dir,
)

# Temporary CI-only mask: the pytest-shard job container has no unprivileged
# user namespaces, so the real codex bwrap sandbox this chain smokes cannot
# start there. Only that job sets the flag (gate-python.yml); everywhere else this
# file runs and fails loudly. Removal is tracked by the re-enable WIP PR.
_CI_NO_USERNS_MASK = pytest.mark.skipif(
    os.environ.get("HYPRIAL_CI_NO_USERNS_CODEX_SANDBOX") == "1",
    reason="TEMPORARY CI MASK: job container lacks unprivileged user namespaces for the codex bwrap sandbox",
)

CODEX = "codex"
FAKE_CODEX = REPO / "tests/fixtures/harnesses/fake_codex_app_server.py"


def seed_codex_worker(node, name: str = WORKER, *, session_ref: str | None = None) -> None:
    """Persist a codex launch spec whose only double is the model protocol."""

    assert FAKE_CODEX.is_file(), "test fixture missing, not a transfer product failure"
    store = DesiredStateStore(
        node.state / "desired-state.json",
        state_db=StateDatabase(node.state / "lifecycle-operations.sqlite3"),
    )
    store.save(
        DesiredState(
            harnesses=(
                HarnessLaunchSpec(
                    harness=CODEX, name=name, headless=True, cwd=str(node.root),
                    session_ref=session_ref or f"test-{name}",
                    command=(sys.executable, str(FAKE_CODEX)),
                ),
            )
        )
    )


def codex_p2_params(operation_id: str) -> dict:
    params = _pi_p2_params(operation_id)
    params["provider"] = CODEX
    return params


def codex_worker_spec(node) -> dict:
    return HarnessLaunchSpec(
        harness=CODEX, name=WORKER, headless=True, cwd=work_dir(node),
        command=(sys.executable, str(FAKE_CODEX)),
    ).to_json()


def ship_codex(node, peer, bundle: Path) -> None:
    """The quiesced home gains one honest codex config artifact, then it ships.

    ``build_native_projection`` maps ``config/native/<harness>/<rel>`` to
    ``<rel>`` in the provider-native root, and a landing refuses a payload that
    is only the custody receipt, so the run gives the quiesced home the config
    file the harness would own (``$CODEX_HOME/config.toml``).
    """

    config_item = agent_home(node) / "config" / "native" / "codex" / "config.toml"
    config_item.parent.mkdir(parents=True, exist_ok=True)
    # ``ephemeral`` keeps this first proof credential-free exactly like I4: the
    # model credential half is TC012 and stays BLOCKED, and the app-server
    # double needs no account.  The P2 launch still validates the store name.
    config_item.write_text(
        'model = "gpt-5.6-sol"\ncli_auth_credentials_store = "ephemeral"\n'
    )
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


def codex_receive_params(node) -> dict:
    return {
        "operationId": "c3-codex-op",
        "planDigest": PLAN_DIGEST,
        "sourceNode": SOURCE_NODE,
        "sourceActor": WORKER,
        "sourceEntityToken": "",
        "targetActor": WORKER,
        "bundleDir": "",
        "spec": codex_worker_spec(node),
    }


def _rows(node, operation: str) -> list[tuple]:
    with sqlite3.connect(node.state / "lifecycle-operations.sqlite3") as conn:
        return conn.execute(
            "SELECT operation_id, actor, phase, step, generation "
            "FROM transfer_p2_steps WHERE operation_id=? ORDER BY generation",
            (operation,),
        ).fetchall()


def _up(node) -> None:
    if node.daemon is None:
        node.start_daemon()


def _open_chain(source, operation: str) -> str:
    seed_codex_worker(source)
    _up(source)
    assert running(source) is True
    token = entity_token(source)
    quiesce = source.ipc("transfer.quiesce", codex_p2_params(operation))
    assert quiesce["stopped"] is True
    assert quiesce["chain"]["quiesce"]["state"] == "quiesced"
    return token


def _receive(target, source, operation: str, bundle: Path) -> dict:
    _up(target)
    params = codex_receive_params(target)
    params.update(
        {
            "operationId": operation,
            "sourceEntityToken": entity_token(source),
            "bundleDir": str(bundle),
        }
    )
    received = target.ipc("transfer.receive", params)
    assert received["ok"] is True
    return received


def _complete(source, operation: str, report: dict) -> dict:
    _up(source)
    return source.ipc(
        "transfer.complete",
        {
            "name": WORKER,
            "provider": CODEX,
            "operationId": operation,
            "chain": {"report": report},
        },
    )


def test_codex_source_half_is_recorded_and_the_worker_stops(source_node) -> None:
    """Positive arm: the chain opens on a tree whose table proves codex."""

    seed_codex_worker(source_node)
    source_node.start_daemon()
    assert running(source_node) is True

    answer = source_node.ipc("transfer.quiesce", codex_p2_params("c3-codex-op-1"))

    assert answer["stopped"] is True
    assert answer["chain"]["operationId"] == "c3-codex-op-1"
    assert answer["chain"]["sourceEntityToken"] == entity_token(source_node)
    assert running(source_node) is False
    row = source_admission(source_node, "c3-codex-op-1")
    assert row is not None and row[2] == "quiesced" and row[3] == "quiesce"


def test_codex_unproven_tree_refuses_both_entries(unproven_node) -> None:
    """Negative arm: with the codex row deleted the fence still speaks.

    The tree is the run tree with every proven row removed; the only thing
    standing between admit and refuse is the table.  Both entries of the fence
    speak the P0 code and mode, and the target entry says it *before* reading a
    bundle (``bundleDir`` does not exist).
    """

    seed_codex_worker(unproven_node)
    unproven_node.start_daemon()
    assert running(unproven_node) is True

    document = ipc_raw(
        unproven_node, "transfer.quiesce", codex_p2_params("c3-codex-op-closed")
    )
    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "TRANSFER_UNSUPPORTED_HARNESS"
    assert error["data"]["mode"] == "agent-home-p2"
    assert error["data"]["harness"] == CODEX
    assert running(unproven_node) is True
    assert source_admission(unproven_node, "c3-codex-op-closed") is None

    params = codex_receive_params(unproven_node)
    params.update(
        {
            "operationId": "c3-codex-op-closed-target",
            "sourceEntityToken": "0" * 64,
            "bundleDir": str(unproven_node.root / "no-such-bundle"),
        }
    )
    document = ipc_raw(unproven_node, "transfer.receive", params)
    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "TRANSFER_UNSUPPORTED_HARNESS"
    assert error["data"]["harness"] == CODEX
    assert source_admission(unproven_node, "c3-codex-op-closed-target") is None


@_CI_NO_USERNS_MASK
def test_codex_chain_lands_on_the_target_and_retires_the_source(
    source_node, target_node, tmp_path
) -> None:
    """The whole chain, driven across two real daemons, step by step."""

    operation = "c3-codex-op-chain"
    seed_codex_worker(source_node)
    source_node.start_daemon()
    assert running(source_node) is True
    token = entity_token(source_node)

    # 1. the source records its half and stops the worker through the hold.
    quiesce = source_node.ipc("transfer.quiesce", codex_p2_params(operation))
    assert quiesce["stopped"] is True
    assert quiesce["chain"]["quiesce"]["state"] == "quiesced"
    assert running(source_node) is False

    # 2. the window the chain is built for: the source daemon dies with the
    #    hold armed.  No receipt is written for this; the witness is the row.
    source_node.stop_daemon()

    bundle = tmp_path / "c3-codex-bundle"
    ship_codex(source_node, target_node, bundle)

    # 3. the target admits its half, lands the bundle and binds the context.
    target_node.start_daemon()
    received = _receive(target_node, source_node, operation, bundle)
    assert received["phases"] == ["landed", "active"]
    assert received["entityToken"] != token
    report = received["chain"]["report"]
    assert report["operationId"] == operation

    # 4. the source comes back: the hold keeps it down, and both daemons read
    #    as two processes, two sockets, two node ids.
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
    completed = _complete(source_node, operation, report)
    assert completed["removedAgent"] is True
    assert completed["chain"]["action"] == "complete"
    assert completed["chain"]["targetPhase"] == "active"

    # 6. the anti-dual-active read, from the two monitors and not a receipt:
    #    after the target's worker is launched, exactly one side runs it.
    target_node.ipc(
        "lifecycle.start",
        {
            "provider": CODEX,
            "name": WORKER,
            "headless": True,
            "cwd": work_dir(target_node),
            "command": [sys.executable, str(FAKE_CODEX)],
        },
    )
    assert wait_running(target_node, timeout=45.0), target_node.ipc("ps", {})
    assert running(source_node) is False


@_CI_NO_USERNS_MASK
def test_codex_tampered_bundle_refuses_and_leaves_the_source_held(
    source_node, target_node, tmp_path
) -> None:
    """Mutation: one flipped payload byte ends the chain at the target."""

    operation = "c3-codex-op-tamper"
    seed_codex_worker(source_node)
    source_node.start_daemon()
    source_node.ipc("transfer.quiesce", codex_p2_params(operation))
    source_node.stop_daemon()

    bundle = tmp_path / "c3-codex-tampered"
    ship_codex(source_node, target_node, bundle)
    payload_file = next(
        path for path in sorted((bundle / "payload").rglob("*")) if path.is_file()
    )
    corrupted = bytearray(payload_file.read_bytes())
    corrupted[0] ^= 0xFF
    payload_file.write_bytes(bytes(corrupted))

    target_node.start_daemon()
    params = codex_receive_params(target_node)
    params.update(
        {
            "operationId": operation,
            "sourceEntityToken": entity_token(source_node),
            "bundleDir": str(bundle),
        }
    )
    document = ipc_raw(target_node, "transfer.receive", params)

    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "TRANSFER_LANDING_FAILED"
    assert not agent_home(target_node).exists()

    source_node.start_daemon()
    assert running(source_node) is False
    row = source_admission(source_node, operation)
    assert row is not None and row[2] == "quiesced"
    assert not agent_home(target_node).exists()


# ---------------------------------------------------------------------------
# the call-boundary crash windows (I5 shape, on the codex tree)


def _assert_receipt_once(source, target, operation: str) -> None:
    assert _rows(source, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ], "the source wrote a second row for one operation"
    assert _rows(target, operation) == [
        (operation, WORKER, "landed", "land", 1),
        (operation, WORKER, "active", "activate", 2),
    ], "the target wrote a second row for one landing"


def _refusal(node, method: str, params: dict) -> dict:
    document = ipc_raw(node, method, params)
    error = document.get("error")
    assert isinstance(error, dict), document
    return error


def _assert_first_step_not_re_enterable(source, operation: str) -> None:
    _up(source)
    error = _refusal(source, "transfer.quiesce", codex_p2_params(operation))
    assert error["code"] == "TRANSFER_WORKER_NOT_FOUND", error
    assert _rows(source, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ], "a refused re-entry still wrote a row"


@_CI_NO_USERNS_MASK
def test_codex_w2_w3_the_export_boundary_re_enters_after_the_first_step(
    source_node, target_node, tmp_path
) -> None:
    """W2/W3 collapsed: the export leaves a bundle and an empty target ledger."""

    operation = "c3-codex-w2w3"
    token = _open_chain(source_node, operation)
    source_node.stop_daemon()
    bundle = tmp_path / "c3-codex-window-bundle"
    ship_codex(source_node, target_node, bundle)

    assert _rows(source_node, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ]
    target_node.start_daemon()
    assert _rows(target_node, operation) == []
    assert entity_token(source_node) == token
    assert running(target_node) is False

    _assert_first_step_not_re_enterable(source_node, operation)

    again = tmp_path / "c3-codex-window-bundle-again"
    ship_codex(source_node, target_node, again)
    received = _receive(target_node, source_node, operation, again)
    assert received["replayed"] is False
    completed = _complete(source_node, operation, received["chain"]["report"])

    assert completed["removedAgent"] is True
    assert completed["chain"]["action"] == "complete"
    _assert_receipt_once(source_node, target_node, operation)
    assert running(source_node) is False
    assert running(target_node) is False


@_CI_NO_USERNS_MASK
def test_codex_w5_after_the_target_answered_both_daemons_restart_and_the_report_holds(
    source_node, target_node, tmp_path
) -> None:
    """The window covered by the response body -- read again on the codex tree."""

    operation = "c3-codex-w5"
    token = _open_chain(source_node, operation)
    source_node.stop_daemon()
    bundle = tmp_path / "c3-codex-window-bundle"
    ship_codex(source_node, target_node, bundle)
    first = _receive(target_node, source_node, operation, bundle)
    report = first["chain"]["report"]
    assert first["replayed"] is False

    target_node.stop_daemon()

    again = _receive(target_node, source_node, operation, bundle)
    assert again["replayed"] is True
    assert again["chain"]["report"] == report
    assert again["entityToken"] == first["entityToken"]

    assert _rows(source_node, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ]
    _assert_receipt_once(source_node, target_node, operation)
    assert entity_token(source_node) == token
    _up(source_node)
    assert running(source_node) is False
    assert running(target_node) is False


@_CI_NO_USERNS_MASK
def test_codex_w6_before_the_complete_the_chain_closes_on_the_report(
    source_node, target_node, tmp_path
) -> None:
    """The orchestrator died one IPC short of the end; nothing was retired."""

    operation = "c3-codex-w6"
    token = _open_chain(source_node, operation)
    source_node.stop_daemon()
    bundle = tmp_path / "c3-codex-window-bundle"
    ship_codex(source_node, target_node, bundle)
    received = _receive(target_node, source_node, operation, bundle)

    assert _rows(source_node, operation) == [
        (operation, WORKER, "quiesced", "quiesce", 1)
    ]
    assert _rows(target_node, operation) == [
        (operation, WORKER, "landed", "land", 1),
        (operation, WORKER, "active", "activate", 2),
    ]
    assert entity_token(source_node) == token
    assert entity_token(target_node) != token
    _up(source_node)
    assert running(source_node) is False
    assert running(target_node) is False

    _assert_first_step_not_re_enterable(source_node, operation)
    again = tmp_path / "c3-codex-window-bundle-again"
    ship_codex(source_node, target_node, again)
    served = _receive(target_node, source_node, operation, again)
    assert served["replayed"] is True
    assert served["chain"]["report"] == received["chain"]["report"]

    completed = _complete(source_node, operation, served["chain"]["report"])
    assert completed["removedAgent"] is True
    assert completed["chain"]["action"] == "complete"
    _assert_receipt_once(source_node, target_node, operation)
    assert running(source_node) is False
    assert running(target_node) is False
