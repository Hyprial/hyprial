"""Mailbox-owned duplicate-generation observations for daemon composition."""

from __future__ import annotations

import threading

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.transport.api import TransportSample
from .duplicate_watch import DuplicateInstanceWatch


class DuplicateInstanceAuthority(DuplicateInstanceWatch):
    def __init__(self, session, node_id, generation, *, keys=None, logger=None):
        if not node_id or not generation:
            raise ValueError("node_id and generation must not be empty")
        self._admission_guard = threading.Lock()
        self._admission_closed = False
        self._rejected = 0
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="duplicate-instance-watch",
                handler_factory=lambda: self._observe,
                mailbox_capacity=128,
            )
        )
        try:
            super().__init__(session, node_id, generation, keys=keys, logger=logger)
        except BaseException:
            self._runtime.stop(self._handle, 1.0)
            raise

    def _on_sample(self, sample):
        message = TransportSample(
            str(sample.key), bytes(sample.payload), str(sample.kind)
        )
        with self._admission_guard:
            admission = (
                AdmissionResult.CLOSED
                if self._admission_closed
                else self._runtime.tell(self._handle, message)
            )
            if admission is not AdmissionResult.ACCEPTED:
                self._rejected += 1

    def _observe(self, message):
        if not isinstance(message, TransportSample):
            raise TypeError("unsupported duplicate presence command")
        # The configured daemon logger admits to its async LogWriter. The
        # pure dedup decision and immutable projection stay in this mailbox.
        super()._on_sample(message)

    def status_payload(self):
        result = super().status_payload()
        with self._admission_guard:
            result["admissionRejected"] = self._rejected
        return result

    def close(self):
        with self._admission_guard:
            self._admission_closed = True
        super().close()
        if not self._runtime.stop(self._handle, 1.0):
            raise TimeoutError("duplicate watch still owns admitted observations")
