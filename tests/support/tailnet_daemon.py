"""Explicit membership-port fixture for isolated, non-network daemon tests.

Only org service construction is adapted. CLI startup, IPC, OrgFS, management
permissions, sessions and messages remain real. This is not tailnet acceptance:
no transport, key derivation, remote authority or lease timer is exercised.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from unittest.mock import patch

from hyprial.daemon.impl.org.network import ipc
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from tests.support.tailnet_membership import FakeMembershipSidecar


def bind_admitted(runtime, transport, **kwargs):
    """Bind a cold test runtime as an explicitly admitted private-path peer.

    Daemon-shaped fixtures obtain their runtime from the real composition root,
    so select the policy before any mesh is constructed. Loopback transport is
    test I/O, not a claim that production admits arbitrary TCP peers.
    """
    assert not runtime._meshes, "admission policy must precede mesh construction"
    runtime._trust_tailnet_members = True
    runtime.bind_transport(transport, **kwargs)


@contextmanager
def membership_services():
    """Supply a local authority double; org.create must still issue v6 init.

    The test double persists its own tables and receipts across CLI restarts.
    It never reconstructs admission from OrgFS. Separate owner identities get
    separate stores, so an owner migration cannot adopt the old owner's roster.
    No global/autouse fixture selects this adapter.
    """
    original = ipc._build_service
    sidecars = {}
    lock = RLock()

    def build(app, *, membership_timeout=None):
        with lock:
            return build_locked(app, membership_timeout)

    def build_locked(app, membership_timeout):
        identity = (str(app.hyprial_home), str(app.owner), app.node_id)
        if identity not in sidecars:
            home = Path(app.hyprial_home)
            assert os.environ.get("HYPRIAL_NETWORK_ISOLATED") == "1"
            fake = FakeMembershipSidecar(owner=str(app.owner), device_id=app.node_id, now_ms=None)
            digest = hashlib.sha256(str(app.owner).encode()).hexdigest()[:16]
            store = home / "test-membership" / f"{digest}.json"
            store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if store.exists():
                saved = json.loads(store.read_text())
                fake.tailnets = saved["tailnets"]
                fake.receipts = saved["receipts"]
                # No member lease survives restart. Authority-local readiness
                # follows the clarification in the frozen contract.
            sidecars[identity] = (fake, store)
        fake, store = sidecars[identity]
        row = fake.local
        write_device_record(Path(app.hyprial_home), DeviceRecord(
            device_id=row["deviceId"], owner=row["owner"],
            client_public=row["masterPublic"], server_public=row["serverPublic"],
        ))

        def request(op, params):
            with lock:
                # The request-path bound reaches the double like the real owner.
                result = fake.membership_request(op, params, timeout=membership_timeout)
                temporary = store.with_suffix(".tmp")
                temporary.write_text(json.dumps({"tailnets": fake.tailnets, "receipts": fake.receipts}))
                temporary.chmod(0o600)
                temporary.replace(store)
                return result

        service = original(app, membership_timeout=membership_timeout)
        service._membership_request = request
        service._read_own_address = lambda: row["address"]
        return service

    with patch.object(ipc, "_build_service", build):
        yield sidecars


if __name__ == "__main__":
    # Explicit test launcher around the unchanged CLI entry point. Other
    # IsolatedHarness users continue to launch hyprial.cli directly.
    from hyprial.cli import main

    with membership_services():
        main()
