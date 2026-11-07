"""Durable ack after an uncatchable process kill and audited recovery."""
import json
import signal
import subprocess
import sys

from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport


def test_ack_uses_synchronous_full_before_success(tmp_path):
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        assert service._db.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        service.close()


def test_successful_ack_survives_sigkill_with_other_pending_preserved(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    program = '''
import json, sys, time
from pathlib import Path
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport, InboxMessage, DeliveryLifecycle
service=InboxService(Path(sys.argv[1]), MemoryDeliveryTransport())
for name in ('acked','pending'):
 message=InboxMessage(message_id=name, conversation_id='crash', sender='agent:sender:node:s',recipient='agent:owner:node:r',payload=b'{}',intent='request',lifecycle=DeliveryLifecycle.DURABLE_SERVICE,idempotency_key=None,created_at_ms=0)
 service.receive(message)
assert service.ack('agent:owner:node:r','acked').acknowledged
print('ACK_COMMITTED',flush=True)
time.sleep(30)
'''
    child = subprocess.Popen([sys.executable, "-c", program, str(path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ACK_COMMITTED"
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    events = []

    class Log:
        def log(self, level, event, **fields):
            events.append((event, fields))

    restored = InboxService(path, MemoryDeliveryTransport(), logger=Log())
    try:
        assert [m.message_id for m in restored.pending_all()] == ["pending"]
        assert restored.ack("agent:owner:node:r", "acked").acknowledged
        recovery = [fields for event, fields in events if event == "inbox.recovery.pending"]
        assert recovery and recovery[0]["messageIds"] == ["pending"]
        assert "acked" not in json.dumps(recovery)
    finally:
        restored.close()
