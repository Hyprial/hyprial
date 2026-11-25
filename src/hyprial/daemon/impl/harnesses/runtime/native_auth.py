"""Bind successful carrier responses to this launch's selected native import.

The existing streaming process owns admission and completion. This observer
does not authorize a launch or send a reply, and preserves the hook observer.
"""

from hyprial.daemon.impl.api import HarnessResultStatus


def native_auth_observer(launch, observer):
    proof = getattr(launch, "native_auth_launch", None)
    return _NativeAuthObserver(proof, observer) if proof is not None else observer


class _NativeAuthObserver:
    def __init__(self, proof, observer):
        self._proof = proof
        self._observer = observer

    def turn_started(self, delivery, *, started_at_ms):
        callback = getattr(self._observer, "turn_started", None)
        if callable(callback):
            callback(delivery, started_at_ms=started_at_ms)

    def __call__(self, delivery, result, *, started_at_ms, ended_at_ms, tool_names):
        if (result.status == HarnessResultStatus.COMPLETED and result.output.strip()
                and result.error is None and result.failure_code is None
                and result.delivery_id == delivery.delivery_id and result.recipient == delivery.recipient
                and ended_at_ms >= started_at_ms):
            self._proof.confirm(ended_at_ms)
        if self._observer is not None:
            self._observer(delivery, result, started_at_ms=started_at_ms,
                           ended_at_ms=ended_at_ms, tool_names=tool_names)
