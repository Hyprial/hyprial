from __future__ import annotations

import threading

from hyprial.kernel import ProcessLiveness, ProcessLivenessState
from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessAuthority


class _Process:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.entered = threading.Event()
        self.probes = 0

    def liveness(self) -> ProcessLiveness:
        self.probes += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(2)
        return ProcessLiveness(
            ProcessLivenessState.DEAD, observed=True, pid=123, marker="birth:1"
        )

    def stop(self) -> None:
        pass


def test_custody_mutations_and_collection_publish_immutable_snapshot(tmp_path) -> None:
    authority = OrphanProcessAuthority(
        tmp_path / "orphans.json", identity_reader=lambda _pid: "birth:1"
    )
    process = _Process()
    orphan_id = authority.observe_start("pi:worker", process, pid=123, marker="birth:1")
    assert authority.observe_stop(
        "pi:worker", process, pid=123, marker="birth:1"
    ) == orphan_id
    rows = authority.status()
    assert rows[0]["orphanId"] == orphan_id
    rows[0]["state"] = "tampered"
    assert authority.status()[0]["state"] != "tampered"
    assert authority.collect_once() == 1
    assert authority.status() == ()
    assert process.probes == 1
    assert authority.close(2)


def test_blocked_probe_prevents_custody_teardown(tmp_path) -> None:
    gate = threading.Event()
    authority = OrphanProcessAuthority(
        tmp_path / "orphans.json", identity_reader=lambda _pid: "birth:1"
    )
    process = _Process(gate)
    authority.observe_stop("pi:worker", process, pid=123, marker="birth:1")
    result: list[int] = []
    worker = threading.Thread(target=lambda: result.append(authority.collect_once()))
    worker.start()
    assert process.entered.wait(1)
    assert not authority.close(0.02)
    gate.set()
    worker.join(2)
    assert result == [1]
    assert authority.close(2)
