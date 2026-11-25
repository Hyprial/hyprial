# Decisions:
# - Phase D, package T2.  This file was the production-entry twin of
#   ``tests/daemon/orgfs/mesh/replication/blobs/test_blobs.py`` and of the
#   R4a/R4b tests in ``.../presence/test_presence.py``.  The six twins that
#   failed on the state runtime are deleted; their daemon-side copies run on
#   ``OrgFsRuntime`` (R4a, R4b, R5 inbound, the large-update fetch once,
#   the failed fetch), and the update-blob queue test is replaced by
#   ``sync/test_lifecycle.py::test_request_blob_queue_is_bounded`` (update
#   blobs are fetched inside the pull round; see the daemon-side file).
# - ``test_round3_r5_zero_length_holder_reply_fails_bounded`` passes on both
#   runtimes and is unchanged.
"""actor_preserved_orgfs_mesh_rework: blob custody scenarios."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import (
    KeySpace,
    ZenohConfig,
    ZenohTransport,
)

from .support import (
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)


def test_round3_r5_zero_length_holder_reply_fails_bounded(tmp_path) -> None:
    """R5: a holder replying length 0 yields bounded blob-unavailable."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    holder_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    receiver_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    receiver = OrgFsRuntime(
        tmp_path / "receiver", node_id="node-b", author="user:owner"
    )
    registration = None
    try:
        time.sleep(0.3)
        digest = hashlib.sha256(b"data").hexdigest()
        response = json.dumps(
            {
                "schemaVersion": 1,
                "type": "orgfs-blob-chunk",
                "digest": digest,
                "offset": 0,
                "length": 0,
                "total": 4,
                "data": "",
            },
            separators=(",", ":"),
        ).encode()
        registration = holder_transport.declare_query_handler(
            KeySpace().orgfs_blob(digest),
            lambda selector, _payload: ((selector, response),),
        )
        space = receiver.facade.create_space("team")
        receiver.bind_transport(
            receiver_transport,
            supplier_online=lambda node: node == "node-a",
            holder_candidates=lambda: ("node-a",),
        )

        outcome: list[object] = []
        finished = threading.Event()

        def fetch() -> None:
            try:
                outcome.append(receiver.fetch_blob(space.space_id, digest))
            except Exception as exc:  # noqa: BLE001 - the assertion inspects it
                outcome.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=fetch, daemon=True)
        worker.start()
        assert finished.wait(1.5), "zero-length reply left blob fetch looping"
        worker.join(0.1)
        assert len(outcome) == 1
        assert isinstance(outcome[0], OrgFsError)
        assert outcome[0].code == "blob-unavailable"
    finally:
        receiver.close()
        if registration is not None:
            registration.close()
        receiver_transport.close()
        holder_transport.close()
