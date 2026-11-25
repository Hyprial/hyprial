"""Trusted serving port for the P3 first-run attempt.

Decision record (web-auth-r3-p3-port-20261118):
- The port derives the sender and subject from the verified local identity on
  every call; no request parameter can name an owner, session or peer.
- It only exists on the serving gateway.  A bootstrap caller has no committed
  login generation, so an absent port answers DAEMON_NOT_READY rather than a
  fabricated attempt.
- Store, verification and authority refusals keep their stable codes; the
  handler still checks those codes against the MethodSpec before they reach
  the wire.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.kernel import canonical_user_uri

from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    _SERVING_CALLERS,
    _require_context,
)
from hyprial.daemon.impl.application.onboarding.errors import OnboardingGatewayError
from hyprial.daemon.impl.onboarding.first_run import (
    FirstRunStoreError,
    FirstRunVerificationError,
)
from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    FIRST_RUN_SUBJECT_REQUIRED,
)
from hyprial.daemon.impl.request_catalog.onboarding.state import CallContext

from .service import FirstRunAttemptService, FirstRunAuthorityError


class FirstRunAttemptPort:
    """Compose the attempt service with the gateway's trusted identity."""

    def __init__(
        self,
        gateway: Any,
        service: FirstRunAttemptService,
        current: Callable[[Any], Any] | None = None,
    ) -> None:
        self._gateway = gateway
        self._service = service
        self._current = current or service.read

    def begin(
        self,
        context: CallContext,
        *,
        operation_key: str,
        agent_uri: str,
    ) -> dict[str, Any]:
        _, _owner, subject = self._trusted(context)
        attempt = self._call(
            lambda: self._service.begin(
                subject,
                operation_key=operation_key,
                agent_uri=agent_uri,
            )
        )
        return {"ok": True, "attempt": attempt.as_status()}

    def bind(
        self,
        context: CallContext,
        *,
        attempt_id: str,
        expected_revision: int,
        operation_key: str,
        conversation_id: str,
        request_message_id: str,
    ) -> dict[str, Any]:
        _, owner, subject = self._trusted(context)
        attempt = self._call(
            lambda: self._service.bind(
                subject,
                trusted_sender=_sender(owner),
                attempt_id=attempt_id,
                expected_revision=expected_revision,
                operation_key=operation_key,
                conversation_id=conversation_id,
                request_message_id=request_message_id,
            )
        )
        return {"ok": True, "attempt": attempt.as_status()}

    def confirm(
        self,
        context: CallContext,
        *,
        attempt_id: str,
        expected_attempt_revision: int,
        expected_receipt_revision: int,
        operation_key: str,
    ) -> dict[str, Any]:
        _, owner, subject = self._trusted(context)
        result = self._call(
            lambda: self._service.confirm(
                subject,
                trusted_sender=_sender(owner),
                attempt_id=attempt_id,
                expected_attempt_revision=expected_attempt_revision,
                expected_receipt_revision=expected_receipt_revision,
                operation_key=operation_key,
            )
        )
        return {
            "ok": True,
            "attempt": result.attempt.as_status(),
            "firstRun": result.first_run.as_status(),
            "requestMessageId": result.request_message_id,
            "replyMessageId": result.reply_message_id,
        }

    def cancel(
        self,
        context: CallContext,
        *,
        attempt_id: str,
        expected_revision: int,
        operation_key: str,
    ) -> dict[str, Any]:
        _, _owner, subject = self._trusted(context)
        attempt = self._call(
            lambda: self._service.cancel(
                subject,
                attempt_id=attempt_id,
                expected_revision=expected_revision,
                operation_key=operation_key,
            )
        )
        return {"ok": True, "attempt": attempt.as_status()}

    def skip(
        self,
        context: CallContext,
        *,
        expected_revision: int,
        operation_key: str,
    ) -> dict[str, Any]:
        _, _owner, subject = self._trusted(context)
        receipt = self._call(
            lambda: self._service.skip(
                subject,
                expected_revision=expected_revision,
                operation_key=operation_key,
            )
        )
        return {"ok": True, "firstRun": receipt.as_status()}

    def status_for(self, subject: Any) -> dict[str, Any] | None:
        """The non-secret attempt projection; corruption answers no attempt."""

        try:
            attempt = self._current(subject)
        except Exception:
            return None
        return None if attempt is None else attempt.as_status()

    def attach_status(self, projected: dict[str, Any], subject: Any) -> None:
        """Attach the attempt to the same trusted status snapshot."""

        if subject is None:
            return
        attempt = self.status_for(subject)
        if attempt is not None:
            projected["firstRunAttempt"] = attempt

    def status(self) -> dict[str, Any] | None:
        """The attempt for the currently verified subject, or no attempt."""

        try:
            _owner, subject = self._gateway.first_run_trusted()
        except Exception:
            return None
        return None if subject is None else self.status_for(subject)

    def _trusted(self, context: CallContext) -> tuple[CallContext, str, Any]:
        checked = _require_context(context, "serving", _SERVING_CALLERS)
        owner, subject = self._gateway.first_run_trusted()
        if subject is None:
            raise OnboardingGatewayError(
                FIRST_RUN_SUBJECT_REQUIRED,
                "a stable onboarding subject is required for the first-run attempt",
            )
        return checked, owner, subject

    @staticmethod
    def _call(action: Callable[[], Any]) -> Any:
        try:
            return action()
        except OnboardingGatewayError:
            raise
        except (
            FirstRunStoreError,
            FirstRunVerificationError,
            FirstRunAuthorityError,
        ) as error:
            raise OnboardingGatewayError(error.code, str(error)) from error


def _sender(owner: str) -> str:
    return canonical_user_uri(owner)


__all__ = ["FirstRunAttemptPort"]
