"""Python worker terminal route frame closure, id dedup, bounds and defaults (I5-4)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.daemon.impl.harnesses.python_worker import (
    MAX_ROUTE_LABEL_LEN,
    MAX_TERMINAL_OUTPUT_BYTES,
    PythonWorkerTurnAdapter,
)
from hyprial.daemon.impl.harnesses.python_worker import _python_worker as worker
from hyprial.daemon.impl.harnesses.python_worker.__init__ import _DeliveryState
from hyprial.kernel import HarnessLaunchSpec


def _adapter(kind: str = "jev") -> PythonWorkerTurnAdapter:
    spec = HarnessLaunchSpec(harness=kind, name="route-worker", headless=True)
    adapter = PythonWorkerTurnAdapter.__new__(PythonWorkerTurnAdapter)
    adapter.spec = spec
    adapter.kind = kind
    adapter._env = {}
    adapter.worker_channel = None
    adapter._logger = logging.getLogger("test.route_terminal")
    adapter._delivery_projection_failure_kinds = set()
    adapter._on_turn_completed = None
    import queue
    import threading

    adapter._lock = threading.Lock()
    adapter._ready = threading.Event()
    adapter._ready.set()
    adapter._stopping = threading.Event()
    adapter._process = SimpleNamespace(poll=lambda: None)
    adapter._records = {}
    adapter._terminal_ids = set()
    adapter._results = queue.Queue()
    adapter._ready_error = None
    adapter.last_error = None
    adapter.max_in_flight = 10
    return adapter


def _reserve(adapter: PythonWorkerTurnAdapter, delivery_id: str) -> HarnessDelivery:
    delivery = HarnessDelivery(
        delivery_id, "conv-1", "agent:a:n:s", "agent:a:n:w", "payload"
    )
    adapter._records[delivery_id] = _DeliveryState(delivery, 0.0, 0)
    return delivery


def test_legacy_result_without_label_naturally_defaults_to_done() -> None:
    adapter = _adapter()
    _reserve(adapter, "req-1")

    adapter._handle_frame(
        {"v": 1, "type": "result", "id": "req-1", "ok": True, "output": {"answer": 42}}
    )

    drained = adapter.drain_results()
    assert len(drained) == 1
    assert drained[0].status is HarnessResultStatus.COMPLETED
    assert drained[0].route_outcome == "done"
    assert drained[0].route_label is None


def test_terminal_frames_are_closed_against_unknown_fields() -> None:
    for frame in (
        {"v": 1, "type": "result", "id": "r1", "ok": True, "output": {}, "extra": 1},
        {
            "v": 1,
            "type": "error",
            "id": "r1",
            "ok": False,
            "stage": "call",
            "code": "PROVIDER_INVALID_REQUEST",
            "message": "bad",
            "retryable": False,
            "extra": 1,
        },
        {
            "v": 1,
            "type": "route",
            "id": "r1",
            "ok": True,
            "outcome": "next",
            "label": "code",
            "unexpected": True,
        },
    ):
        adapter = _adapter()
        _reserve(adapter, "r1")
        adapter._handle_frame(frame)
        assert adapter.last_error is not None
        assert adapter.last_error.startswith("worker protocol failure")
        drained = adapter.drain_results()
        assert len(drained) == 1
        assert drained[0].status is HarnessResultStatus.FAILED


def test_route_terminal_supports_next_done_and_refused_with_id_dedup() -> None:
    adapter = _adapter()
    _reserve(adapter, "r-next")
    _reserve(adapter, "r-done")
    _reserve(adapter, "r-refused")

    adapter._handle_frame(
        {
            "v": 1,
            "type": "route",
            "id": "r-next",
            "ok": True,
            "outcome": "next",
            "label": "review",
            "output": {"step": "ok"},
        }
    )
    adapter._handle_frame(
        {"v": 1, "type": "route", "id": "r-done", "ok": True, "outcome": "done"}
    )
    adapter._handle_frame(
        {"v": 1, "type": "route", "id": "r-refused", "ok": False, "outcome": "refused"}
    )

    results = adapter.drain_results()
    assert [(r.delivery_id, r.route_outcome, r.route_label) for r in results] == [
        ("r-next", "next", "review"),
        ("r-done", "done", None),
        ("r-refused", "refused", None),
    ]

    adapter._handle_frame(
        {"v": 1, "type": "route", "id": "r-next", "ok": True, "outcome": "done"}
    )
    assert adapter.last_error == "worker protocol failure: worker terminal id is not reserved"
    assert adapter.enqueue(
        HarnessDelivery("r-next", "conv-1", "agent:a:n:s", "agent:a:n:w", "again")
    ) is False


def test_route_label_and_output_are_strictly_bounded() -> None:
    adapter_bad_label = _adapter()
    _reserve(adapter_bad_label, "r-long-label")
    adapter_bad_label._handle_frame(
        {
            "v": 1,
            "type": "route",
            "id": "r-long-label",
            "ok": True,
            "outcome": "next",
            "label": "x" * (MAX_ROUTE_LABEL_LEN + 1),
        }
    )
    assert adapter_bad_label.last_error is not None
    assert adapter_bad_label.drain_results()[0].status is HarnessResultStatus.FAILED

    adapter_bad_output = _adapter()
    _reserve(adapter_bad_output, "r-big-output")
    adapter_bad_output._handle_frame(
        {
            "v": 1,
            "type": "result",
            "id": "r-big-output",
            "ok": True,
            "output": {"blob": "y" * (MAX_TERMINAL_OUTPUT_BYTES + 16)},
        }
    )
    assert adapter_bad_output.last_error is not None
    assert adapter_bad_output.drain_results()[0].status is HarnessResultStatus.FAILED


def test_python_worker_route_frame_helper_enforces_bounds_and_closure() -> None:
    frame = worker.build_result_frame("req-1", {"ok": True})
    assert frame == {"v": 1, "type": "result", "id": "req-1", "ok": True, "output": {"ok": True}}
    with pytest.raises(worker.WorkerError):
        worker.build_result_frame(
            "req-1",
            {"blob": "z" * (worker.MAX_OUTPUT_BYTES + 32)},
        )
    with pytest.raises(worker.WorkerError):
        worker.build_route_frame(
            "req-1",
            outcome="next",
            label="k" * (worker.MAX_LABEL_LEN + 1),
        )


@pytest.mark.parametrize("kind", ["jev", "user-proxy"])
def test_a_forward_frame_is_an_unknown_frame_for_every_worker_kind(kind: str) -> None:
    """The user-proxy kind and its forward lane are deleted (I7b-2)."""

    from dataclasses import fields

    from hyprial.daemon.impl.api import HarnessResult
    from hyprial.daemon.impl.harnesses.python_worker import PYTHON_WORKER_KINDS

    assert PYTHON_WORKER_KINDS == frozenset({"jev"})
    assert "forward_to" not in {field.name for field in fields(HarnessResult)}
    adapter = _adapter(kind)
    _reserve(adapter, "r-forward")
    adapter._handle_frame(
        {
            "v": 1,
            "type": "forward",
            "id": "r-forward",
            "ok": True,
            "to": "agent:a:n:x",
            "message": "hello",
        }
    )
    assert adapter.last_error is not None
    assert "worker emitted an unknown frame type" in adapter.last_error
    drained = adapter.drain_results()
    assert len(drained) == 1
    assert drained[0].status is HarnessResultStatus.FAILED
