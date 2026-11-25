# Decision record (UA1b user agent in the hosted scenarios, 2026-11-18):
# - Every daemon with an owner creates that owner's user agent (``hyprial-user``,
#   no harness) on its own thread after ``daemon.ready``.  The AT01/AT04
#   registry snapshots taken right after a start therefore race with it: the
#   snapshot could see no user agent, or one.
# - The scenarios wait for the ensure to settle (the ``userAgent`` block of
#   ``ps`` is ``present`` after at least one attempt), then split the snapshot:
#   the rest must still be exactly the P4 rewrite, and the user agent's own
#   part (one agents row, its agent-record/agent-home resources, files under
#   its home) is checked for its identity, so "nothing else changed" keeps its
#   strength.  ``after_p4`` itself stays the pure P4 statement.
# - A build whose ``ps`` has no ``userAgent`` block (the old tree of AT04)
#   creates no user agent: the snapshot is returned whole.
# - ``request`` is the one daemon socket call of these scenarios: AT01's
#   ``ping`` and this module's ``ps`` both use it (UA1g, so run-hosted-registry.py
#   keeps a plain statement per line under the 1000-line limit).
"""Wait for the daemon-made user agent and split it out of a registry snapshot."""

from __future__ import annotations

import json
import socket
import time
import uuid

USER_AGENT_NAME = "hyprial-user"
SETTLE_SECONDS = 30.0


def request(path, method, timeout=10):
    """The ``result`` of one parameterless *method* over the daemon socket at *path*."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        message = {"version": 1, "id": uuid.uuid4().hex, "method": method, "params": {}}
        client.sendall(json.dumps(message).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            part = client.recv(65536)
            if not part:
                raise OSError(f"closed before {method} response")
            data += part
        return json.loads(data.split(b"\n")[0])["result"]


def ps(path):
    return request(path, "ps")


def wait_user_agent(status, timeout=SETTLE_SECONDS, sleep=time.sleep, clock=time.monotonic):
    """The settled ``userAgent`` block of ``status()``, or None for a build without one."""
    deadline = clock() + timeout
    while True:
        result = status()
        if "userAgent" not in result:
            return None
        block = result["userAgent"]
        if block.get("state") == "present" and int(block.get("attempts") or 0) >= 1:
            return block
        if block.get("state") not in ("pending", "present"):
            raise AssertionError(f"the daemon did not create its user agent: {block}")
        if clock() >= deadline:
            raise AssertionError(f"the user agent did not settle within {timeout:g}s: {block}")
        sleep(0.1)


def split_user_agent(snap):
    """(``snap`` without the user agent, the user agent's own rows and files)."""
    at = {name: i for i, name in enumerate(snap["columns"]["agents"])}
    rows = snap["rows"]
    agents = [row for row in rows["agents"] if row[at["actor"]] == USER_AGENT_NAME]
    keys = {f"agent-record:{USER_AGENT_NAME}", f"agent-home:{USER_AGENT_NAME}",
            *(f"binding:{row[at['uri']]}" for row in agents)}
    resources = [row for row in rows["lifecycle_resources"] if row[0] in keys]
    prefix = f"home/agents/{USER_AGENT_NAME}/"
    own = {"columns": snap["columns"]["agents"], "agents": agents, "lifecycle_resources": resources,
           "files": {path: digest for path, digest in snap["files"].items() if path.startswith(prefix)}}
    rest = dict(snap, rows=dict(
        rows,
        agents=[row for row in rows["agents"] if row not in agents],
        lifecycle_resources=[row for row in rows["lifecycle_resources"] if row not in resources],
    ), files={path: digest for path, digest in snap["files"].items() if path not in own["files"]})
    return rest, own


def check_user_agent(own, owner, machine, agent_id_for):
    """The split-out part is exactly one user agent of ``owner`` on ``machine``."""
    assert len(own["agents"]) == 1, own
    row = dict(zip(own["columns"], own["agents"][0]))
    assert (row["actor"], row["owner"], row["machine"]) == (USER_AGENT_NAME, owner, machine), row
    assert row["uri"] == f"agent:{owner}:{machine}:{agent_id_for(row['entity_token'])}", row
    assert json.loads(row["capabilities"]) == {"template": "user"}, row
    assert row["hosted_by"] is None and row["provider"] is None and row["cwd"] is None, row
    keys = {resource[0] for resource in own["lifecycle_resources"]}
    assert {f"agent-record:{USER_AGENT_NAME}", f"agent-home:{USER_AGENT_NAME}"} <= keys, keys


def settled_snapshot(path, snapshot, root, owner, machine, agent_id_for):
    """``snapshot(root)`` after the user agent settled, with the user agent split out and checked."""
    if wait_user_agent(lambda: ps(path)) is None:
        return snapshot(root)
    rest, own = split_user_agent(snapshot(root))
    check_user_agent(own, owner, machine, agent_id_for)
    return rest
