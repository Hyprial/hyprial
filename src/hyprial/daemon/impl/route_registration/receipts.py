"""RouteRegistrationIo receipt cluster: reassociation, retirement and confirmation of native route receipts."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import uuid
from dataclasses import replace
from queue import Full
from hyprial.kernel  import MutationProvenance
from .commands import _ConfirmReceipt, _ReassociateReceipt, _RetireReceipt

class _RouteReceiptMixin:
    """RouteRegistrationIo cluster; the composing class owns the state."""

    def reassociate(
        self,
        attempt_token: str,
        *,
        correlation_id: str,
        generation: int,
        version: int,
    ) -> bool:
        """Fence an executed completion into a new receiver generation."""

        return bool(self._call_control(_ReassociateReceipt(
            uuid.uuid4().hex, attempt_token, correlation_id, generation, version
        )))

    def _reassociate_owned(
        self, attempt_token: str, correlation_id: str,
        generation: int, version: int,
    ) -> bool:

        with self._condition:
            custody = self._custody.get(attempt_token)
            if custody is None or custody.event is None:
                return False
            custody.event = replace(
                custody.event,
                correlation_id=correlation_id,
                generation=generation,
                version=version,
            )
            try:
                self._queue.put_nowait(attempt_token)
            except Full:
                # Queue fullness can be another route's command. The owner
                # retains this token for a later settlement pass.
                self._resettle_pending.add(attempt_token)
                return True
            self._condition.notify_all()
            return True

    def receipt_pending(self, attempt_token: str) -> bool:
        with self._lock:
            return attempt_token in self._receipts

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool:
        return bool(self._call_control(_RetireReceipt(
            uuid.uuid4().hex, attempt_token, resource_token
        )))

    def _retire_receipt_owned(self, attempt_token: str, resource_token: str) -> bool:
        with self._condition:
            retired = self._retired_receipts.get(attempt_token)
            if retired is not None:
                if retired != resource_token:
                    raise ValueError("retired route receipt token mismatch")
                return True
            receipt = self._receipts.get(attempt_token)
            if receipt is None:
                custody = self._custody.get(attempt_token)
                provenance = (
                    None
                    if custody is None or custody.event is None
                    else getattr(custody.event, "provenance", None)
                )
                if isinstance(provenance, MutationProvenance):
                    if provenance.resource_token != resource_token:
                        raise ValueError("route custody resource token mismatch")
                    self._retire_requests[attempt_token] = resource_token
                    return True
                return False
            _command, event = receipt
            provenance = getattr(event, "provenance", None)
            if (
                not isinstance(provenance, MutationProvenance)
                or provenance.resource_token != resource_token
            ):
                raise ValueError("route receipt resource token mismatch")
            del self._receipts[attempt_token]
            self._retired_receipts[attempt_token] = resource_token
            self._condition.notify_all()
            return True

    def confirm_receipt_retired(self, attempt_token: str, resource_token: str) -> None:
        self._call_control(_ConfirmReceipt(
            uuid.uuid4().hex, attempt_token, resource_token
        ))

    def _confirm_receipt_owned(self, attempt_token: str, resource_token: str) -> None:
        with self._condition:
            retired = self._retired_receipts.get(attempt_token)
            if retired is None:
                pending = self._retire_requests.get(attempt_token)
                if pending is not None:
                    if pending != resource_token:
                        raise ValueError(
                            "route pending retirement confirmation token mismatch"
                        )
                    self._confirm_requests[attempt_token] = resource_token
                return
            if retired != resource_token:
                raise ValueError("route retirement confirmation token mismatch")
            del self._retired_receipts[attempt_token]
            self._condition.notify_all()
