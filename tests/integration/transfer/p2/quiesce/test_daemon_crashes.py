"""TC014 source crash windows using real daemon/lifecycle and fake model RPC.

Test-only admission drives the internal helper for the crash windows. The
override retention case drives the real public ``transfer.custody.override``
IPC; it is still not the full target-side TC015 orchestration.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
import json
from pathlib import Path
import select
import shutil
import socket
import sqlite3
import sys
import time
import uuid

import pytest

from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.transfer.p2.model import Binding, Role
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore
from hyprial.kernel import HarnessLaunchSpec

from ..source import conftest as daemon_fixtures
from ..source.conftest import NODE, OWNER, seed_workers, SOURCE, CONTROL

real_daemon = daemon_fixtures.real_daemon


def _ipc_document(h, method, params):
    """Raw request so a typed rejection keeps its error code."""

    frame = json.dumps({"version": 1, "id": uuid.uuid4().hex, "method": method,
                        "params": params}, separators=(",", ":")).encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(15.0)
    try:
        client.connect(str(h.socket_path))
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


def _source_spec(h, name=SOURCE):
    """A fresh explicit start carrying no session ref.

    ``lifecycle.start`` resolves an explicit ``sessionRef`` before the custody
    fence, so a resume request is the wrong admission to probe here: it would
    fail on RESUME_SESSION_NOT_FOUND instead of proving custody retention.
    A ref-less start reaches the durable custody observation (the resume path
    has its own coverage), which is what this retention assertion means to test.
    """

    fixture = Path(__file__).resolve().parents[4] / "fixtures/harnesses/fake_pi_rpc.py"
    assert fixture.is_file(), "test fixture missing, not a transfer product failure"
    return HarnessLaunchSpec(harness="pi", name=name, headless=True, cwd=str(h.root),
                             command=(sys.executable, str(fixture)))


@pytest.mark.parametrize("phase", ["before-intent", "after-intent", "after-stop", "after-quiesce"])
def test_real_quiesce_crash_does_not_resurrect_owned_source(real_daemon, phase, tmp_path):
    h = real_daemon
    seed_workers(h)
    read_fd, write_fd = os.pipe()
    try:
        h.start_daemon(command=[sys.executable, str(Path(__file__).with_name("daemon_peer.py")),
                                phase, str(write_fd)], pass_fds=(write_fd,))
        os.close(write_fd)
        write_fd = -1
        before = {row["name"]: row for row in h.ipc("ps", {})["connectors"]}
        assert before[SOURCE]["running"] and before[CONTROL]["running"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(h.ipc, "test.p2.quiesce", {})
            try:
                readable, _, _ = select.select([read_fd], [], [], 15)
                if not readable:
                    if call.done():
                        pytest.fail(f"quiesce missed crash barrier: {call.result()}")
                    pytest.fail("real quiesce did not reach the declared crash barrier")
                assert os.read(read_fd, 100) == (phase + "\n").encode()
            finally:
                daemon, h.daemon = h.daemon, None
                daemon.kill()
                daemon.wait(timeout=10)
            with pytest.raises((OSError, AssertionError)):
                call.result(5)
        with closing(sqlite3.connect(h.state / "lifecycle-operations.sqlite3")) as conn:
            row = conn.execute("SELECT phase, receipt FROM transfer_p2_steps "
                               "WHERE operation_id='crash-move'").fetchone()
        assert (row is None) == (phase == "before-intent")
        if row:
            assert row[0] == ("quiesced" if phase == "after-quiesce" else "quiesce-pending")
            assert bool(row[1]) == (phase == "after-quiesce")
        shutil.copyfile(h.root / "daemon.out", tmp_path / "before-restart.log")
        # Restart through the ordinary entry point, with no test-only driver.
        h.start_daemon()
        after = {row["name"]: row for row in h.ipc("ps", {})["connectors"]}
        assert after[CONTROL]["running"], "custody must not block unrelated restore"
        if phase == "before-intent":
            assert after[SOURCE]["running"], "prepared admission alone must not suppress"
        else:
            assert SOURCE not in after or not after[SOURCE]["running"]
        refused = None
        if phase != "before-intent":
            # Restart retention: durable custody keeps suppressing the source
            # at the explicit-start admission too, with a typed refusal.
            document = _ipc_document(h, "lifecycle.start", _source_spec(h).to_json())
            refused = document.get("error")
            assert refused is not None, document
            expected_code = (
                "TRANSFER_STARTS_HELD" if phase in {"after-stop", "after-quiesce"}
                else "TRANSFER_CUSTODY_UNAVAILABLE"
            )
            assert refused["code"] == expected_code, refused
            stopped = {row["name"]: row for row in h.ipc("ps", {})["connectors"]}
            assert SOURCE not in stopped or not stopped[SOURCE]["running"]
        reconciled = None
        if phase == "after-stop":
            # The lifecycle stop receipt outlived the crash; ordinary restart
            # must certify it against the durable intent and close the row,
            # without re-executing the accepted effect or starting the source.
            with closing(sqlite3.connect(h.state / "lifecycle-operations.sqlite3")) as conn:
                reconciled = conn.execute(
                    "SELECT phase, receipt FROM transfer_p2_steps "
                    "WHERE operation_id='crash-move'").fetchone()
            assert reconciled == ("quiesced", "transfer-quiesce:crash-move:1"), reconciled
            assert SOURCE not in after or not after[SOURCE]["running"]
        shutil.copyfile(h.root / "daemon.out", tmp_path / "after-restart.log")
        (tmp_path / "observations.json").write_text(json.dumps({
            "phase": phase, "journal": row, "before": before, "after": after,
            "reconciled": reconciled, "startRefused": refused,
            "entry": "test-only trusted internal quiesce driver; ordinary restart",
        }, indent=2) + "\n")
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_real_daemon_rejects_second_source_transfer_while_first_is_pending(real_daemon, tmp_path):
    h = real_daemon
    seed_workers(h)
    read_fd, write_fd = os.pipe()
    try:
        h.start_daemon(command=[sys.executable, str(Path(__file__).with_name("daemon_peer.py")),
                                "before-hold", str(write_fd)], pass_fds=(write_fd,))
        os.close(write_fd)
        write_fd = -1
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(h.ipc, "test.p2.quiesce", {})
            try:
                readable, _, _ = select.select([read_fd], [], [], 15)
                assert readable, "first transfer did not reach its pending effect barrier"
                assert os.read(read_fd, 100) == b"before-hold\n"
                second = h.ipc("test.p2.quiesce", {"operationId": "second-move", "prepareOnly": True})
                assert second == {"rejected": "reserved"}
                assert h.ipc("ping", {})["running"]
                with closing(sqlite3.connect(h.state / "lifecycle-operations.sqlite3")) as conn:
                    rows = conn.execute("SELECT operation_id, phase FROM transfer_p2_steps").fetchall()
                assert rows == [("crash-move", "quiesce-pending")]
                (tmp_path / "observations.json").write_text(json.dumps({
                    "second": second, "journal": rows,
                    "entry": "test-only trusted internal quiesce driver",
                }, indent=2) + "\n")
            finally:
                daemon, h.daemon = h.daemon, None
                daemon.kill()
                daemon.wait(timeout=10)
            with pytest.raises((OSError, AssertionError)):
                first.result(5)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def _running_workers(h):
    return {row["name"]: row for row in h.ipc("ps", {})["connectors"]}


def _wait_running(h, name, *, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        worker = _running_workers(h).get(name)
        if worker is not None and worker.get("running"):
            return True
        time.sleep(0.1)
    return False


SOURCE_SECOND = "transfer-source-2"



def _entity_token(h, actor):
    with closing(sqlite3.connect(h.state / "agents.sqlite3")) as conn:
        row = conn.execute("SELECT entity_token FROM agents WHERE actor=?", (actor,)).fetchone()
    assert row is not None, f"baseline daemon did not create the {actor} identity"
    return row[0]


def _arrange_quiesced_custody(h, actor, token, operation_id, target_actor):
    """One durable quiesced custody row, arranged while the daemon is stopped."""

    store = AdmissionStore(StateDatabase(h.state / "lifecycle-operations.sqlite3"),
                           node=NODE, owner=OWNER)
    store.prepare(operation_id, Binding(Role.SOURCE, OWNER, NODE, actor, token,
                                        "isolated-target", target_actor, "a" * 64))
    start = store.journal.begin(operation_id, step="quiesce", fingerprint="b" * 64,
                                actor=actor)
    return store.journal.finish(operation_id, expected=start.state,
                                receipt=f"arranged-stop-{operation_id}",
                                entity_token=token)


def test_real_daemon_override_releases_one_hold_and_restart_keeps_the_other(
        real_daemon, tmp_path):
    """TC015 shape on a real daemon: a real crashed quiesce leaves one held
    source, an arranged row leaves a second; the owner-authenticated override
    releases exactly one, with a durable audit and durable alert, custody is
    never deleted, and the two sources decide independently across restarts.
    """

    h = real_daemon
    seed_workers(h, extra=(SOURCE_SECOND,))
    h.start_daemon()
    started = {row["name"]: row for row in h.ipc("ps", {})["connectors"]}
    assert (started[SOURCE]["running"] and started[SOURCE_SECOND]["running"]
            and started[CONTROL]["running"]), "worker fixture did not start"
    h.stop_daemon()
    second_token = _entity_token(h, SOURCE_SECOND)
    _arrange_quiesced_custody(h, SOURCE_SECOND, second_token, "move-2", "target-2")
    seed_workers(h, extra=(SOURCE_SECOND,))
    read_fd, write_fd = os.pipe()
    try:
        h.start_daemon(command=[sys.executable, str(Path(__file__).with_name("daemon_peer.py")),
                                "after-quiesce", str(write_fd)], pass_fds=(write_fd,))
        os.close(write_fd)
        write_fd = -1
        live = {row["name"]: row for row in h.ipc("ps", {})["connectors"]}
        assert live[SOURCE]["running"] and live[CONTROL]["running"], live
        assert SOURCE_SECOND not in live or not live[SOURCE_SECOND]["running"], live
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(h.ipc, "test.p2.quiesce", {})
            try:
                readable, _, _ = select.select([read_fd], [], [], 15)
                assert readable, "real quiesce did not reach the declared crash barrier"
                assert os.read(read_fd, 100) == b"after-quiesce\n"
            finally:
                daemon, h.daemon = h.daemon, None
                daemon.kill()
                daemon.wait(timeout=10)
            with pytest.raises((OSError, AssertionError)):
                call.result(5)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    h.start_daemon()
    # The real quiesce dropped the source's desired spec; mirror an operator
    # that keeps the source managed while retained custody suppresses it.
    seed_workers(h, extra=(SOURCE_SECOND,))
    after = {row["name"]: row for row in h.ipc("ps", {})["connectors"]}
    assert after[CONTROL]["running"], "custody must not block an unrelated worker"
    for name in (SOURCE, SOURCE_SECOND):
        assert name not in after or not after[name]["running"], after.get(name)
        document = _ipc_document(h, "lifecycle.start", _source_spec(h, name).to_json())
        assert document.get("error") is not None, document
        assert document["error"]["code"] == "TRANSFER_STARTS_HELD", document["error"]
    # Owner-authenticated override: audit, durable alert, owned hold release.
    override = _ipc_document(h, "transfer.custody.override", {
        "principal": OWNER, "actor": SOURCE, "operationId": "crash-move",
        "reason": "operator released the crashed source after review",
    })
    result = override.get("result")
    assert result is not None, override
    assert result["custody"] == "held-overridden" and result["holdReleased"] is True, result
    allowed = _ipc_document(h, "lifecycle.start", _source_spec(h, SOURCE).to_json())
    assert allowed.get("error") is None, allowed
    assert _wait_running(h, SOURCE), "the released source must be allowed to run"
    still_held = _ipc_document(h, "lifecycle.start", _source_spec(h, SOURCE_SECOND).to_json())
    assert still_held.get("error") is not None, still_held
    assert still_held["error"]["code"] == "TRANSFER_STARTS_HELD", still_held["error"]
    with closing(sqlite3.connect(h.state / "lifecycle-operations.sqlite3")) as conn:
        rows = dict((str(row[0]), (row[1], row[2])) for row in conn.execute(
            "SELECT operation_id, phase, needs_attention FROM transfer_p2_steps"))
    assert rows["crash-move"] == ("quiesced", 1), rows
    assert rows["move-2"] == ("quiesced", 0), rows
    with closing(sqlite3.connect(h.state / "inbox.sqlite3")) as conn:
        alerts = conn.execute(
            "SELECT payload FROM system_notices WHERE payload LIKE ?",
            ("%transfer-custody-override%",)).fetchall()
    assert len(alerts) == 1, alerts
    # Reopen: the override is durable, custody is never deleted, and the two
    # sources still decide independently.
    h.stop_daemon()
    seed_workers(h, extra=(SOURCE_SECOND,))
    h.start_daemon()
    reopened = {row["name"]: row for row in h.ipc("ps", {})["connectors"]}
    assert reopened[SOURCE]["running"], "an overridden source is allowed to start"
    assert SOURCE_SECOND not in reopened or not reopened[SOURCE_SECOND]["running"]
    assert reopened[CONTROL]["running"]
    with closing(sqlite3.connect(h.state / "lifecycle-operations.sqlite3")) as conn:
        retained = dict((str(row[0]), (row[1], row[2])) for row in conn.execute(
            "SELECT operation_id, phase, needs_attention FROM transfer_p2_steps"))
    assert retained["crash-move"] == ("quiesced", 1), retained
    assert retained["move-2"] == ("quiesced", 0), retained
    (tmp_path / "observations.json").write_text(json.dumps({
        "override": result, "journal": rows, "alerts": len(alerts),
        "reopened": reopened,
        "entry": "real crashed quiesce + arranged second source; real IPC override; "
                 "ordinary restarts",
    }, indent=2, default=str) + "\n")
