"""Imported waits must be finite or provably cancellable asyncio primitives."""
import ast
from pathlib import Path
import subprocess
import threading

import pytest

from hyprial.daemon.impl.adapters.worker.process import AdapterWorkerProcess, CLOSED
from hyprial.daemon.impl.adapters.worker.session import NativeSessionWriterLock, SessionWriterHeld


def test_session_lock_explicitly_rejects_blocking_and_keeps_single_writer(tmp_path):
    first = NativeSessionWriterLock(tmp_path, "weixin", "instance", 1)
    second = NativeSessionWriterLock(tmp_path, "weixin", "instance", 1)
    with pytest.raises(ValueError, match="nonblocking"):
        first.acquire(blocking=True)
    try:
        first.acquire(blocking=False)
        with pytest.raises(SessionWriterHeld):
            second.acquire(blocking=False)
    finally:
        first.release()
        second.release()
    with second:
        assert second.locked
    assert not second.locked


def test_worker_monitor_polls_with_a_finite_process_wait():
    timeouts = []

    class Process:
        def wait(self, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise subprocess.TimeoutExpired("owned-worker", timeout)
            return 0

    worker = object.__new__(AdapterWorkerProcess)
    worker._process = Process()
    worker._guard = threading.Lock()
    worker._expected_close = True
    worker._link = None
    worker._exited = threading.Event()
    worker._monitor()
    assert len(timeouts) == 2 and all(0 < value <= 1 for value in timeouts)
    assert worker._state == CLOSED and worker._exited.is_set()


@pytest.mark.parametrize("relative,method,field,factory", [
    ("discord/provider.py", "wait", "_receive_gate", "Event"),
    ("qq/provider.py", "get", "_ingress_queue", "Queue"),
    ("whatsapp/runtime.py", "get", "_ingress", "Queue"),
])
def test_intentional_ingress_wait_is_awaited_and_constructed_from_asyncio(
    relative, method, field, factory,
):
    root = Path(__file__).resolve().parents[4] / "src/hyprial/daemon/impl/adapters"
    tree = ast.parse((root / relative).read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == method
             and not node.args and not node.keywords]
    assert calls and all(ast.unparse(node.func.value) == f"self.{field}" for node in calls)
    assert all(isinstance(parents[node], ast.Await) for node in calls)
    initializers = []
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(ast.unparse(target) == f"self.{field}" for target in targets):
            initializers.append(node.value)
    assert initializers and all(isinstance(value, ast.Call)
                               and ast.unparse(value.func) == f"asyncio.{factory}"
                               for value in initializers)
