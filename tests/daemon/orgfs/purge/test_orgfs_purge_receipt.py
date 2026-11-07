"""G3: the ordinary purge publishes a safe ``replacements`` receipt.

Real isolated CLI -> canonical ``org fs purge plan`` / ``org fs purge run`` -> local
persisted readback.  Every value asserted here is compared against bytes the
daemon wrote to disk (the ``meta.loro`` retirements and the replacement
snapshot blob), never against the purge's own in-memory state.

Isolation: the CLI fixture owns a temporary ``HOME``, ``HYPRIAL_HOME`` and
``HARNESS_STATE_DIR`` under one test root, unsets ``HYPRIAL_NODE_ID`` (the
daemon mints its node id inside that root) and unsets the worker/tool-profile
bindings.  Nothing ambient survives into either subprocess.

Kill mutations these tests must survive:

* emitting the receipt from the pre-commit plan (no replacement ids yet);
* recomputing the digest from the live document instead of the committed
  snapshot (an edit after the purge would change it);
* reporting a replacement for a purge that never committed;
* re-executing a completed plan on replay (new replacement ids / new snapshot).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from pycrdt import Doc, Map
import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import assert_root_local_environment, isolated_environment

OWNER = "g3-owner"
MEMBER = "user:g3-member"
NODE = "node-g3"
_SOCKET_TIMEOUT_SECONDS = 5.0
_READY_TIMEOUT_SECONDS = 90.0
_CLI_TIMEOUT_SECONDS = 180


class _IsolatedCli:
    """One isolated daemon root plus the real CLI that talks to it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        environment = isolated_environment(root)
        self.home = Path(environment["HOME"])
        self.hyprial_home = root / "hyprial"
        self.state = root / "state"
        for path in (self.home, self.hyprial_home, self.state):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.write_owner(OWNER)
        self.socket_path = self.state / "daemon.sock"
        environment.update({
            "HOME": str(self.home),
            "HYPRIAL_HOME": str(self.hyprial_home),
            "HARNESS_STATE_DIR": str(self.state),
            # Endpoints-less and discovery-free: this run cannot reach, or be
            # reached by, any real mesh.
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "UV_TOOL_DIR": str(root / "uv-tools"),
        })
        # The suite's autouse fixtures supply an identity, a node id and worker
        # bindings for in-process tests; this run's identity comes from its own
        # settings.json and its node id from its own root, so no ambient value
        # may leak into either subprocess.
        for key in (
            "HARNESS_SOCKET_PATH",
            "HYPRIAL_OWNER",
            "HYPRIAL_NODE_ID",
            "HYPRIAL_MANAGED_WORKER",
            "HYPRIAL_WORKER_ACTOR",
            "HYPRIAL_WORKER_SESSION_REF",
            "HYPRIAL_TOOL_PROFILE_ID",
        ):
            environment.pop(key, None)
        # Pin the two invariants the review asked for, so a future edit cannot
        # silently reintroduce an ambient node id or the runner's HOME.
        assert "HYPRIAL_NODE_ID" not in environment
        assert environment["HOME"] == str(self.home)
        assert_root_local_environment(root, environment)
        self.environment = environment
        self.daemon: subprocess.Popen[bytes] | None = None

    def write_owner(self, owner: str) -> None:
        (self.hyprial_home / "settings.json").write_text(
            json.dumps({"version": 1, "owner": owner}), encoding="utf-8"
        )

    def start(self) -> None:
        assert self.daemon is None
        self.daemon = subprocess.Popen(
            [sys.executable, "-m", "hyprial.cli", "daemon", "run"],
            env=self.environment,
            stdout=(self.root / "daemon.out").open("ab"),
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._ready():
                return
            time.sleep(0.2)
        raise AssertionError(
            "daemon did not become ready\n"
            + (self.root / "daemon.out").read_text(errors="replace")[-8000:]
        )

    def stop(self) -> None:
        daemon, self.daemon = self.daemon, None
        assert daemon is not None
        daemon.terminate()
        daemon.wait(timeout=60)

    def close(self) -> None:
        if self.daemon is not None:
            self.stop()

    def _ready(self) -> bool:
        frame = json.dumps(
            {
                "version": 1,
                "id": uuid.uuid4().hex,
                "method": "ping",
                "params": {},
            },
            separators=(",", ":"),
        ).encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(_SOCKET_TIMEOUT_SECONDS)
                client.connect(str(self.socket_path))
                client.sendall(frame + b"\n")
                buffer = bytearray()
                while b"\n" not in buffer:
                    chunk = client.recv(64 * 1024)
                    if not chunk:
                        return False
                    buffer.extend(chunk)
        except OSError:
            return False
        answer = json.loads(buffer.partition(b"\n")[0]).get("result") or {}
        return (
            answer.get("running") is True
            and answer.get("restorePending") is not True
        )

    def run(self, *args: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, "-m", "hyprial.cli", *args, "--json"],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"non-JSON stdout rc={completed.returncode} "
                f"out={completed.stdout!r} err={completed.stderr[-2000:]!r}"
            ) from exc
        assert isinstance(payload, dict)
        return completed.returncode, payload

    def ok(self, *args: str) -> dict:
        code, payload = self.run(*args)
        assert (code, payload.get("ok")) == (0, True), payload
        return payload

    def refused(self, *args: str) -> dict:
        code, payload = self.run(*args)
        assert (code, payload.get("ok")) == (1, False), payload
        return payload

    def write_and_plan(self, space_id: str) -> tuple[dict, dict]:
        node = self.ok("org", "fs", "write", space_id, "note.md", "secret")
        plan = self.ok(
            "org", "fs",
            "purge",
            "plan",
            space_id,
            json.dumps(
                [
                    {
                        "kind": "doc-range",
                        "node": f"id:{node['node_id']}",
                        "from": "all",
                        "to": "all",
                    }
                ]
            ),
        )
        return node, plan


def _durable_retirements(state: Path, space_id: str) -> dict[str, dict]:
    """Read the retirement records out of the persisted meta document."""

    raw = (state / "orgfs" / "spaces" / space_id / "meta.loro").read_bytes()
    document = Doc()
    document.apply_update(raw)
    roots = document.get("retirements", type=Map)
    return dict(roots.to_py()) if roots is not None else {}


def _replacement_snapshot_sha256(state: Path, digest: str) -> str:
    """Recompute the digest of the replacement snapshot bytes on disk."""

    path = state / "orgfs" / "blobs" / digest[:2] / digest
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def cli(tmp_path: Path) -> Iterator[_IsolatedCli]:
    root = Path(tempfile.mkdtemp(prefix="hyprial-g3-", dir=hyprial_test_tmp_root()))
    value = _IsolatedCli(root)
    try:
        yield value
    finally:
        value.close()
        shutil.rmtree(root, ignore_errors=True)


def test_purge_receipt_matches_durable_records_across_replay_and_restart(
    cli: _IsolatedCli,
) -> None:
    cli.start()
    space_id = cli.ok("org", "fs", "space", "create", "team")["space_id"]
    node, plan = cli.write_and_plan(space_id)
    first = cli.ok("org", "fs", "purge", "run", space_id, plan["plan_id"])

    receipt = first["replacements"]
    assert len(receipt) == 1, first
    item = receipt[0]
    assert set(item) == {
        "doc_id",
        "replacement_doc_id",
        "snapshot_id",
        "snapshot_sha256",
    }
    assert item["doc_id"] == node["doc_id"]
    assert item["replacement_doc_id"] != node["doc_id"]
    # Ids and digests only: no content, no paths, no errors.
    encoded = json.dumps(first, sort_keys=True)
    assert "note.md" not in encoded
    assert "secret" not in encoded
    assert len(item["snapshot_sha256"]) == 64
    assert set(item["snapshot_sha256"]) <= set("0123456789abcdef")

    # Replaying the same plan returns the same committed receipt.
    replay = cli.ok("org", "fs", "purge", "run", space_id, plan["plan_id"])
    assert replay["replacements"] == receipt

    # A later edit of the replacement document must not change the receipt: it
    # is derived from committed records, not from the live document bytes.
    edited = cli.ok(
        "org", "fs", "write", space_id, f"id:{node['node_id']}", "edited after purge"
    )
    assert edited["doc_id"] == item["replacement_doc_id"]
    after_edit = cli.ok("org", "fs", "purge", "run", space_id, plan["plan_id"])
    assert after_edit["replacements"] == receipt

    # Local persisted readback, with no daemon running at all.
    cli.stop()
    retirements = _durable_retirements(cli.state, space_id)
    assert retirements[item["doc_id"]] == {
        "replacementDocId": item["replacement_doc_id"],
        "planId": plan["plan_id"],
        "snapshotId": item["snapshot_id"],
        "retiredAt": retirements[item["doc_id"]]["retiredAt"],
    }
    assert (
        _replacement_snapshot_sha256(cli.state, item["snapshot_sha256"])
        == item["snapshot_sha256"]
    )
    assert item["snapshot_id"] == item["snapshot_sha256"]

    # A restarted daemon re-reads the same receipt from the same records.
    cli.start()
    after_restart = cli.ok("org", "fs", "purge", "run", space_id, plan["plan_id"])
    assert after_restart["replacements"] == receipt
    assert after_restart["acknowledged"] == first["acknowledged"]
    assert after_restart["pending"] == first["pending"]


def test_failed_purge_never_reports_a_replacement(cli: _IsolatedCli) -> None:
    cli.start()
    space_id = cli.ok("org", "fs", "space", "create", "team")["space_id"]
    node, plan = cli.write_and_plan(space_id)
    # The document changes after planning, so the purge is refused as stale and
    # commits no retirement at all.
    cli.ok("org", "fs", "write", space_id, f"id:{node['node_id']}", "changed later")
    failure = cli.refused("org", "fs", "purge", "run", space_id, plan["plan_id"])
    assert failure["code"] == "stale-plan"
    assert "replacements" not in failure
    assert "replacements" not in (failure.get("data") or {})

    cli.stop()
    assert _durable_retirements(cli.state, space_id) == {}
    journal = sqlite3.connect(
        f"file:{cli.state / 'orgfs' / 'spaces' / space_id / 'journal.sqlite3'}?mode=ro",
        uri=True,
    )
    try:
        rows = tuple(journal.execute("SELECT doc_id, snapshot_id FROM snapshots"))
    finally:
        journal.close()
    assert all(row[0] != plan["docs"][0]["doc_id"] for row in rows)


def test_real_cli_purge_refuses_foreign_and_non_owner_member(
    cli: _IsolatedCli,
) -> None:
    """The changed method itself, driven through the real CLI, stays owner-only.

    Two principals are exercised on the same space through the same CLI: an
    invited read-only member and a user who was never invited.  Both must be
    refused by ``orgfs.purge`` with no replacement claim and no retirement on
    disk.  The owner's plan was built and accepted before either switch, so the
    refusal is the only reason the purge did not happen.
    """

    cli.start()
    space_id = cli.ok("org", "fs", "space", "create", "team")["space_id"]
    # The member is added before the plan is built: a membership change after
    # planning would rebind the plan's own hash and hide the refusal under
    # `stale-plan`.
    cli.ok(
        "org", "fs",
        "member",
        "invite",
        space_id,
        MEMBER,
        "--mode",
        "ro",
        "--no-notify",
    )
    node, plan = cli.write_and_plan(space_id)
    cli.stop()

    for owner in (MEMBER.removeprefix("user:"), "g3-foreign"):
        cli.write_owner(owner)
        cli.start()
        try:
            refused = cli.refused(
                "org", "fs", "purge", "run", space_id, plan["plan_id"]
            )
        finally:
            cli.stop()
        assert refused["code"] == "not-owner", refused
        assert "replacements" not in refused
        assert "replacements" not in (refused.get("data") or {})
        # Nothing committed for the plan: no replacement can be claimed.
        assert _durable_retirements(cli.state, space_id) == {}

    # The owner keeps the capability: a fresh exact plan still executes and
    # claims one replacement, so the refusals above were about the principal.
    # (A restart rebinds the local writer id, so the pre-switch plan no longer
    # hashes to the same inventory; the owner rebinds before purging.)
    cli.write_owner(OWNER)
    cli.start()
    rebound = cli.ok(
        "org", "fs",
        "purge",
        "plan",
        space_id,
        json.dumps(
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{node['node_id']}",
                    "from": "all",
                    "to": "all",
                }
            ]
        ),
    )
    granted = cli.ok("org", "fs", "purge", "run", space_id, rebound["plan_id"])
    assert len(granted["replacements"]) == 1
    assert granted["replacements"][0]["doc_id"] == node["doc_id"]
